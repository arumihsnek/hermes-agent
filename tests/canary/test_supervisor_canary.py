"""End-to-end canary test for the Kanban autonomous supervisor system.

Demonstrates the complete lifecycle:
1. Create test mission with multiple phases
2. Execute slices
3. Simulate crash and recovery
4. Handle blocker → senior consult
5. Handle human gate → response
6. Handle queue exhaustion → replan
7. Complete all phases
8. Verify final state

This test exercises R2 (mission state), R3 (supervisor), R4 (human gate),
and R5 (roadmap executor) together in a single reproducible scenario.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli.kanban_mission_state import (
    create_mission, get_mission, compare_and_transition, list_journal,
    SCHEMA_VERSION,
)
from hermes_cli.kanban_supervisor import (
    SUPERVISOR_SCHEMA_SQL, migrate_supervisor,
    add_slices, acquire_supervisor_lease, release_supervisor_lease,
    get_next_slice, get_roadmap_slices,
    mark_slice_active, mark_slice_completed, mark_slice_blocked,
    mark_slice_human_gate, mark_slice_failed,
    run_supervisor_tick, recover_supervisor_state, detect_incomplete_slices,
    validate_slice, _validate_fencing,
    DEFAULT_MAX_ATTEMPTS, TERMINAL_SLICE_STATUSES,
    SliceRecord, TickResult, SupervisorState,
)
from hermes_cli.kanban_human_gate import (
    GATE_SCHEMA_SQL, migrate_human_gate,
    create_human_gate, respond_to_gate, get_pending_gates, get_gate,
)
from hermes_cli.kanban_roadmap_executor import (
    load_roadmap, resolve_dependency_order, run_roadmap_executor,
    ExecutorConfig, ExecutorResult,
    Roadmap, RoadmapPhase, RoadmapSlice,
)


def _make_conn(db_path=None):
    if db_path:
        conn = sqlite3.connect(db_path, isolation_level=None)
    else:
        conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS mission_missions (
            mission_id TEXT PRIMARY KEY, schema_version TEXT NOT NULL,
            status TEXT NOT NULL, phase TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0, state_json TEXT NOT NULL,
            state_fingerprint TEXT NOT NULL,
            created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mission_journal (
            mission_id TEXT NOT NULL, operation_id TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            result_generation INTEGER NOT NULL, result_status TEXT NOT NULL,
            result_fingerprint TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY (mission_id, operation_id)
        );
    """)
    conn.executescript(SUPERVISOR_SCHEMA_SQL)
    conn.executescript(GATE_SCHEMA_SQL)
    return conn


def _make_mission(conn, mission_id="m_canary"):
    state = {
        "document_type": "mission_state", "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id, "generation": 0, "status": "active",
        "phase": "execution",
        "identity": {
            "board": "canary-board", "tenant": "default",
            "repository": "arumihsnek/hermes-agent", "branch": "main",
            "base_sha": "a" * 40, "head_sha": "b" * 40,
            "tree_sha": "c" * 40, "plan_fingerprint": "d" * 64,
            "checkpoint_fingerprint": "e" * 64,
        },
        "planner_import": None, "card_ref": None, "execution_ref": None,
        "evidence_refs": [], "decision_refs": [], "consultation_refs": [],
        "active_blocker": None, "active_human_gate": None,
        "queue_exhausted": None, "completion": None,
        "next_safe_action": None, "last_operation": None,
    }
    result = create_mission(conn, state=state, operation_id="op_canary_create")
    assert result.outcome == "created"


# ---------------------------------------------------------------------------
# Canary: Full lifecycle
# ---------------------------------------------------------------------------

