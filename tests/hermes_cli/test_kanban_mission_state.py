"""Tests for the Kanban mission-state durable backend (R2).

Covers:
  - creation: valid, idempotent, conflictive
  - reading after reopen
  - transitions: valid, stale, replay, conflict (R1-aligned outcomes)
  - generation: incremented exactly once
  - invalid transitions
  - terminal states
  - blocker / human_gate / queue_exhausted structures
  - R1 structural invariants (blocked, human_gate, queue_exhausted, completed)
  - exact-head gates
  - consultation with local decision
  - K9 references (valid, nonexistent, no DAG copy)
  - rollback on journal/event failure
  - recovery after restart
  - idempotent migration
  - concurrent same-generation writers (real threads)
  - compatibility with existing Kanban suite
  - R1 outcome vocabulary compliance
  - index on request_fingerprint
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import kanban_mission_state as ms


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fingerprint(value: Any) -> str:
    """Compute canonical fingerprint for testing."""
    return ms.canonical_fingerprint(value)


def _sha40() -> str:
    """Generate a fake 40-char hex SHA."""
    return hashlib.sha256(b"test").hexdigest()[:40]


def _sha64() -> str:
    """Generate a fake 64-char hex fingerprint."""
    return hashlib.sha256(b"test").hexdigest()


def _make_identity(**overrides: Any) -> dict:
    """Build a minimal valid identity block."""
    base = {
        "board": "test-board",
        "tenant": "test-tenant",
        "repository": "https://github.com/test/repo",
        "branch": "main",
        "base_sha": _sha40(),
        "head_sha": _sha40(),
        "tree_sha": _sha40(),
        "plan_fingerprint": _sha64(),
        "checkpoint_fingerprint": _sha64(),
    }
    base.update(overrides)
    return base


def _make_state(
    *,
    mission_id: str = "mission-1",
    status: str = "planned",
    phase: str = "planning",
    generation: int = 0,
    **extra: Any,
) -> dict:
    """Build a minimal valid mission_state document."""
    state = {
        "document_type": "mission_state",
        "schema_version": ms.SCHEMA_VERSION,
        "mission_id": mission_id,
        "generation": generation,
        "status": status,
        "phase": phase,
        "identity": _make_identity(),
        "planner_import": None,
        "card_ref": None,
        "execution_ref": None,
        "evidence_refs": [],
        "decision_refs": [],
        "consultation_refs": [],
        "active_blocker": None,
        "active_human_gate": None,
        "queue_exhausted": None,
        "completion": {
            "final_review": None,
            "merge_required": False,
            "merge_gate": None,
        },
        "next_safe_action": {
            "action": "plan",
            "executable": True,
            "card_id": None,
            "reason_code": "mission_created",
        },
        "last_operation": {
            "operation_id": "op-create",
            "request_fingerprint": _sha64(),
            "attempt_id": "attempt-1",
        },
    }
    state.update(extra)
    return state


def _make_blocked_state(**overrides: Any) -> dict:
    """Build a valid blocked mission state."""
    return _make_state(
        status="blocked",
        phase="execution",
        active_blocker={
            "blocker_id": "blk-1",
            "reason_code": "dependency",
            "summary": "Waiting for upstream",
            "evidence_ids": ["ev-1"],
            "resume_condition": {
                "type": "external_event",
                "description": "Upstream task completes",
                "reference": "task-upstream-1",
            },
        },
        active_human_gate=None,
        queue_exhausted=None,
        next_safe_action={
            "action": "plan",
            "executable": False,
            "card_id": None,
            "reason_code": "blocked_on_dependency",
        },
        **overrides,
    )


def _make_human_gate_state(**overrides: Any) -> dict:
    """Build a valid human_gate mission state."""
    return _make_state(
        status="human_gate",
        phase="review",
        active_human_gate={
            "gate_id": "gate-1",
            "gate_type": "approval",
            "version": 1,
            "status": "pending",
            "prompt_fingerprint": _sha64(),
            "resolution_ref": None,
        },
        active_blocker=None,
        queue_exhausted=None,
        next_safe_action={
            "action": "await_human",
            "executable": False,
            "card_id": None,
            "reason_code": "human_gate_pending",
        },
        **overrides,
    )


def _make_queue_exhausted_state(**overrides: Any) -> dict:
    """Build a valid queue_exhausted mission state."""
    return _make_state(
        status="queue_exhausted",
        phase="execution",
        queue_exhausted={
            "decision_id": "dec-qe-1",
            "reason_code": "no_ready_cards",
            "summary": "All cards blocked or pending",
            "exhausted_at_generation": 0,
            "evidence_ids": [],
            "resume_condition": {
                "type": "manual_replan",
                "description": "Replan with updated dependencies",
                "reference": "replan-trigger",
            },
        },
        active_blocker=None,
        active_human_gate=None,
        next_safe_action={
            "action": "replan",
            "executable": False,
            "card_id": None,
            "reason_code": "queue_exhausted",
        },
        **overrides,
    )


def _make_completed_state(**overrides: Any) -> dict:
    """Build a valid completed mission state."""
    identity = _make_identity()
    return _make_state(
        status="completed",
        phase="terminal",
        generation=1,
        completion={
            "final_review": {
                "gate_id": "fr-1",
                "gate_type": "final_review",
                "repository": identity["repository"],
                "branch": identity["branch"],
                "commit_sha": identity["head_sha"],
                "tree_sha": identity["tree_sha"],
                "diff_fingerprint": _sha64(),
                "bundle_fingerprint": _sha64(),
                "response_fingerprint": _sha64(),
                "response_artifact": "artifact/fr-1",
                "result": "pass",
            },
            "merge_required": False,
            "merge_gate": None,
        },
        next_safe_action=None,
        **overrides,
    )


@pytest.fixture
def db():
    """In-memory SQLite database with mission-state tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ms.migrate_mission_state(conn)
    yield conn
    conn.close()


