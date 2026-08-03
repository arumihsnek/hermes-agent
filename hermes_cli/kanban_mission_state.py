"""Durable mission-state backend for Kanban long-running missions.

Provides SQLite-backed persistent storage for mission lifecycle state,
an idempotent operation journal, and a typed Python API for creating,
reading, and atomically transitioning missions.

Schema version: ``kanban-mission-state/v1`` (matches R1 contract).

This module sits alongside the existing K9 kanban tables (tasks,
task_links, etc.) in the same SQLite database.  It adds two new tables
(``mission_missions`` and ``mission_journal``) via idempotent
``CREATE TABLE IF NOT EXISTS`` and never modifies or removes K9 tables.

Concurrency model: WAL mode + ``BEGIN IMMEDIATE`` for every write
transaction, matching the proven pattern in ``kanban_db.py``.

Ownership
---------
- Tables, migration, and API live here.
- K9 tables remain owned by ``kanban_db.py``.
- Schema/policy/fixtures live in the R1 contract repository.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "kanban-mission-state/v1"
DOCUMENT_TYPE_STATE = "mission_state"
DOCUMENT_TYPE_TRANSITION = "mission_transition"
DOCUMENT_TYPE_RESULT = "mission_transition_result"

VALID_STATUSES = frozenset({
    "planned", "active", "blocked", "human_gate",
    "queue_exhausted", "failed", "completed",
})

VALID_PHASES = frozenset({
    "planning", "execution", "review", "correction", "closing", "terminal",
})

TERMINAL_STATUSES = frozenset({"failed", "completed"})
SUSPENDED_STATUSES = frozenset({"blocked", "human_gate", "queue_exhausted"})

# R1 mission_state allowed top-level properties (additionalProperties: false)
_VALID_STATE_KEYS = frozenset({
    "document_type", "schema_version", "mission_id", "generation",
    "status", "phase", "identity", "planner_import", "card_ref",
    "execution_ref", "evidence_refs", "decision_refs", "consultation_refs",
    "active_blocker", "active_human_gate", "queue_exhausted",
    "completion", "next_safe_action", "last_operation",
})

# R1 consultationRef required fields
_CONSULTATION_REQUIRED_KEYS = frozenset({
    "execution_id", "mode", "snapshot_head", "tree_sha",
    "plan_fingerprint", "checkpoint_fingerprint", "bundle_fingerprint",
    "expected_question_ids", "schema_version", "status",
    "detailed_status", "verdict", "response_fingerprint",
    "response_artifact",
})

# R1 nested object allowlists (additionalProperties: false)
# Each maps a dotted path prefix to its allowed key set.
_NESTED_ALLOWLISTS = {
    "identity": frozenset({
        "board", "tenant", "repository", "branch",
        "base_sha", "head_sha", "tree_sha",
        "plan_fingerprint", "checkpoint_fingerprint",
    }),
    "planner_import": frozenset({
        "board", "import_id", "envelope_fingerprint",
    }),
    "card_ref": frozenset({"card_id", "phase"}),
    "execution_ref": frozenset({"run_id", "attempt_id"}),
    "last_operation": frozenset({
        "operation_id", "request_fingerprint", "attempt_id",
    }),
    "next_safe_action": frozenset({
        "action", "executable", "card_id", "reason_code",
    }),
    "completion": frozenset({
        "final_review", "merge_required", "merge_gate",
    }),
    "active_blocker": frozenset({
        "blocker_id", "reason_code", "summary",
        "evidence_ids", "resume_condition",
    }),
    "active_blocker.resume_condition": frozenset({
        "type", "description", "reference",
    }),
    "active_human_gate": frozenset({
        "gate_id", "gate_type", "version", "status",
        "prompt_fingerprint", "resolution_ref",
    }),
    "queue_exhausted": frozenset({
        "decision_id", "reason_code", "summary",
        "exhausted_at_generation", "evidence_ids",
        "resume_condition",
    }),
    "queue_exhausted.resume_condition": frozenset({
        "type", "description", "reference",
    }),
    "evidence_ref": frozenset({
        "evidence_id", "kind", "fingerprint", "artifact",
    }),
    "decision_ref": frozenset({
        "decision_id", "kind", "outcome", "evidence_ids",
    }),
    "consultation_ref": frozenset({
        "execution_id", "mode", "snapshot_head", "tree_sha",
        "plan_fingerprint", "checkpoint_fingerprint",
        "bundle_fingerprint", "expected_question_ids",
        "schema_version", "status", "detailed_status",
        "verdict", "response_fingerprint", "response_artifact",
    }),
    "gate_ref": frozenset({
        "gate_id", "gate_type", "repository", "branch",
        "commit_sha", "tree_sha", "diff_fingerprint",
        "bundle_fingerprint", "response_fingerprint",
        "response_artifact", "result",
    }),
}

# R1-aligned outcome vocabulary for TransitionResult.
# The R1 schema defines: applied, replayed, stale_generation, conflict, invalid.
# "not-found" and "failed" map to "invalid" with appropriate error codes.
R1_OUTCOMES_TRANSITIONED = "applied"
R1_OUTCOMES_REPLAYED = "replayed"
R1_OUTCOMES_STALE = "stale_generation"
R1_OUTCOMES_CONFLICT = "conflict"
R1_OUTCOMES_INVALID = "invalid"


# ---------------------------------------------------------------------------
# Canonical fingerprint
# ---------------------------------------------------------------------------

def canonical_fingerprint(value: Any) -> str:
    """Deterministic SHA-256 fingerprint over canonical JSON.

    Algorithm (per senior consultation):
      1. ``json.dumps(value, sort_keys=True, separators=(',',':'),
         ensure_ascii=False)``
      2. UTF-8 encode.
      3. SHA-256 hexdigest (64 lowercase hex chars).

    ``sort_keys`` makes the fingerprint independent of dict insertion
    order.  ``ensure_ascii=False`` preserves non-ASCII characters
    faithfully.  ``separators=(',',':')`` removes whitespace for a
    compact, unambiguous representation.
    """
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Typed result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CreateResult:
    """Result of :func:`create_mission`."""
    outcome: str  # "created" | "already-applied" | "conflict" | "invalid"
    mission_id: str
    generation: int
    state_fingerprint: str
    state: Optional[dict] = None
    error: Optional[dict] = None


@dataclass(frozen=True)
class TransitionResult:
    """Result of :func:`compare_and_transition`.

    Outcomes follow R1 vocabulary exactly:
    - ``applied``: generation advanced, new state persisted.
    - ``replayed``: same operation_id + same fingerprint → replay.
    - ``stale_generation``: expected_generation doesn't match current.
    - ``conflict``: same operation_id + different fingerprint → rejected.
    - ``invalid``: next_state fails validation, mission not found, or
      unexpected error.
    """
    outcome: str  # "applied" | "replayed" | "stale_generation" | "conflict" | "invalid"
    mission_id: str
    operation_id: str
    request_fingerprint: str
    generation: int
    state_fingerprint: str
    state: Optional[dict] = None
    error: Optional[dict] = None


@dataclass(frozen=True)
class MissionRecord:
    """Persistent mission row from ``mission_missions``."""
    mission_id: str
    schema_version: str
    status: str
    phase: str
    generation: int
    state_json: str
    state_fingerprint: str
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class JournalRecord:
    """Persistent journal row from ``mission_journal``."""
    mission_id: str
    operation_id: str
    request_fingerprint: str
    result_generation: int
    result_status: str
    result_fingerprint: str
    created_at: int


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

MISSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mission_missions (
    mission_id        TEXT PRIMARY KEY,
    schema_version    TEXT NOT NULL,
    status            TEXT NOT NULL,
    phase             TEXT NOT NULL,
    generation        INTEGER NOT NULL DEFAULT 0,
    state_json        TEXT NOT NULL,
    state_fingerprint TEXT NOT NULL,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_journal (
    mission_id         TEXT NOT NULL,
    operation_id       TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    result_generation  INTEGER NOT NULL,
    result_status      TEXT NOT NULL,
    result_fingerprint TEXT NOT NULL,
    created_at         INTEGER NOT NULL,
    PRIMARY KEY (mission_id, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_mission_journal_request_fingerprint
    ON mission_journal (request_fingerprint);
"""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_mission_state(conn: sqlite3.Connection) -> None:
    """Idempotently add mission-state tables to an existing database.

    Safe to call on:
    - a fresh database (creates both tables + index);
    - an existing K9 database (adds tables without touching K9);
    - a database that already has the tables (no-op).

    This function never drops, renames, or alters K9 tables or columns.
    """
    conn.executescript(MISSION_SCHEMA_SQL)