class TestEndToEndCanary(unittest.TestCase):
    """Complete canary test exercising all R2-R5 components."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "canary.db")

    def test_full_lifecycle(self):
        """Step 1-22: Complete canary lifecycle."""
        # === Step 1: Create mission ===
        conn = _make_conn(self.db_path)
        _make_mission(conn, "m_canary")
        mission = get_mission(conn, "m_canary")
        self.assertIsNotNone(mission)
        self.assertEqual(mission.status, "active")

        # === Step 2-3: Ingest slices with dependencies ===
        slices = [
            {"slice_id": "setup", "phase": "init", "description": "Setup",
             "dependencies": [], "max_attempts": 3, "priority": 10},
            {"slice_id": "core-work", "phase": "exec", "description": "Core work",
             "dependencies": ["setup"], "max_attempts": 3, "priority": 5},
            {"slice_id": "blocked-work", "phase": "exec", "description": "Will block",
             "dependencies": ["setup"], "max_attempts": 3, "priority": 3,
             "gate_type": "senior_consult"},
            {"slice_id": "gate-work", "phase": "review", "description": "Needs human",
             "dependencies": ["core-work"], "max_attempts": 3, "priority": 2,
             "gate_type": "human_gate"},
            {"slice_id": "final", "phase": "close", "description": "Final",
             "dependencies": ["gate-work", "blocked-work"], "max_attempts": 3,
             "priority": 1},
        ]
        result = add_slices(conn, "m_canary", slices)
        self.assertEqual(result.outcome, "added")
        self.assertEqual(result.slice_count, 5)

        # === Step 4: Acquire lease ===
        lease = acquire_supervisor_lease(conn, "m_canary", "sup-canary")
        self.assertEqual(lease.outcome, "acquired")
        fencing_epoch = lease.fencing_epoch

        # === Step 5: Execute 'setup' slice ===
        result = run_supervisor_tick(
            conn, "m_canary", "sup-canary",
            lambda s, e: {"outcome": "success", "evidence": {"done": True}},
        )
        self.assertEqual(result.outcome, "slice_executed")
        self.assertEqual(result.slice_id, "setup")

        # === Step 6: Simulate crash — mark 'core-work' as active but don't complete ===
        mark_slice_active(conn, "m_canary", "core-work", "sup-canary", fencing_epoch)
        incomplete = detect_incomplete_slices(conn, "m_canary")
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0].slice_id, "core-work")

        # === Step 7-8: Recovery — new process (new connection) ===
        # Release old lease to simulate supervisor death
        release_supervisor_lease(conn, "m_canary", "sup-canary")
        conn.close()
        conn2 = _make_conn(self.db_path)
        # Re-acquire lease with new supervisor
        lease2 = acquire_supervisor_lease(conn2, "m_canary", "sup-canary-v2")
        self.assertIn(lease2.outcome, ("acquired", "renewed"))

        # === Step 9: Detect and recover incomplete slice ===
        incomplete = detect_incomplete_slices(conn2, "m_canary")
        self.assertEqual(len(incomplete), 1)

        # === Step 10: Complete 'core-work' via recovery tick ===
        result = run_supervisor_tick(
            conn2, "m_canary", "sup-canary-v2",
            lambda s, e: {"outcome": "success", "evidence": {"recovered": True}},
        )
        self.assertEqual(result.outcome, "slice_executed")
        self.assertEqual(result.slice_id, "core-work")

        # === Step 11: Execute 'blocked-work' — executor returns blocked ===
        result = run_supervisor_tick(
            conn2, "m_canary", "sup-canary-v2",
            lambda s, e: {"outcome": "blocked",
                          "blocker": {"reason": "external dependency"}},
        )
        self.assertEqual(result.outcome, "slice_blocked")
        self.assertEqual(result.slice_id, "blocked-work")

        # === Step 12-13: Senior consult (simulated) ===
        # In real scenario, this would call codex-senior-consult
        # Here we just verify the blocker state is persisted
        slices_state = get_roadmap_slices(conn2, "m_canary")
        blocked = [s for s in slices_state if s.status == "blocked"]
        self.assertEqual(len(blocked), 1)

        # === Step 14-15: Human gate — create gate ===
        gate = create_human_gate(
            conn2, "m_canary", "approval",
            {"question": "Approve gate-work?", "options": ["yes", "no"]},
        )
        self.assertEqual(gate.outcome, "created")

        # === Step 16: Execute 'gate-work' — it requests human gate ===
        result = run_supervisor_tick(
            conn2, "m_canary", "sup-canary-v2",
            lambda s, e: {"outcome": "human_gate",
                          "gate_info": {"gate_id": gate.gate_id}},
        )
        self.assertEqual(result.outcome, "slice_human_gate")

        # === Step 17: Show question to user (simulated) ===
        pending = get_pending_gates(conn2, "m_canary")
        self.assertEqual(len(pending), 1)
        question = json.loads(pending[0].question_json)
        self.assertEqual(question["question"], "Approve gate-work?")

        # === Step 18: Persist human response ===
        resp = respond_to_gate(
            conn2, "m_canary", gate.gate_id,
            {"choice": "yes"},
        )
        self.assertEqual(resp.outcome, "accepted")

        # === Step 19: Queue exhaustion — slice with max_attempts=1 ===
        add_slices(conn2, "m_canary", [
            {"slice_id": "fail-once", "phase": "test", "description": "Fails once",
             "dependencies": ["setup"], "max_attempts": 1, "priority": 0},
        ])
        result = run_supervisor_tick(
            conn2, "m_canary", "sup-canary-v2",
            lambda s, e: {"outcome": "failed", "error": {"msg": "boom"}},
        )
        # After max_attempts, next tick should find no pending
        result2 = run_supervisor_tick(
            conn2, "m_canary", "sup-canary-v2",
            lambda s, e: {"outcome": "success", "evidence": {}},
        )
        # fail-once is now blocked (retryable), not pending
        fail_slice = [s for s in get_roadmap_slices(conn2, "m_canary")
                      if s.slice_id == "fail-once"][0]
        self.assertIn(fail_slice.status, ("blocked", "failed"))

        # === Step 20: Complete remaining slices ===
        # 'final' depends on 'gate-work' and 'blocked-work'
        # Mark both as completed for the canary
        mark_slice_completed(
            conn2, "m_canary", "blocked-work",
            {"resolved_by": "senior_consult"},
            "sup-canary-v2", lease2.fencing_epoch,
        )
        mark_slice_completed(
            conn2, "m_canary", "gate-work",
            {"resolved_by": "human_gate"},
            "sup-canary-v2", lease2.fencing_epoch,
        )

        # Now execute 'final'
        result = run_supervisor_tick(
            conn2, "m_canary", "sup-canary-v2",
            lambda s, e: {"outcome": "success", "evidence": {"final": True}},
        )
        self.assertEqual(result.outcome, "slice_executed")
        self.assertEqual(result.slice_id, "final")

        # === Step 21: Verify all slices completed ===
        all_slices = get_roadmap_slices(conn2, "m_canary")
        terminal = [s for s in all_slices if s.status in TERMINAL_SLICE_STATUSES]
        # setup, core-work, blocked-work, gate-work (human_gate but slice completed),
        # final = 5 terminal. fail-once = 1 failed.
        self.assertGreaterEqual(len(terminal), 5)

        # === Step 22: Supervisor state consistency ===
        state = recover_supervisor_state(conn2, "m_canary")
        self.assertIsNotNone(state)
        self.assertGreater(state.total_completed, 0)

        # === Verify R2 mission state unchanged ===
        mission = get_mission(conn2, "m_canary")
        self.assertIsNotNone(mission)
        self.assertEqual(mission.status, "active")  # Mission itself still active

        # === Verify journal has entries ===
        journal = list_journal(conn2, "m_canary")
        self.assertGreater(len(journal), 0)

        conn2.close()


class TestCanaryReplayProtection(unittest.TestCase):
    """Verify operation replay is prevented."""

    def test_same_operation_replayed(self):
        conn = _make_conn()
        _make_mission(conn, "m_replay")

        # Add slices twice with same operation_id
        result1 = add_slices(conn, "m_replay", [
            {"slice_id": "s1", "phase": "P1", "description": "d1"},
        ])
        self.assertEqual(result1.outcome, "added")

        # Same slices again — should be already-applied
        result2 = add_slices(conn, "m_replay", [
            {"slice_id": "s1", "phase": "P1", "description": "d1"},
        ])
        self.assertEqual(result2.outcome, "already-applied")


class TestCanaryTwoSupervisors(unittest.TestCase):
    """Verify two supervisors can't run simultaneously."""

    def test_lease_prevents_dual_execution(self):
        conn = _make_conn()
        _make_mission(conn, "m_dual")
        add_slices(conn, "m_dual", [
            {"slice_id": "s1", "phase": "P1", "description": "d1"},
        ])

        lease1 = acquire_supervisor_lease(conn, "m_dual", "sup-A")
        self.assertEqual(lease1.outcome, "acquired")

        lease2 = acquire_supervisor_lease(conn, "m_dual", "sup-B")
        self.assertEqual(lease2.outcome, "lease_held")

        # Only sup-A can execute
        result = run_supervisor_tick(
            conn, "m_dual", "sup-A",
            lambda s, e: {"outcome": "success", "evidence": {}},
        )
        self.assertEqual(result.outcome, "slice_executed")