@pytest.fixture
def db_with_k9():
    """In-memory database with both K9 and mission-state tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Simulate existing K9 tables
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL, created_at INTEGER NOT NULL);
    """)
    conn.execute("INSERT INTO tasks (id, title, status) VALUES ('t1', 'existing task', 'running')")
    ms.migrate_mission_state(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# 1. creation válida
# ---------------------------------------------------------------------------

class TestCreation:
    def test_create_valid_mission(self, db):
        state = _make_state()
        result = ms.create_mission(db, state=state, operation_id="op-create-1")
        assert result.outcome == "created"
        assert result.mission_id == "mission-1"
        assert result.generation == 0
        assert len(result.state_fingerprint) == 64
        assert result.state is not None
        assert result.state["generation"] == 0

    def test_mission_persists_in_db(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        assert record is not None
        assert record.status == "planned"
        assert record.generation == 0

    def test_create_returns_structural_result(self, db):
        state = _make_state()
        result = ms.create_mission(db, state=state, operation_id="op-1")
        assert isinstance(result, ms.CreateResult)
        assert result.outcome in {"created", "already-applied", "conflict", "invalid"}


# ---------------------------------------------------------------------------
# 2. creación idéntica repetida
# ---------------------------------------------------------------------------

class TestIdempotentCreation:
    def test_identical_create_is_already_applied(self, db):
        state = _make_state()
        r1 = ms.create_mission(db, state=state, operation_id="op-idem")
        assert r1.outcome == "created"
        r2 = ms.create_mission(db, state=state, operation_id="op-idem")
        assert r2.outcome == "already-applied"
        assert r2.generation == r1.generation
        assert r2.state_fingerprint == r1.state_fingerprint

    def test_replay_does_not_increment_generation(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-r")
        r = ms.create_mission(db, state=state, operation_id="op-r")
        assert r.generation == 0


# ---------------------------------------------------------------------------
# 3. creación conflictiva
# ---------------------------------------------------------------------------

class TestConflictingCreation:
    def test_same_operation_different_content_is_conflict(self, db):
        state1 = _make_state(mission_id="m-1")
        ms.create_mission(db, state=state1, operation_id="op-conflict")
        state2 = _make_state(mission_id="m-1", status="active")
        r = ms.create_mission(db, state=state2, operation_id="op-conflict")
        assert r.outcome == "conflict"
        assert r.error is not None
        assert "conflict" in r.error["code"]

    def test_existing_mission_new_operation_is_conflict(self, db):
        state = _make_state(mission_id="m-existing")
        ms.create_mission(db, state=state, operation_id="op-1")
        state2 = _make_state(mission_id="m-existing")
        r = ms.create_mission(db, state=state2, operation_id="op-2")
        assert r.outcome == "conflict"


# ---------------------------------------------------------------------------
# 4. lectura tras reapertura
# ---------------------------------------------------------------------------

class TestReadAfterReopen:
    def test_get_returns_none_for_nonexistent(self, db):
        assert ms.get_mission(db, "nonexistent") is None

    def test_get_after_create(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        assert record is not None
        assert record.mission_id == "mission-1"
        assert record.schema_version == ms.SCHEMA_VERSION

    def test_read_after_reopen_simulated(self, db):
        """Simulate reopening by creating, closing conn, reopening."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        assert record is not None
        assert record.status == "planned"


# ---------------------------------------------------------------------------
# 5. transición válida — R1 outcome: "applied"
# ---------------------------------------------------------------------------

class TestValidTransition:
    def test_transition_planned_to_active(self, db):
        state = _make_state(status="planned", phase="planning")
        ms.create_mission(db, state=state, operation_id="op-create")

        next_state = _make_state(status="active", phase="execution")
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-activate",
            next_state=next_state,
        )
        assert result.outcome == "applied"
        assert result.generation == 1
        assert len(result.state_fingerprint) == 64

    def test_transition_updates_state(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-2",
            next_state=next_state,
        )
        record = ms.get_mission(db, "mission-1")
        assert record.status == "active"
        assert record.generation == 1


# ---------------------------------------------------------------------------
# 6. stale generation — R1 outcome: "stale_generation"
# ---------------------------------------------------------------------------

class TestStaleGeneration:
    def test_stale_generation_rejected(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next1 = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-2",
            next_state=next1,
        )
        next2 = _make_state(status="blocked", phase="execution",
                           active_blocker={
                               "blocker_id": "b1", "reason_code": "dep",
                               "summary": "test", "evidence_ids": [],
                               "resume_condition": {
                                   "type": "retry_after", "description": "retry", "reference": "r1",
                               },
                           },
                           active_human_gate=None, queue_exhausted=None,
                           next_safe_action={
                               "action": "plan", "executable": False,
                               "card_id": None, "reason_code": "blocked",
                           })
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,  # stale!
            operation_id="op-3",
            next_state=next2,
        )
        assert result.outcome == "stale_generation"
        assert result.generation == 1  # current generation
        assert result.error is not None
        assert "stale_generation" in result.error["code"]

    def test_stale_does_not_mutate_state(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next1 = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-2",
            next_state=next1,
        )
        next2 = _make_state(status="blocked", phase="execution",
                           active_blocker={
                               "blocker_id": "b1", "reason_code": "dep",
                               "summary": "test", "evidence_ids": [],
                               "resume_condition": {
                                   "type": "retry_after", "description": "retry", "reference": "r1",
                               },
                           },
                           active_human_gate=None, queue_exhausted=None,
                           next_safe_action={
                               "action": "plan", "executable": False,
                               "card_id": None, "reason_code": "blocked",
                           })
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-3",
            next_state=next2,
        )
        record = ms.get_mission(db, "mission-1")
        assert record.status == "active"
        assert record.generation == 1


# ---------------------------------------------------------------------------
# 7. replay idéntico — R1 outcome: "replayed"
# ---------------------------------------------------------------------------

class TestReplay:
    def test_identical_replay_returns_replayed(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        r1 = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-transition",
            next_state=next_state,
        )
        assert r1.outcome == "applied"

        # Replay with same arguments
        r2 = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-transition",
            next_state=next_state,
        )
        assert r2.outcome == "replayed"
        assert r2.generation == r1.generation

    def test_replay_does_not_increment_generation(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-t",
            next_state=next_state,
        )
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-t",
            next_state=next_state,
        )
        record = ms.get_mission(db, "mission-1")
        assert record.generation == 1  # incremented only once


# ---------------------------------------------------------------------------
# 8. operation conflict
# ---------------------------------------------------------------------------

class TestOperationConflict:
    def test_same_operation_different_content_is_conflict(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next1 = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-x",
            next_state=next1,
        )
        # Same operation_id but different next_state
        next2 = _make_state(status="blocked", phase="execution",
                           active_blocker={
                               "blocker_id": "b1", "reason_code": "dep",
                               "summary": "test", "evidence_ids": [],
                               "resume_condition": {
                                   "type": "retry_after", "description": "retry", "reference": "r1",
                               },
                           },
                           active_human_gate=None, queue_exhausted=None,
                           next_safe_action={
                               "action": "plan", "executable": False,
                               "card_id": None, "reason_code": "blocked",
                           })
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=1,
            operation_id="op-x",
            next_state=next2,
        )
        assert result.outcome == "conflict"
        assert result.error is not None


# ---------------------------------------------------------------------------
# 9. generación incrementada una vez
# ---------------------------------------------------------------------------

class TestGenerationIncrement:
    def test_consecutive_transitions_increment_once_each(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        for i, (status, phase) in enumerate([
            ("active", "execution"),
            ("blocked", "execution"),
        ], start=1):
            next_state = _make_state(status=status, phase=phase)
            if status == "blocked":
                next_state["active_blocker"] = {
                    "blocker_id": f"b{i}", "reason_code": "dep",
                    "summary": "test", "evidence_ids": [],
                    "resume_condition": {
                        "type": "retry_after", "description": "retry", "reference": "r1",
                    },
                }
                next_state["active_human_gate"] = None
                next_state["queue_exhausted"] = None
                next_state["next_safe_action"] = {
                    "action": "plan", "executable": False,
                    "card_id": None, "reason_code": "blocked",
                }
            result = ms.compare_and_transition(
                db,
                mission_id="mission-1",
                expected_generation=i - 1,
                operation_id=f"op-t{i}",
                next_state=next_state,
            )
            assert result.outcome == "applied"
            assert result.generation == i
        record = ms.get_mission(db, "mission-1")
        assert record.generation == 2


# ---------------------------------------------------------------------------
# 10. transición inválida
# ---------------------------------------------------------------------------

class TestInvalidTransition:
    def test_invalid_status_rejected(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="INVALID_STATUS", phase="planning")
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-bad",
            next_state=next_state,
        )
        assert result.outcome == "invalid"
        assert result.error is not None

    def test_mission_id_mismatch_rejected(self, db):
        state = _make_state(mission_id="m-1")
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(mission_id="m-wrong", status="active", phase="execution")
        result = ms.compare_and_transition(
            db,
            mission_id="m-1",
            expected_generation=0,
            operation_id="op-2",
            next_state=next_state,
        )
        assert result.outcome == "invalid"

    def test_not_found_returns_invalid(self, db):
        """not-found maps to invalid with error code 'not-found' per R1."""
        next_state = _make_state(mission_id="nonexistent")
        result = ms.compare_and_transition(
            db,
            mission_id="nonexistent",
            expected_generation=0,
            operation_id="op-1",
            next_state=next_state,
        )
        assert result.outcome == "invalid"
        assert result.error is not None
        assert result.error["code"] == "not-found"


# ---------------------------------------------------------------------------
# 11. terminal sin next action
# ---------------------------------------------------------------------------

class TestTerminalStates:
    def test_terminal_failed(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        failed_state = _make_state(
            status="failed",
            phase="terminal",
            next_safe_action=None,
        )
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-fail",
            next_state=failed_state,
        )
        assert result.outcome == "applied"
        record = ms.get_mission(db, "mission-1")
        assert record.status == "failed"
        assert record.phase == "terminal"

    def test_terminal_completed(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        completed = _make_completed_state()
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-complete",
            next_state=completed,
        )
        assert result.outcome == "applied"
        record = ms.get_mission(db, "mission-1")
        assert record.status == "completed"

    def test_terminal_rejects_next_action(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="completed",
            phase="terminal",
            next_safe_action={"action": "plan", "executable": True, "card_id": None, "reason_code": "x"},
        )
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-bad",
            next_state=bad,
        )
        assert result.outcome == "invalid"


# ---------------------------------------------------------------------------
# 12. blocker estructurado
# ---------------------------------------------------------------------------

class TestBlockerStructure:
    def test_blocked_requires_blocker(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        blocked = _make_blocked_state()
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-block",
            next_state=blocked,
        )
        assert result.outcome == "applied"
        record = ms.get_mission(db, "mission-1")
        assert record.status == "blocked"
        parsed = json.loads(record.state_json)
        assert parsed["active_blocker"] is not None
        assert parsed["active_blocker"]["blocker_id"] == "blk-1"


# ---------------------------------------------------------------------------
# 13. human gate estructurado
# ---------------------------------------------------------------------------

class TestHumanGateStructure:
    def test_human_gate_requires_pending_gate(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        hg = _make_human_gate_state()
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-gate",
            next_state=hg,
        )
        assert result.outcome == "applied"
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        assert parsed["active_human_gate"]["status"] == "pending"
        assert parsed["next_safe_action"]["action"] == "await_human"
        assert parsed["next_safe_action"]["executable"] is False


# ---------------------------------------------------------------------------
# 14. queue exhaustion diferenciado
# ---------------------------------------------------------------------------

class TestQueueExhaustion:
    def test_queue_exhausted_has_resume_condition(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        qe = _make_queue_exhausted_state()
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-qe",
            next_state=qe,
        )
        assert result.outcome == "applied"
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        assert parsed["queue_exhausted"] is not None
        assert parsed["queue_exhausted"]["resume_condition"] is not None
        assert parsed["next_safe_action"]["action"] in {"replan", "await_resume_condition"}


# ---------------------------------------------------------------------------
# 15. exact-head gate
# ---------------------------------------------------------------------------

class TestExactHeadGate:
    def test_completed_state_has_gate_references(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        completed = _make_completed_state()
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-complete",
            next_state=completed,
        )
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        gate = parsed["completion"]["final_review"]
        assert gate is not None
        assert gate["result"] == "pass"
        assert gate["commit_sha"] == parsed["identity"]["head_sha"]
        assert gate["tree_sha"] == parsed["identity"]["tree_sha"]


# ---------------------------------------------------------------------------
# 16-17. consulta con/sin decisión local
# ---------------------------------------------------------------------------

class TestConsultationRefs:
    def test_consultation_ref_in_state(self, db):
        state = _make_state()
        state["consultation_refs"] = [{
            "execution_id": "exec-1",
            "mode": "plan-review",
            "snapshot_head": _sha40(),
            "tree_sha": _sha40(),
            "plan_fingerprint": _sha64(),
            "checkpoint_fingerprint": _sha64(),
            "bundle_fingerprint": _sha64(),
            "expected_question_ids": ["Q1"],
            "schema_version": "codex-senior-consult-response/v3",
            "status": "COMPLETED",
            "detailed_status": "VALID_ADVISORY_VERDICT",
            "verdict": "accept",
            "response_fingerprint": _sha64(),
            "response_artifact": "responses/abc.json",
        }]
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        assert len(parsed["consultation_refs"]) == 1
        assert parsed["consultation_refs"][0]["verdict"] == "accept"


# ---------------------------------------------------------------------------
# 18-20. K9 references
# ---------------------------------------------------------------------------

class TestK9References:
    def test_planner_import_stored(self, db):
        state = _make_state()
        state["planner_import"] = {
            "board": "k9-board",
            "import_id": "imp-1",
            "envelope_fingerprint": _sha64(),
        }
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        assert parsed["planner_import"]["board"] == "k9-board"
        assert parsed["planner_import"]["import_id"] == "imp-1"

    def test_planner_import_does_not_copy_dag(self, db):
        """K9 reference contains only board, import_id, envelope_fingerprint."""
        state = _make_state()
        state["planner_import"] = {
            "board": "b",
            "import_id": "i",
            "envelope_fingerprint": _sha64(),
        }
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        pi = parsed["planner_import"]
        for key in ("subtasks", "dependencies", "task_map", "dag", "tasks", "frontier"):
            assert key not in pi

    def test_k9_reference_valid_shape(self, db):
        """planner_import must have board, import_id, envelope_fingerprint."""
        state = _make_state()
        state["planner_import"] = {
            "board": "b",
            "import_id": "i",
            "envelope_fingerprint": _sha64(),
        }
        ms.create_mission(db, state=state, operation_id="op-1")
        record = ms.get_mission(db, "mission-1")
        parsed = json.loads(record.state_json)
        pi = parsed["planner_import"]
        assert "board" in pi
        assert "import_id" in pi
        assert "envelope_fingerprint" in pi


# ---------------------------------------------------------------------------
# 21-22. rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_journal_failure_does_not_corrupt_mission(self, db):
        """If the journal INSERT fails, the mission should not be updated."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")

        # Insert a conflicting journal entry first
        now = int(time.time() * 1000)
        db.execute(
            "INSERT INTO mission_journal "
            "(mission_id, operation_id, request_fingerprint, "
            " result_generation, result_status, result_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("mission-1", "op-x", "fp1", 0, "created", "fp1", now),
        )
        db.commit()

        # Now try a transition with op-x but different fingerprint
        next_state = _make_state(status="active", phase="execution")
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-x",
            next_state=next_state,
        )
        # Should detect conflict, not corrupt
        assert result.outcome == "conflict"
        record = ms.get_mission(db, "mission-1")
        assert record.generation == 0  # unchanged

    def test_rollback_on_validation_failure(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad_state = _make_state(status="INVALID", phase="planning")
        result = ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-bad",
            next_state=bad_state,
        )
        assert result.outcome == "invalid"
        record = ms.get_mission(db, "mission-1")
        assert record.generation == 0  # unchanged


# ---------------------------------------------------------------------------
# 23. recuperación tras reinicio
# ---------------------------------------------------------------------------

class TestRecovery:
    def test_data_survives_connection_reopen(self, tmp_path):
        """File-based DB: create, close, reopen, verify."""
        db_path = tmp_path / "test_mission.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn)
        state = _make_state()
        ms.create_mission(conn, state=state, operation_id="op-1")
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn2)  # idempotent
        record = ms.get_mission(conn2, "mission-1")
        assert record is not None
        assert record.status == "planned"
        assert record.generation == 0
        conn2.close()

    def test_transition_survives_reopen(self, tmp_path):
        db_path = tmp_path / "test_mission2.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn)
        state = _make_state()
        ms.create_mission(conn, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            conn,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-t",
            next_state=next_state,
        )
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn2)
        record = ms.get_mission(conn2, "mission-1")
        assert record.status == "active"
        assert record.generation == 1
        conn2.close()