def ensure_mission_state_schema(conn: sqlite3.Connection) -> None:
    """Public alias for :func:`migrate_mission_state`.

    Intended for callers that open a connection outside the normal
    ``kanban_db.connect()`` path and need to guarantee the mission-state
    tables exist before using the API.
    """
    migrate_mission_state(conn)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_write_txn():
    """Return the write-transaction context manager.

    Prefers ``kanban_db.write_txn`` (which includes retry, delegated-child
    mutation guard, and file-length invariant) when available.  Falls back
    to a minimal local implementation for test isolation.
    """
    try:
        from hermes_cli.kanban_db import write_txn as _canonical_write_txn
        return _canonical_write_txn
    except (ImportError, AttributeError):
        pass
    return _local_write_txn


@contextmanager
def _local_write_txn(conn: sqlite3.Connection):
    """Fallback IMMEDIATE write transaction with rollback on exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    else:
        conn.execute("COMMIT")


# Cache the resolved context manager at module load.
_write_txn_ctx = None


def _write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Delegates to ``kanban_db.write_txn`` when available, providing retry,
    delegated-child mutation guard, and file-length invariant.
    """
    global _write_txn_ctx
    if _write_txn_ctx is None:
        _write_txn_ctx = _get_write_txn()
    return _write_txn_ctx(conn)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _new_mission_id() -> str:
    """Generate a short, URL-safe mission id."""
    return "m_" + secrets.token_hex(4)


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def _row_to_mission_record(row: sqlite3.Row) -> MissionRecord:
    return MissionRecord(
        mission_id=row["mission_id"],
        schema_version=row["schema_version"],
        status=row["status"],
        phase=row["phase"],
        generation=row["generation"],
        state_json=row["state_json"],
        state_fingerprint=row["state_fingerprint"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_journal_record(row: sqlite3.Row) -> JournalRecord:
    return JournalRecord(
        mission_id=row["mission_id"],
        operation_id=row["operation_id"],
        request_fingerprint=row["request_fingerprint"],
        result_generation=row["result_generation"],
        result_status=row["result_status"],
        result_fingerprint=row["result_fingerprint"],
        created_at=row["created_at"],
    )


def _reject_non_finite(obj, path: str = "$") -> list[str]:
    """Reject NaN/Infinity at any depth. Returns error list."""
    errors: list[str] = []
    if isinstance(obj, float):
        if obj != obj:  # NaN
            errors.append(f"{path}: NaN is not allowed")
        elif obj == float("inf"):
            errors.append(f"{path}: Infinity is not allowed")
        elif obj == float("-inf"):
            errors.append(f"{path}: -Infinity is not allowed")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            errors.extend(_reject_non_finite(v, f"{path}.{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            errors.extend(_reject_non_finite(v, f"{path}[{i}]"))
    return errors


def _check_nested_allowlists(state: dict) -> list[str]:
    """Check additionalProperties:false for all nested R1 object types."""
    errors: list[str] = []

    def _check_obj(obj: dict, allowed: frozenset, path: str) -> None:
        if not isinstance(obj, dict):
            return
        extra = set(obj.keys()) - allowed
        if extra:
            errors.append(f"{path}: unknown properties {sorted(extra)}")

    # Top-level
    _check_obj(state, _VALID_STATE_KEYS, "$")

    # identity
    ident = state.get("identity")
    if isinstance(ident, dict):
        _check_obj(ident, _NESTED_ALLOWLISTS["identity"], "$.identity")

    # planner_import
    pi = state.get("planner_import")
    if isinstance(pi, dict):
        _check_obj(pi, _NESTED_ALLOWLISTS["planner_import"], "$.planner_import")

    # card_ref
    cr = state.get("card_ref")
    if isinstance(cr, dict):
        _check_obj(cr, _NESTED_ALLOWLISTS["card_ref"], "$.card_ref")

    # execution_ref
    er = state.get("execution_ref")
    if isinstance(er, dict):
        _check_obj(er, _NESTED_ALLOWLISTS["execution_ref"], "$.execution_ref")

    # last_operation
    lo = state.get("last_operation")
    if isinstance(lo, dict):
        _check_obj(lo, _NESTED_ALLOWLISTS["last_operation"], "$.last_operation")

    # next_safe_action
    nsa = state.get("next_safe_action")
    if isinstance(nsa, dict):
        _check_obj(nsa, _NESTED_ALLOWLISTS["next_safe_action"], "$.next_safe_action")

    # completion
    comp = state.get("completion")
    if isinstance(comp, dict):
        _check_obj(comp, _NESTED_ALLOWLISTS["completion"], "$.completion")

    # active_blocker
    ab = state.get("active_blocker")
    if isinstance(ab, dict):
        _check_obj(ab, _NESTED_ALLOWLISTS["active_blocker"], "$.active_blocker")
        rc = ab.get("resume_condition")
        if isinstance(rc, dict):
            _check_obj(rc, _NESTED_ALLOWLISTS["active_blocker.resume_condition"], "$.active_blocker.resume_condition")

    # active_human_gate
    ahg = state.get("active_human_gate")
    if isinstance(ahg, dict):
        _check_obj(ahg, _NESTED_ALLOWLISTS["active_human_gate"], "$.active_human_gate")

    # queue_exhausted
    qe = state.get("queue_exhausted")
    if isinstance(qe, dict):
        _check_obj(qe, _NESTED_ALLOWLISTS["queue_exhausted"], "$.queue_exhausted")
        rc = qe.get("resume_condition")
        if isinstance(rc, dict):
            _check_obj(rc, _NESTED_ALLOWLISTS["queue_exhausted.resume_condition"], "$.queue_exhausted.resume_condition")

    # evidence_refs
    for i, ev in enumerate(state.get("evidence_refs", [])):
        if isinstance(ev, dict):
            _check_obj(ev, _NESTED_ALLOWLISTS["evidence_ref"], f"$.evidence_refs[{i}]")

    # decision_refs
    for i, dr in enumerate(state.get("decision_refs", [])):
        if isinstance(dr, dict):
            _check_obj(dr, _NESTED_ALLOWLISTS["decision_ref"], f"$.decision_refs[{i}]")

    # consultation_refs
    for i, cr in enumerate(state.get("consultation_refs", [])):
        if isinstance(cr, dict):
            _check_obj(cr, _NESTED_ALLOWLISTS["consultation_ref"], f"$.consultation_refs[{i}]")

    # completion.final_review (gateRef)
    if isinstance(comp, dict):
        fr = comp.get("final_review")
        if isinstance(fr, dict):
            _check_obj(fr, _NESTED_ALLOWLISTS["gate_ref"], "$.completion.final_review")
        mg = comp.get("merge_gate")
        if isinstance(mg, dict):
            _check_obj(mg, _NESTED_ALLOWLISTS["gate_ref"], "$.completion.merge_gate")

    return errors


def _validate_state_shape(state: dict) -> list[str]:
    """Validate that *state* matches the R1 mission_state document shape.

    Returns a list of error strings; empty means valid.
    Enforces all R1 structural invariants including status-dependent
    payload requirements and mutual exclusions.

    Strategy (R2.1 senior consultation):
      Product-only validator implementing R1 structural + semantic invariants.
      No JSON Schema dependency, no R1 worktree path dependency.
      R1 schema/tests remain the normative development reference.
    """
    errors: list[str] = []

    # Non-finite check (NaN/Infinity at any depth)
    errors.extend(_reject_non_finite(state))

    # Nested allowlists (additionalProperties:false)
    errors.extend(_check_nested_allowlists(state))

    if state.get("document_type") != DOCUMENT_TYPE_STATE:
        errors.append("document_type must be 'mission_state'")
    if state.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be '{SCHEMA_VERSION}'")
    mission_id = state.get("mission_id")
    if not mission_id or not isinstance(mission_id, str):
        errors.append("mission_id must be a non-empty string")
    status = state.get("status")
    if status not in VALID_STATUSES:
        errors.append(f"status must be one of {sorted(VALID_STATUSES)}")
    phase = state.get("phase")
    if phase not in VALID_PHASES:
        errors.append(f"phase must be one of {sorted(VALID_PHASES)}")
    generation = state.get("generation")
    if not isinstance(generation, int) or generation < 0 or isinstance(generation, bool):
        errors.append("generation must be a non-negative integer")

    # Reject unknown top-level keys (R1 additionalProperties: false)
    unknown_keys = set(state.keys()) - _VALID_STATE_KEYS
    if unknown_keys:
        errors.append(f"unknown top-level properties: {sorted(unknown_keys)}")

    # Structural check for consultation_refs entries
    for i, ref in enumerate(state.get("consultation_refs", [])):
        if isinstance(ref, dict):
            missing = _CONSULTATION_REQUIRED_KEYS - set(ref.keys())
            if missing:
                errors.append(
                    f"consultation_refs[{i}] missing required fields: "
                    f"{sorted(missing)}"
                )

    # Reject extra properties in planner_import (R1 additionalProperties: false)
    pi = state.get("planner_import")
    if isinstance(pi, dict):
        _valid_pi_keys = {"board", "import_id", "envelope_fingerprint"}
        pi_unknown = set(pi.keys()) - _valid_pi_keys
        if pi_unknown:
            errors.append(
                f"planner_import has unknown properties: {sorted(pi_unknown)}"
            )

    # --- Terminal status invariant ---
    if status in TERMINAL_STATUSES:
        if phase != "terminal":
            errors.append(f"terminal status '{status}' requires phase 'terminal'")
        if state.get("next_safe_action") is not None:
            errors.append(f"terminal status '{status}' requires null next_safe_action")

    # --- completed: final_review binding + merge_gate implication ---
    if status == "completed":
        completion = state.get("completion")
        if not isinstance(completion, dict):
            errors.append("completed status requires completion object")
        else:
            if completion.get("final_review") is None:
                errors.append("completed status requires non-null completion.final_review")
            else:
                fr = completion["final_review"]
                identity = state.get("identity") or {}
                if isinstance(fr, dict):
                    if fr.get("commit_sha") != identity.get("head_sha"):
                        errors.append(
                            "final_review.commit_sha must match identity.head_sha"
                        )
                    if fr.get("tree_sha") != identity.get("tree_sha"):
                        errors.append(
                            "final_review.tree_sha must match identity.tree_sha"
                        )
            if completion.get("merge_required") is True:
                if completion.get("merge_gate") is None:
                    errors.append(
                        "merge_required=True requires non-null completion.merge_gate"
                    )

    # --- blocked: blocker + resume_condition + next_safe_action required ---
    if status == "blocked":
        blocker = state.get("active_blocker")
        if not isinstance(blocker, dict):
            errors.append("blocked status requires active_blocker object")
        else:
            if "resume_condition" not in blocker or blocker["resume_condition"] is None:
                errors.append(
                    "blocked active_blocker requires non-null resume_condition"
                )
        if state.get("active_human_gate") is not None:
            errors.append("blocked status requires null active_human_gate")
        if state.get("queue_exhausted") is not None:
            errors.append("blocked status requires null queue_exhausted")
        nsa = state.get("next_safe_action")
        if not isinstance(nsa, dict):
            errors.append("blocked status requires structured next_safe_action")
        else:
            if nsa.get("executable") is not False:
                errors.append("blocked status requires non-executable next_safe_action")

    # --- human_gate: gate + next_safe_action exact match ---
    if status == "human_gate":
        hg = state.get("active_human_gate")
        if not isinstance(hg, dict):
            errors.append("human_gate status requires active_human_gate object")
        else:
            for field in ("gate_id", "gate_type", "version", "status",
                          "prompt_fingerprint", "resolution_ref"):
                if field not in hg:
                    errors.append(
                        f"active_human_gate requires field '{field}'"
                    )
            if hg.get("status") != "pending":
                errors.append("human_gate requires active_human_gate.status='pending'")
        if state.get("active_blocker") is not None:
            errors.append("human_gate status requires null active_blocker")
        if state.get("queue_exhausted") is not None:
            errors.append("human_gate status requires null queue_exhausted")
        nsa = state.get("next_safe_action")
        if not isinstance(nsa, dict):
            errors.append("human_gate status requires structured next_safe_action")
        else:
            _expected_nsa = {
                "action": "await_human",
                "executable": False,
                "card_id": None,
                "reason_code": "human_gate_pending",
            }
            if nsa != _expected_nsa:
                errors.append(
                    "human_gate requires exact next_safe_action "
                    "{action='await_human', executable=false, ...}"
                )

    # --- queue_exhausted: payload + resume_condition + next_safe_action ---
    if status == "queue_exhausted":
        qe = state.get("queue_exhausted")
        if not isinstance(qe, dict):
            errors.append("queue_exhausted status requires queue_exhausted object")
        else:
            if "resume_condition" not in qe or qe["resume_condition"] is None:
                errors.append(
                    "queue_exhausted requires non-null resume_condition"
                )
        if state.get("active_blocker") is not None:
            errors.append("queue_exhausted status requires null active_blocker")
        if state.get("active_human_gate") is not None:
            errors.append("queue_exhausted status requires null active_human_gate")
        nsa = state.get("next_safe_action")
        if not isinstance(nsa, dict):
            errors.append("queue_exhausted status requires structured next_safe_action")
        else:
            if nsa.get("action") not in ("replan", "await_resume_condition"):
                errors.append(
                    "queue_exhausted requires next_safe_action.action "
                    "in {'replan', 'await_resume_condition'}"
                )
            if nsa.get("executable") is not False:
                errors.append("queue_exhausted requires non-executable next_safe_action")

    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_mission(
    conn: sqlite3.Connection,
    *,
    state: dict,
    operation_id: str,
) -> CreateResult:
    """Create a new durable mission from a full mission_state document.

    The *state* must be a complete ``mission_state`` document matching
    the R1 schema.  The ``generation`` field in *state* is ignored;
    creation always sets generation to 0.

    Idempotency: if *operation_id* has already been applied for this
    mission, returns ``already-applied`` with the original result.
    Conflict: if *operation_id* exists with a different fingerprint,
    returns ``conflict``.
    """
    errors = _validate_state_shape(state)
    if errors:
        return CreateResult(
            outcome="invalid",
            mission_id=state.get("mission_id", ""),
            generation=0,
            state_fingerprint="",
            error={"code": "invalid", "message": "; ".join(errors)},
        )

    mission_id = state["mission_id"]
    # Force generation to 0 for creation
    state_copy = dict(state)
    state_copy["generation"] = 0
    state_fingerprint = canonical_fingerprint(state_copy)
    request_fingerprint = canonical_fingerprint({
        "operation_id": operation_id,
        "state": state_copy,
    })
    now = _now_ms()

    with _write_txn(conn):
        # Check for existing mission
        existing = conn.execute(
            "SELECT mission_id FROM mission_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if existing is not None:
            # Mission already exists — check journal for idempotency
            journal_row = conn.execute(
                "SELECT operation_id, request_fingerprint, result_generation, "
                "result_status, result_fingerprint "
                "FROM mission_journal "
                "WHERE mission_id = ? AND operation_id = ?",
                (mission_id, operation_id),
            ).fetchone()
            if journal_row is not None:
                if journal_row["request_fingerprint"] == request_fingerprint:
                    return CreateResult(
                        outcome="already-applied",
                        mission_id=mission_id,
                        generation=journal_row["result_generation"],
                        state_fingerprint=journal_row["result_fingerprint"],
                    )
                else:
                    return CreateResult(
                        outcome="conflict",
                        mission_id=mission_id,
                        generation=0,
                        state_fingerprint="",
                        error={
                            "code": "conflict",
                            "message": f"operation_id '{operation_id}' already used with different content",
                        },
                    )
            else:
                # Mission exists but this operation_id is new — still a
                # creation conflict (can't re-create an existing mission)
                return CreateResult(
                    outcome="conflict",
                    mission_id=mission_id,
                    generation=0,
                    state_fingerprint="",
                    error={
                        "code": "conflict",
                        "message": f"mission '{mission_id}' already exists",
                    },
                )

        # Insert mission
        conn.execute(
            "INSERT INTO mission_missions "
            "(mission_id, schema_version, status, phase, generation, "
            " state_json, state_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                SCHEMA_VERSION,
                state_copy["status"],
                state_copy["phase"],
                0,
                json.dumps(state_copy, sort_keys=True, ensure_ascii=False),
                state_fingerprint,
                now,
                now,
            ),
        )

        # Insert journal entry
        conn.execute(
            "INSERT INTO mission_journal "
            "(mission_id, operation_id, request_fingerprint, "
            " result_generation, result_status, result_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                operation_id,
                request_fingerprint,
                0,
                "created",
                state_fingerprint,
                now,
            ),
        )

    return CreateResult(
        outcome="created",
        mission_id=mission_id,
        generation=0,
        state_fingerprint=state_fingerprint,
        state=state_copy,
    )


def get_mission(
    conn: sqlite3.Connection,
    mission_id: str,
) -> Optional[MissionRecord]:
    """Read the current durable state of a mission.

    Returns ``None`` if the mission does not exist.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT mission_id, schema_version, status, phase, generation, "
        "state_json, state_fingerprint, created_at, updated_at "
        "FROM mission_missions WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_mission_record(row)


def compare_and_transition(
    conn: sqlite3.Connection,
    *,
    mission_id: str,
    expected_generation: int,
    operation_id: str,
    next_state: dict,
    consumes_consultation: str | None = None,
    decision_id: str | None = None,
) -> TransitionResult:
    """Atomically compare-and-transition a mission.

    Implements optimistic concurrency control:

    1. Check journal for idempotency (same operation_id + fingerprint → replay).
    2. Verify the current generation matches *expected_generation*.
    3. Validate the next_state document.
    4. Within one IMMEDIATE transaction: update mission, write journal, increment
       generation.

    Outcomes (R1-aligned):
    - ``applied``: generation advanced, new state persisted.
    - ``replayed``: same operation_id + same fingerprint → replay.
    - ``stale_generation``: expected_generation doesn't match current.
    - ``conflict``: same operation_id + different fingerprint → rejected.
    - ``invalid``: next_state fails validation, mission not found, or
      unexpected error.
    """
    errors = _validate_state_shape(next_state)
    if errors:
        return TransitionResult(
            outcome=R1_OUTCOMES_INVALID,
            mission_id=mission_id,
            operation_id=operation_id,
            request_fingerprint="",
            generation=0,
            state_fingerprint="",
            error={"code": "invalid", "message": "; ".join(errors)},
        )

    # Verify mission_id consistency
    if next_state.get("mission_id") != mission_id:
        return TransitionResult(
            outcome=R1_OUTCOMES_INVALID,
            mission_id=mission_id,
            operation_id=operation_id,
            request_fingerprint="",
            generation=0,
            state_fingerprint="",
            error={
                "code": "invalid",
                "message": f"next_state mission_id '{next_state.get('mission_id')}' "
                           f"does not match request mission_id '{mission_id}'",
            },
        )

    # Normalize generation in next_state BEFORE fingerprinting.
    # (R2.1 senior decision: option (c) — caller-supplied generation is noise;
    #  the canonical representation is expected_generation + 1.)
    _normalized_next_state = dict(next_state)
    _normalized_next_state["generation"] = expected_generation + 1

    # Consultation consumption requires a local decision (R1 invariant)
    if consumes_consultation and not decision_id:
        return TransitionResult(
            outcome=R1_OUTCOMES_INVALID,
            mission_id=mission_id,
            operation_id=operation_id,
            request_fingerprint="",
            generation=0,
            state_fingerprint="",
            error={
                "code": "missing_local_decision",
                "message": "consultation consumption requires a local decision",
            },
        )

    request_fingerprint = canonical_fingerprint({
        "operation_id": operation_id,
        "expected_generation": expected_generation,
        "next_state": _normalized_next_state,
    })

    with _write_txn(conn):
        # 1. Check if mission exists
        current = conn.execute(
            "SELECT mission_id, generation, state_fingerprint, status "
            "FROM mission_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if current is None:
            return TransitionResult(
                outcome=R1_OUTCOMES_INVALID,
                mission_id=mission_id,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                generation=0,
                state_fingerprint="",
                error={
                    "code": "not-found",
                    "message": f"mission '{mission_id}' does not exist",
                },
            )

        current_generation = current["generation"]

        # 2. Check journal for idempotency
        journal_row = conn.execute(
            "SELECT request_fingerprint, result_generation, "
            "result_status, result_fingerprint "
            "FROM mission_journal "
            "WHERE mission_id = ? AND operation_id = ?",
            (mission_id, operation_id),
        ).fetchone()

        if journal_row is not None:
            if journal_row["request_fingerprint"] == request_fingerprint:
                # Same operation + same fingerprint → replay
                return TransitionResult(
                    outcome=R1_OUTCOMES_REPLAYED,
                    mission_id=mission_id,
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                    generation=journal_row["result_generation"],
                    state_fingerprint=journal_row["result_fingerprint"],
                )
            else:
                # Same operation + different fingerprint → conflict
                return TransitionResult(
                    outcome=R1_OUTCOMES_CONFLICT,
                    mission_id=mission_id,
                    operation_id=operation_id,
                    request_fingerprint=request_fingerprint,
                    generation=current_generation,
                    state_fingerprint=current["state_fingerprint"],
                    error={
                        "code": "conflict",
                        "message": f"operation_id '{operation_id}' already used with different content",
                    },
                )

        # 3. CAS check: generation must match
        if current_generation != expected_generation:
            return TransitionResult(
                outcome=R1_OUTCOMES_STALE,
                mission_id=mission_id,
                operation_id=operation_id,
                request_fingerprint=request_fingerprint,
                generation=current_generation,
                state_fingerprint=current["state_fingerprint"],
                error={
                    "code": "stale_generation",
                    "message": f"expected generation {expected_generation}, "
                               f"current is {current_generation}",
                },
            )

        # 4. Compute new state — generation already normalized above
        new_generation = expected_generation + 1
        next_state_copy = dict(_normalized_next_state)
        new_fingerprint = canonical_fingerprint(next_state_copy)
        now = _now_ms()

        # 5. Atomic update: mission + journal
        conn.execute(
            "UPDATE mission_missions SET "
            "status = ?, phase = ?, generation = ?, "
            "state_json = ?, state_fingerprint = ?, updated_at = ? "
            "WHERE mission_id = ?",
            (
                next_state_copy["status"],
                next_state_copy["phase"],
                new_generation,
                json.dumps(next_state_copy, sort_keys=True, ensure_ascii=False),
                new_fingerprint,
                now,
                mission_id,
            ),
        )

        conn.execute(
            "INSERT INTO mission_journal "
            "(mission_id, operation_id, request_fingerprint, "
            " result_generation, result_status, result_fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mission_id,
                operation_id,
                request_fingerprint,
                new_generation,
                next_state_copy["status"],
                new_fingerprint,
                now,
            ),
        )

    return TransitionResult(
        outcome=R1_OUTCOMES_TRANSITIONED,
        mission_id=mission_id,
        operation_id=operation_id,
        request_fingerprint=request_fingerprint,
        generation=new_generation,
        state_fingerprint=new_fingerprint,
        state=next_state_copy,
    )


def list_journal(
    conn: sqlite3.Connection,
    mission_id: str,
) -> list[JournalRecord]:
    """List journal entries for a mission, ordered by creation time."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT mission_id, operation_id, request_fingerprint, "
        "result_generation, result_status, result_fingerprint, created_at "
        "FROM mission_journal WHERE mission_id = ? ORDER BY created_at",
        (mission_id,),
    ).fetchall()
    return [_row_to_journal_record(r) for r in rows]