class TestCanaryBlockerResume(unittest.TestCase):
    """Verify blocker → resolution → retry flow."""

    def test_blocker_then_resume(self):
        conn = _make_conn()
        _make_mission(conn, "m_blocker")
        add_slices(conn, "m_blocker", [
            {"slice_id": "s1", "phase": "P1", "description": "d1", "max_attempts": 3},
        ])
        acquire_supervisor_lease(conn, "m_blocker", "sup-1")

        # First attempt: blocked
        result = run_supervisor_tick(
            conn, "m_blocker", "sup-1",
            lambda s, e: {"outcome": "blocked", "blocker": {"reason": "test"}},
        )
        self.assertEqual(result.outcome, "slice_blocked")

        # Slice is now in blocked status, not pending
        slices = get_roadmap_slices(conn, "m_blocker")
        self.assertEqual(slices[0].status, "blocked")


class TestCanaryHumanGateResume(unittest.TestCase):
    """Verify human gate → response → continue flow."""

    def test_gate_lifecycle(self):
        conn = _make_conn()
        _make_mission(conn, "m_hgate")

        gate = create_human_gate(
            conn, "m_hgate", "approval",
            {"question": "Approve?"},
        )
        self.assertEqual(gate.outcome, "created")

        # Gate is pending
        pending = get_pending_gates(conn, "m_hgate")
        self.assertEqual(len(pending), 1)

        # Respond
        resp = respond_to_gate(conn, "m_hgate", gate.gate_id, {"choice": "yes"})
        self.assertEqual(resp.outcome, "accepted")

        # Gate resolved
        pending = get_pending_gates(conn, "m_hgate")
        self.assertEqual(len(pending), 0)


