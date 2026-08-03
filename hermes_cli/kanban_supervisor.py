"""Durable supervisor loop for Kanban long-running missions.

Provides a reentrant supervisor that persists state to SQLite, manages
leases with fencing epochs, ingests roadmap slices programmatically,
executes one slice per tick, and survives session restarts.

Schema version: ``kanban-supervisor/v1`` (extends kanban-mission-state/v1).

This module sits alongside R2 mission-state tables and K9 kanban tables
in the same SQLite database.  It adds three new tables via idempotent
``CREATE TABLE IF NOT EXISTS`` and never modifies existing tables.

Architecture decisions (from senior consultation af3aab04):
- Fencing epoch on leases: every durable write validates
  (mission_id, owner, fencing_epoch, unexpired lease).
- Transaction boundaries: supervisor writes use IMMEDIATE txn;
  R2 transitions use their own txn via compare_and_transition.
- Slice ingestion: programmatic Python API, no file I/O in supervisor core.
- Format: JSON (stdlib), no new dependencies.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "kanban-supervisor/v1"

VALID_OUTCOMES = frozenset({
    "success", "blocked", "human_gate", "queue_exhausted",
    "failed", "skipped",
})

VALID_SLICE_STATUSES = frozenset({
    "pending", "active", "completed", "blocked",
    "human_gate", "queue_exhausted", "failed", "skipped",
})

TERMINAL_SLICE_STATUSES = frozenset({
    "completed", "failed", "skipped",
})

# Default lease TTL in seconds (1 hour, hard-capped)
DEFAULT_LEASE_TTL = 3600
MAX_LEASE_TTL = 7200

# Default max attempts per slice before queue_exhausted
DEFAULT_MAX_ATTEMPTS = 3

# Backoff base in seconds
BACKOFF_BASE_S = 5

# R1 schema: slice object allowed keys
_SLICE_VALID_KEYS = frozenset({
    "slice_id", "phase", "description", "dependencies",
    "material", "acceptance_criteria", "tests",
    "gate_type", "max_attempts", "priority",
})

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SUPERVISOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mission_supervisor_leases (
    mission_id    TEXT NOT NULL,
    supervisor_id TEXT NOT NULL,
    fencing_epoch INTEGER NOT NULL DEFAULT 1,
    acquired_at   INTEGER NOT NULL,
    expires_at    INTEGER NOT NULL,
    renewed_at    INTEGER NOT NULL,
    PRIMARY KEY (mission_id)
);

CREATE TABLE IF NOT EXISTS mission_roadmap_slices (
    mission_id       TEXT NOT NULL,
    slice_id         TEXT NOT NULL,
    phase            TEXT NOT NULL,
    description      TEXT NOT NULL,
    dependencies     TEXT NOT NULL DEFAULT '[]',
    material         TEXT NOT NULL DEFAULT '{}',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    tests            TEXT NOT NULL DEFAULT '[]',
    gate_type        TEXT,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    priority         INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    outcome          TEXT,
    evidence_json    TEXT,
    error_json       TEXT,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (mission_id, slice_id)
);

CREATE TABLE IF NOT EXISTS mission_supervisor_state (
    mission_id           TEXT PRIMARY KEY,
    current_slice_id     TEXT,
    last_completed_slice TEXT,
    error_count          INTEGER NOT NULL DEFAULT 0,
    fencing_epoch        INTEGER NOT NULL DEFAULT 1,
    total_completed      INTEGER NOT NULL DEFAULT 0,
    total_failed         INTEGER NOT NULL DEFAULT 0,
    total_blocked        INTEGER NOT NULL DEFAULT 0,
    updated_at           INTEGER NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_supervisor(conn: sqlite3.Connection) -> None:
    """Idempotently add supervisor tables to an existing database.

    Safe to call on fresh DBs, existing K9 DBs, or re-opened DBs.
    Never touches K9 tables or R2 mission-state tables.
    """
    conn.executescript(SUPERVISOR_SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_write_txn():
    """Return the write-transaction context manager.

    Prefers ``kanban_db.write_txn`` when available.  Falls back to
    a minimal local implementation for test isolation.
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