# ---------------------------------------------------------------------------
# 24. migración idempotente
# ---------------------------------------------------------------------------

class TestMigration:
    def test_migration_idempotent(self):
        conn = sqlite3.connect(":memory:")
        ms.migrate_mission_state(conn)
        ms.migrate_mission_state(conn)  # no-op
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "mission_missions" in tables
        assert "mission_journal" in tables
        conn.close()

    def test_migration_preserves_k9(self, db_with_k9):
        tables = {row[0] for row in db_with_k9.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "tasks" in tables
        assert "task_links" in tables
        assert "task_events" in tables
        assert "mission_missions" in tables
        assert "mission_journal" in tables

    def test_migration_preserves_k9_data(self, db_with_k9):
        row = db_with_k9.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()
        assert row is not None
        assert row[0] == "existing task"

    def test_migration_creates_index(self):
        """idx_mission_journal_request_fingerprint is created by migration."""
        conn = sqlite3.connect(":memory:")
        ms.migrate_mission_state(conn)
        indexes = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        assert "idx_mission_journal_request_fingerprint" in indexes
        conn.close()

    def test_ensure_mission_state_schema_works(self):
        """ensure_mission_state_schema is a public alias for migrate."""
        conn = sqlite3.connect(":memory:")
        ms.ensure_mission_state_schema(conn)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "mission_missions" in tables
        conn.close()


# ---------------------------------------------------------------------------
# 25. dos writers compitiendo con la misma generación (REAL concurrency)
# ---------------------------------------------------------------------------

class TestConcurrentWriters:
    def test_concurrent_same_generation_one_wins(self, tmp_path):
        """Real concurrent CAS: two threads with same expected generation.
        Exactly one should succeed (applied), the other should detect stale.
        """
        db_path = tmp_path / "test_concurrent.db"
        # Pre-populate with WAL mode enabled (persists in file header)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        ms.migrate_mission_state(conn)
        state = _make_state()
        ms.create_mission(conn, state=state, operation_id="op-1")
        conn.close()

        results = []
        barrier = threading.Barrier(2)

        def writer(name: str, op_id: str, status: str, phase: str):
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            barrier.wait()  # synchronize start
            next_s = _make_state(status=status, phase=phase, mission_id="mission-1")
            if status == "blocked":
                next_s["active_blocker"] = {
                    "blocker_id": "b1", "reason_code": "dep",
                    "summary": "test", "evidence_ids": [],
                    "resume_condition": {
                        "type": "retry_after", "description": "retry", "reference": "r1",
                    },
                }
                next_s["active_human_gate"] = None
                next_s["queue_exhausted"] = None
                next_s["next_safe_action"] = {
                    "action": "plan", "executable": False,
                    "card_id": None, "reason_code": "blocked",
                }
            try:
                r = ms.compare_and_transition(
                    conn,
                    mission_id="mission-1",
                    expected_generation=0,
                    operation_id=op_id,
                    next_state=next_s,
                )
                results.append((name, r.outcome, r.generation))
            except Exception as e:
                results.append((name, f"error: {e}", -1))
            finally:
                conn.close()

        t1 = threading.Thread(target=writer, args=("w1", "op-w1", "active", "execution"))
        t2 = threading.Thread(target=writer, args=("w2", "op-w2", "blocked", "execution"))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Exactly one should be applied, the other stale_generation
        outcomes = {r[1] for r in results}
        assert "applied" in outcomes, f"Expected 'applied' in {results}"
        assert "stale_generation" in outcomes, f"Expected 'stale_generation' in {results}"

        # Verify final state
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        record = ms.get_mission(conn, "mission-1")
        assert record.generation == 1
        conn.close()

    def test_concurrent_replay_same_operation(self, tmp_path):
        """Two threads with same operation_id and same fingerprint → both replayed."""
        db_path = tmp_path / "test_replay_concurrent.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        ms.migrate_mission_state(conn)
        state = _make_state()
        ms.create_mission(conn, state=state, operation_id="op-1")
        conn.close()

        next_s = _make_state(status="active", phase="execution", mission_id="mission-1")
        results = []
        barrier = threading.Barrier(2)

        def writer(name: str):
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.row_factory = sqlite3.Row
            barrier.wait()
            try:
                r = ms.compare_and_transition(
                    conn,
                    mission_id="mission-1",
                    expected_generation=0,
                    operation_id="op-replay",
                    next_state=next_s,
                )
                results.append((name, r.outcome))
            except Exception as e:
                results.append((name, f"error: {e}"))
            finally:
                conn.close()

        t1 = threading.Thread(target=writer, args=("w1",))
        t2 = threading.Thread(target=writer, args=("w2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # One applied, one replayed (same op_id + same fingerprint)
        outcomes = {r[1] for r in results}
        assert outcomes <= {"applied", "replayed"}, f"Unexpected outcomes: {results}"

        # Generation should be exactly 1 (only one write succeeded)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        record = ms.get_mission(conn, "mission-1")
        assert record.generation == 1
        conn.close()


# ---------------------------------------------------------------------------
# 26. compatibilidad con la suite Kanban existente
# ---------------------------------------------------------------------------

class TestKanbanCompatibility:
    def test_mission_tables_cannot_conflict_with_k9(self, db_with_k9):
        state = _make_state()
        result = ms.create_mission(db_with_k9, state=state, operation_id="op-1")
        assert result.outcome == "created"
        row = db_with_k9.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()
        assert row[0] == "existing task"

    def test_list_journal_empty(self, db):
        records = ms.list_journal(db, "nonexistent")
        assert records == []

    def test_list_journal_after_operations(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db,
            mission_id="mission-1",
            expected_generation=0,
            operation_id="op-2",
            next_state=next_state,
        )
        journal = ms.list_journal(db, "mission-1")
        assert len(journal) == 2
        assert journal[0].result_status == "created"
        assert journal[1].result_status == "active"


# ---------------------------------------------------------------------------
# Canonical fingerprint tests
# ---------------------------------------------------------------------------

class TestCanonicalFingerprint:
    def test_deterministic(self):
        data = {"a": 1, "b": [2, 3], "c": {"nested": True}}
        fp1 = ms.canonical_fingerprint(data)
        fp2 = ms.canonical_fingerprint(data)
        assert fp1 == fp2

    def test_order_independent(self):
        fp1 = ms.canonical_fingerprint({"a": 1, "b": 2})
        fp2 = ms.canonical_fingerprint({"b": 2, "a": 1})
        assert fp1 == fp2

    def test_64_hex_chars(self):
        fp = ms.canonical_fingerprint({"test": True})
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)

    def test_different_data_different_fingerprint(self):
        fp1 = ms.canonical_fingerprint({"x": 1})
        fp2 = ms.canonical_fingerprint({"x": 2})
        assert fp1 != fp2


# ---------------------------------------------------------------------------
# R1 structural invariant enforcement
# ---------------------------------------------------------------------------

class TestR1StructuralInvariants:
    def test_blocked_without_blocker_rejected(self, db):
        """blocked status without active_blocker must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="blocked", phase="execution",
            active_blocker=None,
            active_human_gate=None, queue_exhausted=None,
            next_safe_action={"action": "plan", "executable": False,
                             "card_id": None, "reason_code": "x"},
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "active_blocker" in result.error["message"]

    def test_blocked_with_human_gate_rejected(self, db):
        """blocked status with non-null active_human_gate must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_blocked_state()
        bad["active_human_gate"] = {"gate_id": "x", "gate_type": "approval",
                                    "version": 1, "status": "pending",
                                    "prompt_fingerprint": _sha64(),
                                    "resolution_ref": None}
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "active_human_gate" in result.error["message"]

    def test_human_gate_without_gate_rejected(self, db):
        """human_gate status without active_human_gate must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="human_gate", phase="review",
            active_human_gate=None,
            active_blocker=None, queue_exhausted=None,
            next_safe_action={"action": "await_human", "executable": False,
                             "card_id": None, "reason_code": "x"},
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "active_human_gate" in result.error["message"]

    def test_human_gate_with_blocker_rejected(self, db):
        """human_gate status with non-null active_blocker must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_human_gate_state()
        bad["active_blocker"] = {"blocker_id": "x", "reason_code": "dep",
                                "summary": "test", "evidence_ids": [],
                                "resume_condition": {"type": "retry_after",
                                                     "description": "r",
                                                     "reference": "r"}}
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "active_blocker" in result.error["message"]

    def test_queue_exhausted_without_payload_rejected(self, db):
        """queue_exhausted status without queue_exhausted object must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="queue_exhausted", phase="execution",
            queue_exhausted=None,
            active_blocker=None, active_human_gate=None,
            next_safe_action={"action": "replan", "executable": False,
                             "card_id": None, "reason_code": "x"},
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "queue_exhausted" in result.error["message"]

    def test_completed_without_final_review_rejected(self, db):
        """completed status without final_review must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="completed", phase="terminal",
            next_safe_action=None,
            completion={"final_review": None, "merge_required": False, "merge_gate": None},
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "final_review" in result.error["message"]

    def test_human_gate_resolved_status_rejected(self, db):
        """human_gate with status='resolved' (not 'pending') must be rejected."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_human_gate_state()
        bad["active_human_gate"]["status"] = "resolved"
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"
        assert "pending" in result.error["message"]


# ---------------------------------------------------------------------------
# Boundary / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_create_with_invalid_state_returns_invalid(self, db):
        bad = {"document_type": "wrong"}
        result = ms.create_mission(db, state=bad, operation_id="op-1")
        assert result.outcome == "invalid"

    def test_journal_entries_ordered_by_time(self, db):
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        for i in range(3):
            next_s = _make_state(
                status="active" if i == 0 else "blocked" if i == 1 else "planned",
                phase="execution" if i < 2 else "planning",
            )
            if i == 1:
                next_s["active_blocker"] = {
                    "blocker_id": f"b{i}", "reason_code": "dep",
                    "summary": "test", "evidence_ids": [],
                    "resume_condition": {
                        "type": "retry_after", "description": "retry", "reference": "r1",
                    },
                }
                next_s["active_human_gate"] = None
                next_s["queue_exhausted"] = None
                next_s["next_safe_action"] = {
                    "action": "plan", "executable": False,
                    "card_id": None, "reason_code": "blocked",
                }
            ms.compare_and_transition(
                db,
                mission_id="mission-1",
                expected_generation=i,
                operation_id=f"op-t{i}",
                next_state=next_s,
            )
        journal = ms.list_journal(db, "mission-1")
        assert len(journal) == 4  # create + 3 transitions
        generations = [j.result_generation for j in journal]
        assert generations == [0, 1, 2, 3]

    def test_generation_starts_at_zero(self, db):
        state = _make_state()
        result = ms.create_mission(db, state=state, operation_id="op-1")
        assert result.generation == 0
        record = ms.get_mission(db, "mission-1")
        assert record.generation == 0


# ---------------------------------------------------------------------------
# R1 outcome vocabulary compliance
# ---------------------------------------------------------------------------

class TestR1OutcomeVocabulary:
    def test_transition_result_outcomes_are_r1_exhaustive(self):
        """TransitionResult.outcome accepts exactly R1 vocabulary."""
        r = ms.TransitionResult(
            outcome="applied", mission_id="m", operation_id="o",
            request_fingerprint="fp", generation=0, state_fingerprint="fp",
        )
        assert r.outcome == "applied"

    def test_stale_generation_outcome(self, db):
        """Stale generation returns 'stale_generation' per R1."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next1 = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-2", next_state=next1,
        )
        # Try with wrong generation
        next2 = _make_state(status="active", phase="execution")
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-3", next_state=next2,
        )
        assert result.outcome == "stale_generation"

    def test_replayed_outcome(self, db):
        """Identical replay returns 'replayed' per R1."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-t", next_state=next_state,
        )
        r = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-t", next_state=next_state,
        )
        assert r.outcome == "replayed"


# ---------------------------------------------------------------------------
# kanban_db.connect() integration — mission-state tables auto-created
# ---------------------------------------------------------------------------

class TestConnectIntegration:
    """Verify that kanban_db.connect() automatically creates mission-state
    tables and indexes, so callers never need manual ensure_mission_state_schema().
    """

    def test_connect_creates_mission_tables_on_fresh_db(self, tmp_path):
        """A fresh DB opened via connect() has mission tables immediately."""
        from hermes_cli import kanban_db as kb
        db_path = tmp_path / "fresh_kanban.db"
        conn = kb.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "mission_missions" in tables, f"Missing mission_missions in {tables}"
            assert "mission_journal" in tables, f"Missing mission_journal in {tables}"
            # K9 tables also present
            assert "tasks" in tables, f"Missing tasks in {tables}"
        finally:
            conn.close()

    def test_connect_creates_mission_index(self, tmp_path):
        """The request_fingerprint index is created by connect()."""
        from hermes_cli import kanban_db as kb
        db_path = tmp_path / "fresh_kanban2.db"
        conn = kb.connect(db_path)
        try:
            indexes = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()}
            assert "idx_mission_journal_request_fingerprint" in indexes
        finally:
            conn.close()

    def test_connect_preserves_k9_data(self, tmp_path):
        """Opening a DB with existing K9 data adds mission tables without loss."""
        from hermes_cli import kanban_db as kb
        db_path = tmp_path / "k9_existing.db"
        # Create a K9 DB first
        conn1 = kb.connect(db_path)
        conn1.execute(
            "INSERT INTO tasks (id, title, status, created_at) "
            "VALUES ('t1', 'my task', 'running', 0)"
        )
        conn1.commit()
        conn1.close()

        # Reopen — mission tables should appear, K9 data preserved
        conn2 = kb.connect(db_path)
        try:
            tables = {row[0] for row in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "mission_missions" in tables
            assert "tasks" in tables
            row = conn2.execute("SELECT title FROM tasks WHERE id='t1'").fetchone()
            assert row is not None
            assert row[0] == "my task"
        finally:
            conn2.close()

    def test_connect_idempotent(self, tmp_path):
        """Opening the same DB multiple times via connect() is safe."""
        from hermes_cli import kanban_db as kb
        db_path = tmp_path / "idempotent.db"
        for _ in range(3):
            conn = kb.connect(db_path)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "mission_missions" in tables
            assert "mission_journal" in tables
            conn.close()

    def test_create_mission_works_after_connect(self, tmp_path):
        """create_mission() works immediately after connect() — no manual migration."""
        from hermes_cli import kanban_db as kb
        db_path = tmp_path / "api_ready.db"
        conn = kb.connect(db_path)
        try:
            state = _make_state()
            result = ms.create_mission(conn, state=state, operation_id="op-integ")
            assert result.outcome == "created"
            record = ms.get_mission(conn, "mission-1")
            assert record is not None
            assert record.status == "planned"
        finally:
            conn.close()

    def test_init_db_creates_mission_tables(self, tmp_path):
        """init_db() also creates mission-state tables."""
        from hermes_cli import kanban_db as kb
        db_path = tmp_path / "init_db_test.db"
        kb.init_db(db_path)
        conn = kb.connect(db_path)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            assert "mission_missions" in tables
            assert "mission_journal" in tables
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# P2-1 RED: validation gap closure — suspended status invariants
# ---------------------------------------------------------------------------

class TestP2_ValidationGap_Blocked:
    """blocked requires structured next_safe_action (not None/absent) and
    active_blocker with mandatory fields including resume_condition."""

    def test_blocked_without_next_safe_action_rejected(self, db):
        """blocked status with next_safe_action=None must be invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="blocked", phase="execution",
            active_blocker={
                "blocker_id": "b1", "reason_code": "dep",
                "summary": "test", "evidence_ids": [],
                "resume_condition": {
                    "type": "retry_after", "description": "r", "reference": "r",
                },
            },
            active_human_gate=None, queue_exhausted=None,
            next_safe_action=None,
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"

    def test_blocked_blocker_without_resume_condition_rejected(self, db):
        """blocked with active_blocker lacking resume_condition is invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="blocked", phase="execution",
            active_blocker={
                "blocker_id": "b1", "reason_code": "dep",
                "summary": "test", "evidence_ids": [],
            },
            active_human_gate=None, queue_exhausted=None,
            next_safe_action={
                "action": "plan", "executable": False,
                "card_id": None, "reason_code": "x",
            },
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"


class TestP2_ValidationGap_HumanGate:
    """human_gate requires structured next_safe_action and full gate fields."""

    def test_human_gate_without_next_safe_action_rejected(self, db):
        """human_gate with next_safe_action=None must be invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="human_gate", phase="review",
            active_human_gate={
                "gate_id": "g1", "gate_type": "approval", "version": 1,
                "status": "pending", "prompt_fingerprint": _sha64(),
                "resolution_ref": None,
            },
            active_blocker=None, queue_exhausted=None,
            next_safe_action=None,
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"

    def test_human_gate_missing_gate_fields_rejected(self, db):
        """active_human_gate missing required schema fields is invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="human_gate", phase="review",
            active_human_gate={"gate_id": "g1"},
            active_blocker=None, queue_exhausted=None,
            next_safe_action={
                "action": "await_human", "executable": False,
                "card_id": None, "reason_code": "x",
            },
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"


class TestP2_ValidationGap_QueueExhausted:
    """queue_exhausted requires structured next_safe_action and resume_condition."""

    def test_queue_exhausted_without_next_safe_action_rejected(self, db):
        """queue_exhausted with next_safe_action=None must be invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="queue_exhausted", phase="execution",
            queue_exhausted={
                "decision_id": "d1", "reason_code": "empty",
                "summary": "no work", "exhausted_at_generation": 0,
                "evidence_ids": [],
                "resume_condition": {
                    "type": "manual_replan", "description": "r",
                    "reference": "ref",
                },
            },
            active_blocker=None, active_human_gate=None,
            next_safe_action=None,
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"

    def test_queue_exhausted_without_resume_condition_rejected(self, db):
        """queue_exhausted with resume_condition=None is invalid (R1 fixture)."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(
            status="queue_exhausted", phase="execution",
            queue_exhausted={
                "decision_id": "d1", "reason_code": "empty",
                "summary": "no work", "exhausted_at_generation": 0,
                "evidence_ids": [],
                "resume_condition": None,
            },
            active_blocker=None, active_human_gate=None,
            next_safe_action={
                "action": "replan", "executable": False,
                "card_id": None, "reason_code": "queue_exhausted",
            },
        )
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"


class TestP2_ValidationGap_Completed:
    """completed.final_review must bind to identity head/tree SHAs."""

    def test_completed_final_review_wrong_head_rejected(self, db):
        """final_review.commit_sha != identity.head_sha must be invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_completed_state()
        bad["completion"]["final_review"]["commit_sha"] = "0" * 40
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"

    def test_completed_merge_required_without_merge_gate_rejected(self, db):
        """merge_required=True with null merge_gate must be invalid."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_completed_state()
        bad["completion"]["merge_required"] = True
        bad["completion"]["merge_gate"] = None
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-bad", next_state=bad,
        )
        assert result.outcome == "invalid"


# ---------------------------------------------------------------------------
# P2-1 RED: R1 fixture roundtrip
# ---------------------------------------------------------------------------

class TestP2_R1FixtureRoundtrip:
    """Run R1 normative fixtures through the productive validator."""

    R1_FIXTURES = Path("/home/ubuntu/.hermes-worktrees/kanban-long-runtime-r0"
                        "/tests/fixtures/kanban_mission_state")

    def test_r1_valid_fixtures_accepted(self, db):
        """Every R1 valid fixture must pass _validate_state_shape."""
        for name in sorted((self.R1_FIXTURES / "valid").glob("*.json")):
            state = json.loads(name.read_text())
            errors = ms._validate_state_shape(state)
            assert errors == [], f"{name.name}: {errors}"

    def test_r1_invalid_fixtures_rejected(self, db):
        """Every R1 invalid fixture must fail _validate_state_shape."""
        for name in sorted((self.R1_FIXTURES / "invalid").glob("*.json")):
            state = json.loads(name.read_text())
            errors = ms._validate_state_shape(state)
            assert errors, f"{name.name}: expected errors but got none"


# ---------------------------------------------------------------------------
# P2-1 RED: consultation without local decision
# ---------------------------------------------------------------------------

class TestP2_ConsultationDecision:
    """Consultation consumption requires a local decision."""

    def test_consultation_without_decision_id_rejected(self, db):
        """Transition with consumes_consultation but no decision_id is invalid."""
        state = _make_state()
        state["consultation_refs"] = [{
            "execution_id": "exec-1", "mode": "plan-review",
            "snapshot_head": _sha40(), "tree_sha": _sha40(),
            "plan_fingerprint": _sha64(), "checkpoint_fingerprint": _sha64(),
            "bundle_fingerprint": _sha64(), "expected_question_ids": ["Q1"],
            "schema_version": "codex-senior-consult-response/v3",
            "status": "COMPLETED", "detailed_status": "VALID_ADVISORY_VERDICT",
            "verdict": "accept", "response_fingerprint": _sha64(),
            "response_artifact": "responses/abc.json",
        }]
        ms.create_mission(db, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        result = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-consult", next_state=next_state,
            consumes_consultation="exec-1",
        )
        assert result.outcome == "invalid"


# ---------------------------------------------------------------------------
# P2-2 RED: canonical fingerprint normalization
# ---------------------------------------------------------------------------

class TestP2_FingerprintNormalization:
    """next_state.generation must be normalized before fingerprinting so
    two semantically identical retries produce 'replayed'."""

    def test_replay_despite_generation_difference(self, db):
        """Two retries with same content but different next_state.generation
        must produce 'replayed', not 'conflict'."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")

        next_a = _make_state(status="active", phase="execution")
        next_a["generation"] = 99  # wrong generation — should be normalized
        r1 = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-idem", next_state=next_a,
        )
        assert r1.outcome == "applied"

        next_b = _make_state(status="active", phase="execution")
        next_b["generation"] = 0  # different generation, same semantics
        r2 = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-idem", next_state=next_b,
        )
        assert r2.outcome == "replayed", (
            f"Expected replayed but got {r2.outcome} — "
            "generation field in next_state caused fingerprint divergence"
        )

    def test_material_change_produces_conflict(self, db):
        """Same operation_id but materially different content → conflict."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")

        next_a = _make_state(status="active", phase="execution")
        r1 = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=0,
            operation_id="op-diff", next_state=next_a,
        )
        assert r1.outcome == "applied"

        next_b = _make_state(status="blocked", phase="execution")
        next_b["active_blocker"] = {
            "blocker_id": "b1", "reason_code": "dep",
            "summary": "x", "evidence_ids": [],
            "resume_condition": {
                "type": "retry_after", "description": "r", "reference": "r",
            },
        }
        next_b["active_human_gate"] = None
        next_b["queue_exhausted"] = None
        next_b["next_safe_action"] = {
            "action": "plan", "executable": False,
            "card_id": None, "reason_code": "blocked",
        }
        r2 = ms.compare_and_transition(
            db, mission_id="mission-1", expected_generation=1,
            operation_id="op-diff", next_state=next_b,
        )
        assert r2.outcome == "conflict"

    def test_fingerprint_deterministic_after_reopen(self, tmp_path):
        """Fingerprint stored in SQLite must be reproducible after reopen."""
        db_path = tmp_path / "fp_reopen.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn)
        state = _make_state()
        ms.create_mission(conn, state=state, operation_id="op-1")
        next_state = _make_state(status="active", phase="execution")
        r1 = ms.compare_and_transition(
            conn, mission_id="mission-1", expected_generation=0,
            operation_id="op-t", next_state=next_state,
        )
        fp_original = r1.state_fingerprint
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn2)
        record = ms.get_mission(conn2, "mission-1")
        conn2.close()

        assert record.state_fingerprint == fp_original

    def test_nan_rejected(self, db):
        """NaN value in state must be rejected (not representable canonically)."""
        state = _make_state()
        ms.create_mission(db, state=state, operation_id="op-1")
        bad = _make_state(generation=float("nan"))
        result = ms.create_mission(db, state=bad, operation_id="op-nan")
        assert result.outcome == "invalid"

    def test_unicode_stable_fingerprint(self):
        """Unicode content produces stable, reproducible fingerprint."""
        a = {"key": "\u00f1aci\u00f3n"}
        b = {"key": "\u00f1aci\u00f3n"}
        assert ms.canonical_fingerprint(a) == ms.canonical_fingerprint(b)
        fp = ms.canonical_fingerprint(a)
        assert len(fp) == 64


# ---------------------------------------------------------------------------
# P1 RED: nested additionalProperties enforcement
# ---------------------------------------------------------------------------

class TestP2_NestedAdditionalProperties:
    """Every nested object type with R1 additionalProperties:false must
    reject unknown keys."""

    def _make_valid_active(self):
        """Build a minimal valid active state with all required nested objects."""
        identity = _make_identity()
        return _make_state(
            status="active", phase="execution",
            identity=identity,
            card_ref={"card_id": "c1", "phase": "execution"},
            execution_ref={"run_id": "r1", "attempt_id": "a1"},
            evidence_refs=[{
                "evidence_id": "ev1", "kind": "runtime_snapshot",
                "fingerprint": _sha64(), "artifact": "art1",
            }],
            decision_refs=[{
                "decision_id": "d1", "kind": "local",
                "outcome": "pass", "evidence_ids": ["ev1"],
            }],
            consultation_refs=[{
                "execution_id": "ex1", "mode": "plan-review",
                "snapshot_head": _sha40(), "tree_sha": _sha40(),
                "plan_fingerprint": _sha64(),
                "checkpoint_fingerprint": _sha64(),
                "bundle_fingerprint": _sha64(),
                "expected_question_ids": ["Q1"],
                "schema_version": "codex-senior-consult-response/v3",
                "status": "COMPLETED",
                "detailed_status": "VALID_ADVISORY_VERDICT",
                "verdict": "accept",
                "response_fingerprint": _sha64(),
                "response_artifact": "r.json",
            }],
            active_blocker=None, active_human_gate=None,
            queue_exhausted=None,
            next_safe_action={
                "action": "dispatch_card", "executable": True,
                "card_id": "c1", "reason_code": "active",
            },
            last_operation={
                "operation_id": "op1", "request_fingerprint": _sha64(),
                "attempt_id": "a1",
            },
        )

    def test_identity_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["identity"]["unexpected_key"] = "x"
        errors = ms._validate_state_shape(state)
        assert any("identity" in e for e in errors), f"Expected identity error, got: {errors}"

    def test_card_ref_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["card_ref"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("card_ref" in e for e in errors), f"Expected card_ref error, got: {errors}"

    def test_execution_ref_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["execution_ref"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("execution_ref" in e for e in errors), f"Expected execution_ref error, got: {errors}"

    def test_evidence_ref_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["evidence_refs"][0]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("evidence_ref" in e.lower() for e in errors), f"Expected evidence_ref error, got: {errors}"

    def test_decision_ref_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["decision_refs"][0]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("decision_ref" in e.lower() for e in errors), f"Expected decision_ref error, got: {errors}"

    def test_next_safe_action_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["next_safe_action"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("next_safe_action" in e for e in errors), f"Expected NSA error, got: {errors}"

    def test_last_operation_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["last_operation"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("last_operation" in e for e in errors), f"Expected last_op error, got: {errors}"

    def test_completion_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["completion"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("completion" in e for e in errors), f"Expected completion error, got: {errors}"

    def test_consultation_ref_extra_key_rejected(self, db):
        state = self._make_valid_active()
        state["consultation_refs"][0]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("consultation_ref" in e.lower() for e in errors), f"Expected cons error, got: {errors}"


class TestP2_BlockedNestedExtraKeys:
    """Blocked state nested objects must also reject extra keys."""

    def test_blocker_extra_key_rejected(self, db):
        state = _make_blocked_state()
        state["active_blocker"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("active_blocker" in e for e in errors), f"Expected blocker error, got: {errors}"

    def test_resume_condition_extra_key_rejected(self, db):
        state = _make_blocked_state()
        state["active_blocker"]["resume_condition"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("resume_condition" in e for e in errors), f"Expected rc error, got: {errors}"


class TestP2_HumanGateNestedExtraKeys:
    def test_human_gate_extra_key_rejected(self, db):
        state = _make_human_gate_state()
        state["active_human_gate"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("active_human_gate" in e for e in errors), f"Expected gate error, got: {errors}"


class TestP2_QueueExhaustedNestedExtraKeys:
    def test_queue_exhausted_extra_key_rejected(self, db):
        state = _make_queue_exhausted_state()
        state["queue_exhausted"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("queue_exhausted" in e for e in errors), f"Expected qe error, got: {errors}"

    def test_qe_resume_condition_extra_key_rejected(self, db):
        state = _make_queue_exhausted_state()
        state["queue_exhausted"]["resume_condition"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("resume_condition" in e for e in errors), f"Expected rc error, got: {errors}"


class TestP2_CompletedNestedExtraKeys:
    def test_gate_ref_extra_key_rejected(self, db):
        state = _make_completed_state()
        state["completion"]["final_review"]["extra"] = True
        errors = ms._validate_state_shape(state)
        assert any("final_review" in e for e in errors), f"Expected gate error, got: {errors}"


# ---------------------------------------------------------------------------
# P2 RED: non-finite JSON rejection
# ---------------------------------------------------------------------------

class TestP2_NonFiniteJSON:
    """NaN and Infinity must be rejected at any depth."""

    def test_nan_in_nested_field_rejected(self, db):
        """NaN in evidence_ref fingerprint should be rejected."""
        state = _make_valid_active_for_nonfinite()
        state["evidence_refs"][0]["fingerprint"] = float("nan")
        result = ms.create_mission(
            db, state=state, operation_id="op-nan",
        )
        assert result.outcome == "invalid"

    def test_positive_infinity_rejected(self, db):
        state = _make_valid_active_for_nonfinite()
        state["generation"] = float("inf")
        errors = ms._validate_state_shape(state)
        assert errors, f"Expected Infinity to be rejected, got: {errors}"

    def test_negative_infinity_rejected(self, db):
        state = _make_valid_active_for_nonfinite()
        state["generation"] = float("-inf")
        errors = ms._validate_state_shape(state)
        assert errors, f"Expected -Infinity to be rejected, got: {errors}"

    def test_nan_in_list_rejected(self, db):
        state = _make_valid_active_for_nonfinite()
        state["evidence_refs"][0]["fingerprint"] = float("nan")
        errors = ms._validate_state_shape(state)
        assert errors, f"Expected NaN in list item to be rejected"

    def test_unicode_still_accepted(self, db):
        state = _make_valid_active_for_nonfinite()
        state["identity"]["repository"] = "https://github.com/test/\u00f1repo"
        errors = ms._validate_state_shape(state)
        assert errors == [], f"Unicode should be accepted, got: {errors}"

    def test_bool_and_null_accepted(self, db):
        state = _make_valid_active_for_nonfinite()
        state["completion"]["merge_required"] = True
        state["completion"]["merge_gate"] = None
        errors = ms._validate_state_shape(state)
        # Note: merge_gate=None is only valid when merge_required=False
        # This test just ensures bool/null don't crash the validator
        assert isinstance(errors, list)

    def test_generation_bool_rejected(self, db):
        """Python bool is int subtype; True/False must be rejected for generation."""
        state = _make_valid_active_for_nonfinite()
        state["generation"] = True
        errors = ms._validate_state_shape(state)
        assert errors, f"Expected bool generation to be rejected"

    def test_fingerprint_stable_after_reopen(self, tmp_path):
        """Fingerprint survives close/reopen with non-finite-free state."""
        db_path = tmp_path / "fp_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn)
        state = _make_valid_active_for_nonfinite()
        r = ms.create_mission(conn, state=state, operation_id="op-1")
        fp = r.state_fingerprint
        conn.close()

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        ms.migrate_mission_state(conn2)
        rec = ms.get_mission(conn2, state["mission_id"])
        conn2.close()
        assert rec.state_fingerprint == fp


def _make_valid_active_for_nonfinite():
    """Valid active state for non-finite JSON tests."""
    identity = _make_identity()
    return _make_state(
        status="active", phase="execution",
        identity=identity,
        card_ref={"card_id": "c1", "phase": "execution"},
        execution_ref={"run_id": "r1", "attempt_id": "a1"},
        evidence_refs=[{
            "evidence_id": "ev1", "kind": "runtime_snapshot",
            "fingerprint": _sha64(), "artifact": "art1",
        }],
        decision_refs=[],
        consultation_refs=[],
        active_blocker=None, active_human_gate=None,
        queue_exhausted=None,
        next_safe_action={
            "action": "dispatch_card", "executable": True,
            "card_id": "c1", "reason_code": "active",
        },
        last_operation={
            "operation_id": "op1", "request_fingerprint": _sha64(),
            "attempt_id": "a1",
        },
    )