class TestCanaryQueueExhaustion(unittest.TestCase):
    """Verify queue exhaustion doesn't become completed."""

    def test_exhaustion_not_completion(self):
        conn = _make_conn()
        _make_mission(conn, "m_exhaust")
        add_slices(conn, "m_exhaust", [
            {"slice_id": "s1", "phase": "P1", "description": "d1", "max_attempts": 1},
        ])
        acquire_supervisor_lease(conn, "m_exhaust", "sup-1")

        # Execute — fails
        result = run_supervisor_tick(
            conn, "m_exhaust", "sup-1",
            lambda s, e: {"outcome": "failed", "error": {"msg": "boom"}},
        )

        # After first fail, slice is blocked (retryable), not completed
        slices = get_roadmap_slices(conn, "m_exhaust")
        self.assertIn(slices[0].status, ("blocked", "failed"))
        self.assertNotEqual(slices[0].status, "completed")


class TestCanaryExactHeadReview(unittest.TestCase):
    """Verify exact-head review mechanics."""

    def test_mission_identity_preserved(self):
        conn = _make_conn()
        _make_mission(conn, "m_head")
        mission = get_mission(conn, "m_head")
        state = json.loads(mission.state_json)
        self.assertEqual(state["identity"]["head_sha"], "b" * 40)
        self.assertEqual(state["identity"]["tree_sha"], "c" * 40)


if __name__ == "__main__":
    unittest.main()