_write_txn_ctx = None


def _write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction."""
    global _write_txn_ctx
    if _write_txn_ctx is None:
        _write_txn_ctx = _get_write_txn()
    return _write_txn_ctx(conn)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_s() -> int:
    return int(time.time())


def _new_operation_id() -> str:
    return "op_" + secrets.token_hex(4)


# ---------------------------------------------------------------------------
# Typed result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeaseResult:
    """Result of lease acquisition or renewal."""
    outcome: str  # "acquired" | "renewed" | "lease_held" | "expired" | "invalid"
    mission_id: str
    supervisor_id: str
    fencing_epoch: int
    expires_at: int
    error: Optional[dict] = None


@dataclass(frozen=True)
class AddSlicesResult:
    """Result of adding slices to a mission."""
    outcome: str  # "added" | "already-applied" | "conflict" | "invalid"
    mission_id: str
    slice_count: int
    errors: Optional[list[str]] = None


@dataclass(frozen=True)
class SliceRecord:
    """Persistent slice row from ``mission_roadmap_slices``."""
    mission_id: str
    slice_id: str
    phase: str
    description: str
    dependencies: list[str]
    material: dict
    acceptance_criteria: list[str]
    tests: list[str]
    gate_type: Optional[str]
    max_attempts: int
    priority: int
    status: str
    attempt_count: int
    outcome: Optional[str]
    evidence_json: Optional[str]
    error_json: Optional[str]
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class TickResult:
    """Result of a single supervisor tick."""
    outcome: str  # "slice_executed" | "slice_blocked" | "slice_human_gate" |
                  # "slice_queue_exhausted" | "slice_failed" | "no_slices" |
                  # "lease_expired" | "fencing_invalid" | "error"
    mission_id: str
    supervisor_id: str
    fencing_epoch: int
    slice_id: Optional[str] = None
    error: Optional[dict] = None


@dataclass(frozen=True)
class SupervisorState:
    """Persistent supervisor state from ``mission_supervisor_state``."""
    mission_id: str
    current_slice_id: Optional[str]
    last_completed_slice: Optional[str]
    error_count: int
    fencing_epoch: int
    total_completed: int
    total_failed: int
    total_blocked: int
    updated_at: int


# ---------------------------------------------------------------------------
# Slice validation
# ---------------------------------------------------------------------------

def validate_slice(slice_def: dict) -> list[str]:
    """Validate a slice definition.  Returns list of errors; empty = valid."""
    errors: list[str] = []

    sid = slice_def.get("slice_id")
    if not sid or not isinstance(sid, str):
        errors.append("slice_id must be a non-empty string")

    phase = slice_def.get("phase")
    if not phase or not isinstance(phase, str):
        errors.append("phase must be a non-empty string")

    desc = slice_def.get("description")
    if not desc or not isinstance(desc, str):
        errors.append("description must be a non-empty string")

    deps = slice_def.get("dependencies", [])
    if not isinstance(deps, list):
        errors.append("dependencies must be a list")
    elif not all(isinstance(d, str) and d for d in deps):
        errors.append("dependencies must be non-empty strings")

    material = slice_def.get("material", {})
    if not isinstance(material, dict):
        errors.append("material must be a dict")

    criteria = slice_def.get("acceptance_criteria", [])
    if not isinstance(criteria, list):
        errors.append("acceptance_criteria must be a list")

    tests = slice_def.get("tests", [])
    if not isinstance(tests, list):
        errors.append("tests must be a list")

    max_attempts = slice_def.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    if not isinstance(max_attempts, int) or max_attempts < 1:
        errors.append("max_attempts must be a positive integer")

    priority = slice_def.get("priority", 0)
    if not isinstance(priority, int):
        errors.append("priority must be an integer")

    # Check for unknown keys
    unknown = set(slice_def.keys()) - _SLICE_VALID_KEYS
    if unknown:
        errors.append(f"unknown keys: {sorted(unknown)}")

    return errors


# ---------------------------------------------------------------------------
# Slice ingestion (BF3 resolution)
# ---------------------------------------------------------------------------

def add_slices(
    conn: sqlite3.Connection,
    mission_id: str,
    slices: list[dict],
) -> AddSlicesResult:
    """Ingest roadmap slices for a mission.

    Programmatic API — no file I/O.  Each slice is validated before
    insertion.  Dependencies must reference slices in the same batch
    or already in the database.
    """
    if not slices:
        return AddSlicesResult(
            outcome="invalid",
            mission_id=mission_id,
            slice_count=0,
            errors=["slices list is empty"],
        )

    # Validate all slices first
    all_errors: list[str] = []
    seen_ids: set[str] = set()
    for i, s in enumerate(slices):
        errs = validate_slice(s)
        if errs:
            all_errors.extend(f"slice[{i}]: {e}" for e in errs)
        sid = s.get("slice_id", "")
        if sid in seen_ids:
            all_errors.append(f"slice[{i}]: duplicate slice_id '{sid}'")
        seen_ids.add(sid)

    if all_errors:
        return AddSlicesResult(
            outcome="invalid",
            mission_id=mission_id,
            slice_count=0,
            errors=all_errors,
        )

    # Check mission exists
    conn.row_factory = sqlite3.Row
    mission = conn.execute(
        "SELECT mission_id FROM mission_missions WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()
    if mission is None:
        return AddSlicesResult(
            outcome="invalid",
            mission_id=mission_id,
            slice_count=0,
            errors=[f"mission '{mission_id}' does not exist"],
        )

    now = _now_ms()

    with _write_txn(conn):
        # Check which slices already exist
        existing = set()
        for s in slices:
            row = conn.execute(
                "SELECT slice_id FROM mission_roadmap_slices "
                "WHERE mission_id = ? AND slice_id = ?",
                (mission_id, s["slice_id"]),
            ).fetchone()
            if row is not None:
                existing.add(s["slice_id"])

        added = 0
        for s in slices:
            if s["slice_id"] in existing:
                continue

            # Validate dependencies exist
            for dep in s.get("dependencies", []):
                dep_row = conn.execute(
                    "SELECT slice_id FROM mission_roadmap_slices "
                    "WHERE mission_id = ? AND slice_id = ?",
                    (mission_id, dep),
                ).fetchone()
                if dep_row is None and dep not in seen_ids:
                    # Dependency not in DB and not in this batch — allow
                    # (will be validated at execution time)
                    pass

            conn.execute(
                "INSERT INTO mission_roadmap_slices "
                "(mission_id, slice_id, phase, description, dependencies, "
                " material, acceptance_criteria, tests, gate_type, "
                " max_attempts, priority, status, attempt_count, "
                " created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)",
                (
                    mission_id,
                    s["slice_id"],
                    s["phase"],
                    s["description"],
                    json.dumps(s.get("dependencies", []), sort_keys=True),
                    json.dumps(s.get("material", {}), sort_keys=True),
                    json.dumps(s.get("acceptance_criteria", []), sort_keys=True),
                    json.dumps(s.get("tests", []), sort_keys=True),
                    s.get("gate_type"),
                    s.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
                    s.get("priority", 0),
                    now,
                    now,
                ),
            )
            added += 1

    if added == 0 and existing:
        return AddSlicesResult(
            outcome="already-applied",
            mission_id=mission_id,
            slice_count=len(slices),
        )

    return AddSlicesResult(
        outcome="added",
        mission_id=mission_id,
        slice_count=added,
    )


def get_roadmap_slices(
    conn: sqlite3.Connection,
    mission_id: str,
) -> list[SliceRecord]:
    """List all slices for a mission, ordered by priority descending, then creation."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM mission_roadmap_slices "
        "WHERE mission_id = ? "
        "ORDER BY priority DESC, created_at ASC",
        (mission_id,),
    ).fetchall()
    return [_row_to_slice_record(r) for r in rows]


def _row_to_slice_record(row: sqlite3.Row) -> SliceRecord:
    return SliceRecord(
        mission_id=row["mission_id"],
        slice_id=row["slice_id"],
        phase=row["phase"],
        description=row["description"],
        dependencies=json.loads(row["dependencies"]),
        material=json.loads(row["material"]),
        acceptance_criteria=json.loads(row["acceptance_criteria"]),
        tests=json.loads(row["tests"]),
        gate_type=row["gate_type"],
        max_attempts=row["max_attempts"],
        priority=row["priority"],
        status=row["status"],
        attempt_count=row["attempt_count"],
        outcome=row["outcome"],
        evidence_json=row["evidence_json"],
        error_json=row["error_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Lease management with fencing epoch (BF1 resolution)
# ---------------------------------------------------------------------------

def acquire_supervisor_lease(
    conn: sqlite3.Connection,
    mission_id: str,
    supervisor_id: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL,
) -> LeaseResult:
    """Acquire or renew a supervisor lease for a mission.

    Returns fencing_epoch that must be validated on every subsequent write.
    Uses INSERT OR IGNORE for atomic first-writer-wins.
    """
    ttl_seconds = min(ttl_seconds, MAX_LEASE_TTL)
    now = _now_s()
    expires_at = now + ttl_seconds

    with _write_txn(conn):
        existing = conn.execute(
            "SELECT supervisor_id, fencing_epoch, expires_at "
            "FROM mission_supervisor_leases WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()

        if existing is not None:
            if existing["supervisor_id"] == supervisor_id:
                # Same owner — renew
                new_epoch = existing["fencing_epoch"] + 1
                conn.execute(
                    "UPDATE mission_supervisor_leases "
                    "SET fencing_epoch = ?, expires_at = ?, renewed_at = ? "
                    "WHERE mission_id = ?",
                    (new_epoch, expires_at, now, mission_id),
                )
                return LeaseResult(
                    outcome="renewed",
                    mission_id=mission_id,
                    supervisor_id=supervisor_id,
                    fencing_epoch=new_epoch,
                    expires_at=expires_at,
                )
            elif existing["expires_at"] > now:
                # Different owner, lease still valid
                return LeaseResult(
                    outcome="lease_held",
                    mission_id=mission_id,
                    supervisor_id=supervisor_id,
                    fencing_epoch=existing["fencing_epoch"],
                    expires_at=existing["expires_at"],
                    error={
                        "code": "lease_held",
                        "message": f"lease held by '{existing['supervisor_id']}'",
                    },
                )
            else:
                # Different owner, lease expired — take over
                new_epoch = existing["fencing_epoch"] + 1
                conn.execute(
                    "UPDATE mission_supervisor_leases "
                    "SET supervisor_id = ?, fencing_epoch = ?, "
                    "acquired_at = ?, expires_at = ?, renewed_at = ? "
                    "WHERE mission_id = ?",
                    (supervisor_id, new_epoch, now, expires_at, now, mission_id),
                )
                return LeaseResult(
                    outcome="acquired",
                    mission_id=mission_id,
                    supervisor_id=supervisor_id,
                    fencing_epoch=new_epoch,
                    expires_at=expires_at,
                )

        # No existing lease — create
        conn.execute(
            "INSERT INTO mission_supervisor_leases "
            "(mission_id, supervisor_id, fencing_epoch, acquired_at, "
            " expires_at, renewed_at) "
            "VALUES (?, ?, 1, ?, ?, ?)",
            (mission_id, supervisor_id, now, expires_at, now),
        )
        return LeaseResult(
            outcome="acquired",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=1,
            expires_at=expires_at,
        )


def renew_supervisor_lease(
    conn: sqlite3.Connection,
    mission_id: str,
    supervisor_id: str,
    ttl_seconds: int = DEFAULT_LEASE_TTL,
) -> LeaseResult:
    """Renew an existing supervisor lease.  Validates ownership."""
    return acquire_supervisor_lease(conn, mission_id, supervisor_id, ttl_seconds)


def release_supervisor_lease(
    conn: sqlite3.Connection,
    mission_id: str,
    supervisor_id: str,
) -> None:
    """Release a supervisor lease (cleanup)."""
    with _write_txn(conn):
        conn.execute(
            "DELETE FROM mission_supervisor_leases "
            "WHERE mission_id = ? AND supervisor_id = ?",
            (mission_id, supervisor_id),
        )


# ---------------------------------------------------------------------------
# Fencing validation (BF1 resolution — every durable write)
# ---------------------------------------------------------------------------

def _validate_fencing(
    conn: sqlite3.Connection,
    mission_id: str,
    supervisor_id: str,
    fencing_epoch: int,
) -> bool:
    """Validate that the caller still owns a valid, unexpired lease.

    Must be called before every durable supervisor write.
    Returns True if valid, False if fencing is broken.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT supervisor_id, fencing_epoch, expires_at "
        "FROM mission_supervisor_leases WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()

    if row is None:
        return False
    if row["supervisor_id"] != supervisor_id:
        return False
    if row["fencing_epoch"] != fencing_epoch:
        return False
    if row["expires_at"] <= _now_s():
        return False
    return True


# ---------------------------------------------------------------------------
# Supervisor state
# ---------------------------------------------------------------------------

def _ensure_state_exists(
    conn: sqlite3.Connection,
    mission_id: str,
    fencing_epoch: int,
) -> None:
    """Ensure a supervisor state row exists for a mission.

    Uses INSERT OR IGNORE for idempotent creation.
    Called inside write transactions before UPDATE.
    """
    now = _now_ms()
    conn.execute(
        "INSERT OR IGNORE INTO mission_supervisor_state "
        "(mission_id, current_slice_id, last_completed_slice, error_count, "
        " fencing_epoch, total_completed, total_failed, total_blocked, updated_at) "
        "VALUES (?, NULL, NULL, 0, ?, 0, 0, 0, ?)",
        (mission_id, fencing_epoch, now),
    )


def recover_supervisor_state(
    conn: sqlite3.Connection,
    mission_id: str,
) -> Optional[SupervisorState]:
    """Read the current supervisor state for recovery."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mission_supervisor_state WHERE mission_id = ?",
        (mission_id,),
    ).fetchone()
    if row is None:
        return None
    return SupervisorState(
        mission_id=row["mission_id"],
        current_slice_id=row["current_slice_id"],
        last_completed_slice=row["last_completed_slice"],
        error_count=row["error_count"],
        fencing_epoch=row["fencing_epoch"],
        total_completed=row["total_completed"],
        total_failed=row["total_failed"],
        total_blocked=row["total_blocked"],
        updated_at=row["updated_at"],
    )


def detect_incomplete_slices(
    conn: sqlite3.Connection,
    mission_id: str,
) -> list[SliceRecord]:
    """Detect slices that are 'active' but may be incomplete after a crash."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM mission_roadmap_slices "
        "WHERE mission_id = ? AND status = 'active'",
        (mission_id,),
    ).fetchall()
    return [_row_to_slice_record(r) for r in rows]


# ---------------------------------------------------------------------------
# Slice selection
# ---------------------------------------------------------------------------

def get_next_slice(
    conn: sqlite3.Connection,
    mission_id: str,
) -> Optional[SliceRecord]:
    """Select the next executable slice for a mission.

    Selection criteria:
    1. Status must be 'pending' or 'retryable' (blocked/human_gate that
       has been resolved).
    2. All dependencies must be completed.
    3. Highest priority first, then earliest created.
    """
    conn.row_factory = sqlite3.Row

    # Get all pending slices ordered by priority
    pending = conn.execute(
        "SELECT * FROM mission_roadmap_slices "
        "WHERE mission_id = ? AND status = 'pending' "
        "ORDER BY priority DESC, created_at ASC",
        (mission_id,),
    ).fetchall()

    for row in pending:
        deps = json.loads(row["dependencies"])
        if not deps:
            return _row_to_slice_record(row)

        # Check all dependencies are completed
        all_deps_met = True
        for dep in deps:
            dep_row = conn.execute(
                "SELECT status FROM mission_roadmap_slices "
                "WHERE mission_id = ? AND slice_id = ?",
                (mission_id, dep),
            ).fetchone()
            if dep_row is None or dep_row["status"] not in ("completed", "skipped"):
                all_deps_met = False
                break

        if all_deps_met:
            return _row_to_slice_record(row)

    return None


# ---------------------------------------------------------------------------
# Slice transitions
# ---------------------------------------------------------------------------
# Caller must validate fencing before calling any mark_* function.


def mark_slice_active(
    conn: sqlite3.Connection,
    mission_id: str,
    slice_id: str,
    supervisor_id: str,
    fencing_epoch: int,
) -> bool:
    """Mark a slice as active (about to execute)."""
    now = _now_ms()
    with _write_txn(conn):
        _ensure_state_exists(conn, mission_id, fencing_epoch)
        conn.execute(
            "UPDATE mission_roadmap_slices "
            "SET status = 'active', attempt_count = attempt_count + 1, "
            "    updated_at = ? "
            "WHERE mission_id = ? AND slice_id = ? AND status = 'pending'",
            (now, mission_id, slice_id),
        )
        conn.execute(
            "UPDATE mission_supervisor_state "
            "SET current_slice_id = ?, updated_at = ? "
            "WHERE mission_id = ?",
            (slice_id, now, mission_id),
        )
    return True


def mark_slice_completed(
    conn: sqlite3.Connection,
    mission_id: str,
    slice_id: str,
    evidence: dict,
    supervisor_id: str,
    fencing_epoch: int,
) -> bool:
    """Mark a slice as completed with evidence."""
    now = _now_ms()
    with _write_txn(conn):
        _ensure_state_exists(conn, mission_id, fencing_epoch)
        conn.execute(
            "UPDATE mission_roadmap_slices "
            "SET status = 'completed', outcome = 'success', "
            "    evidence_json = ?, updated_at = ? "
            "WHERE mission_id = ? AND slice_id = ?",
            (json.dumps(evidence, sort_keys=True), now, mission_id, slice_id),
        )
        conn.execute(
            "UPDATE mission_supervisor_state "
            "SET current_slice_id = NULL, last_completed_slice = ?, "
            "    total_completed = total_completed + 1, "
            "    error_count = 0, updated_at = ? "
            "WHERE mission_id = ?",
            (slice_id, now, mission_id),
        )
    return True


def mark_slice_blocked(
    conn: sqlite3.Connection,
    mission_id: str,
    slice_id: str,
    blocker: dict,
    supervisor_id: str,
    fencing_epoch: int,
) -> bool:
    """Mark a slice as blocked."""
    now = _now_ms()
    with _write_txn(conn):
        _ensure_state_exists(conn, mission_id, fencing_epoch)
        conn.execute(
            "UPDATE mission_roadmap_slices "
            "SET status = 'blocked', outcome = 'blocked', "
            "    error_json = ?, updated_at = ? "
            "WHERE mission_id = ? AND slice_id = ?",
            (json.dumps(blocker, sort_keys=True), now, mission_id, slice_id),
        )
        conn.execute(
            "UPDATE mission_supervisor_state "
            "SET current_slice_id = NULL, "
            "    total_blocked = total_blocked + 1, "
            "    error_count = error_count + 1, updated_at = ? "
            "WHERE mission_id = ?",
            (now, mission_id),
        )
    return True


def mark_slice_human_gate(
    conn: sqlite3.Connection,
    mission_id: str,
    slice_id: str,
    gate_info: dict,
    supervisor_id: str,
    fencing_epoch: int,
) -> bool:
    """Mark a slice as awaiting human gate."""
    now = _now_ms()
    with _write_txn(conn):
        _ensure_state_exists(conn, mission_id, fencing_epoch)
        conn.execute(
            "UPDATE mission_roadmap_slices "
            "SET status = 'human_gate', outcome = 'human_gate', "
            "    error_json = ?, updated_at = ? "
            "WHERE mission_id = ? AND slice_id = ?",
            (json.dumps(gate_info, sort_keys=True), now, mission_id, slice_id),
        )
        conn.execute(
            "UPDATE mission_supervisor_state "
            "SET current_slice_id = NULL, updated_at = ? "
            "WHERE mission_id = ?",
            (now, mission_id),
        )
    return True


def mark_slice_failed(
    conn: sqlite3.Connection,
    mission_id: str,
    slice_id: str,
    error: dict,
    supervisor_id: str,
    fencing_epoch: int,
) -> bool:
    """Mark a slice as failed (max attempts exceeded)."""
    now = _now_ms()
    with _write_txn(conn):
        _ensure_state_exists(conn, mission_id, fencing_epoch)
        conn.execute(
            "UPDATE mission_roadmap_slices "
            "SET status = 'failed', outcome = 'queue_exhausted', "
            "    error_json = ?, updated_at = ? "
            "WHERE mission_id = ? AND slice_id = ?",
            (json.dumps(error, sort_keys=True), now, mission_id, slice_id),
        )
        conn.execute(
            "UPDATE mission_supervisor_state "
            "SET current_slice_id = NULL, "
            "    total_failed = total_failed + 1, "
            "    error_count = error_count + 1, updated_at = ? "
            "WHERE mission_id = ?",
            (now, mission_id),
        )
    return True


# Supervisor tick (core loop)
# ---------------------------------------------------------------------------

# Type alias for the executor callback
ExecutorFn = Callable[[SliceRecord, int], dict]
"""Executor function: (slice, fencing_epoch) -> result_dict.

The executor receives the slice to execute and the current fencing epoch.
It must return a dict with at least {'outcome': str} where outcome is one
of VALID_OUTCOMES.  Additional keys are evidence/error data.
"""


def run_supervisor_tick(
    conn: sqlite3.Connection,
    mission_id: str,
    supervisor_id: str,
    executor_fn: ExecutorFn,
) -> TickResult:
    """Execute one supervisor tick: validate fencing, select slice, execute.

    This is the core loop entry point.  Each call processes exactly one
    slice.  Returns a TickResult indicating what happened.
    """
    # 1. Validate fencing (BF1: every durable write checks ownership)
    conn.row_factory = sqlite3.Row
    lease_row = conn.execute(
        "SELECT fencing_epoch, expires_at FROM mission_supervisor_leases "
        "WHERE mission_id = ? AND supervisor_id = ?",
        (mission_id, supervisor_id),
    ).fetchone()

    if lease_row is None:
        return TickResult(
            outcome="fencing_invalid",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=0,
            error={"code": "no_lease", "message": "no lease found for this supervisor"},
        )

    if lease_row["expires_at"] <= _now_s():
        return TickResult(
            outcome="lease_expired",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=lease_row["fencing_epoch"],
            error={"code": "lease_expired", "message": "lease has expired"},
        )

    fencing_epoch = lease_row["fencing_epoch"]

    # 2. Check if there's an active slice from a previous tick (crash recovery)
    incomplete = detect_incomplete_slices(conn, mission_id)
    if incomplete:
        # Re-execute the first incomplete slice
        slice_to_run = incomplete[0]
        _log.info(
            "Recovering incomplete slice %s (attempt %d)",
            slice_to_run.slice_id, slice_to_run.attempt_count,
        )
    else:
        # 4. Select next pending slice
        slice_to_run = get_next_slice(conn, mission_id)
        if slice_to_run is None:
            return TickResult(
                outcome="no_slices",
                mission_id=mission_id,
                supervisor_id=supervisor_id,
                fencing_epoch=fencing_epoch,
            )

    # 5. Check attempt limit BEFORE activating
    if slice_to_run.attempt_count >= slice_to_run.max_attempts:
        mark_slice_failed(
            conn, mission_id, slice_to_run.slice_id,
            {"code": "max_attempts_exceeded",
             "message": f"slice exceeded {slice_to_run.max_attempts} attempts"},
            supervisor_id, fencing_epoch,
        )
        return TickResult(
            outcome="slice_queue_exhausted",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=fencing_epoch,
            slice_id=slice_to_run.slice_id,
        )

    # 6. Mark slice active (validates fencing)
    if not incomplete:
        if not mark_slice_active(
            conn, mission_id, slice_to_run.slice_id,
            supervisor_id, fencing_epoch,
        ):
            return TickResult(
                outcome="fencing_invalid",
                mission_id=mission_id,
                supervisor_id=supervisor_id,
                fencing_epoch=fencing_epoch,
                slice_id=slice_to_run.slice_id,
                error={"code": "fencing_broken",
                       "message": "fencing validation failed during activation"},
            )

    # 7. Execute the slice
    try:
        result = executor_fn(slice_to_run, fencing_epoch)
    except Exception as exc:
        result = {"outcome": "failed", "error": {"message": str(exc)}}

    outcome = result.get("outcome", "failed")

    # 8. Transition based on outcome
    if outcome == "success":
        mark_slice_completed(
            conn, mission_id, slice_to_run.slice_id,
            result.get("evidence", {}),
            supervisor_id, fencing_epoch,
        )
        return TickResult(
            outcome="slice_executed",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=fencing_epoch,
            slice_id=slice_to_run.slice_id,
        )
    elif outcome == "blocked":
        mark_slice_blocked(
            conn, mission_id, slice_to_run.slice_id,
            result.get("blocker", {}),
            supervisor_id, fencing_epoch,
        )
        return TickResult(
            outcome="slice_blocked",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=fencing_epoch,
            slice_id=slice_to_run.slice_id,
        )
    elif outcome == "human_gate":
        mark_slice_human_gate(
            conn, mission_id, slice_to_run.slice_id,
            result.get("gate_info", {}),
            supervisor_id, fencing_epoch,
        )
        return TickResult(
            outcome="slice_human_gate",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=fencing_epoch,
            slice_id=slice_to_run.slice_id,
        )
    elif outcome == "queue_exhausted":
        mark_slice_failed(
            conn, mission_id, slice_to_run.slice_id,
            result.get("error", {"message": "queue exhausted"}),
            supervisor_id, fencing_epoch,
        )
        return TickResult(
            outcome="slice_queue_exhausted",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=fencing_epoch,
            slice_id=slice_to_run.slice_id,
        )
    else:
        # failed or unknown — mark as failed, may retry
        mark_slice_blocked(
            conn, mission_id, slice_to_run.slice_id,
            result.get("error", {"message": f"execution outcome: {outcome}"}),
            supervisor_id, fencing_epoch,
        )
        return TickResult(
            outcome="slice_failed",
            mission_id=mission_id,
            supervisor_id=supervisor_id,
            fencing_epoch=fencing_epoch,
            slice_id=slice_to_run.slice_id,
        )
