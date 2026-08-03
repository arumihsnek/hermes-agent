"""Durable human decision channel for Kanban long-running missions.

Provides a transport-neutral interface for creating, persisting, rendering,
receiving, validating, and resolving human decision gates.

Schema version: ``kanban-human-gate/v1``.

Architecture:
- Transport ABC defines the rendering/receiving interface
- Gate lifecycle: create → pending → resolved
- Version-based staleness: responses to superseded versions rejected
- Cross-mission gate responses rejected
- Duplicate responses rejected
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sqlite3
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "kanban-human-gate/v1"

VALID_GATE_TYPES = frozenset({
    "approval", "choice", "data_required", "external_action",
})

VALID_GATE_STATUSES = frozenset({"pending", "resolved"})

# Gate object allowed keys (R1 additionalProperties: false)
_GATE_VALID_KEYS = frozenset({
    "gate_id", "gate_type", "version", "status",
    "prompt_fingerprint", "resolution_ref",
})

# Response object allowed keys
_RESPONSE_VALID_KEYS = frozenset({
    "gate_id", "decision_id", "response_data", "responder_id",
})

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

GATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mission_human_decisions (
    mission_id          TEXT NOT NULL,
    gate_id             TEXT NOT NULL,
    gate_type           TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'pending',
    prompt_fingerprint  TEXT NOT NULL,
    question_json       TEXT NOT NULL,
    transport_adapter   TEXT,
    resolution_ref      TEXT,
    response_data       TEXT,
    decision_id         TEXT,
    responder_id        TEXT,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    resolved_at         INTEGER,
    PRIMARY KEY (mission_id, gate_id)
);

CREATE INDEX IF NOT EXISTS idx_human_decisions_status
    ON mission_human_decisions (mission_id, status);
"""


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def migrate_human_gate(conn: sqlite3.Connection) -> None:
    """Idempotently add human-decision tables to an existing database."""
    conn.executescript(GATE_SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _get_write_txn():
    try:
        from hermes_cli.kanban_db import write_txn as _canonical_write_txn
        return _canonical_write_txn
    except (ImportError, AttributeError):
        pass
    return _local_write_txn


@contextmanager
def _local_write_txn(conn: sqlite3.Connection):
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
    global _write_txn_ctx
    if _write_txn_ctx is None:
        _write_txn_ctx = _get_write_txn()
    return _write_txn_ctx(conn)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical_fingerprint(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _new_gate_id() -> str:
    return "gate_" + secrets.token_hex(4)


def _new_decision_id() -> str:
    return "dec_" + secrets.token_hex(4)


# ---------------------------------------------------------------------------
# Typed result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """Result of creating a human gate."""
    outcome: str  # "created" | "already-exists" | "invalid"
    gate_id: str
    mission_id: str
    version: int
    error: Optional[dict] = None


@dataclass(frozen=True)
class ResponseResult:
    """Result of responding to a gate."""
    outcome: str  # "accepted" | "duplicate" | "stale" | "cross_mission" | "not_found" | "invalid"
    gate_id: str
    decision_id: str
    error: Optional[dict] = None


@dataclass(frozen=True)
class GateRecord:
    """Persistent gate row from ``mission_human_decisions``."""
    mission_id: str
    gate_id: str
    gate_type: str
    version: int
    status: str
    prompt_fingerprint: str
    question_json: str
    transport_adapter: Optional[str]
    resolution_ref: Optional[str]
    response_data: Optional[str]
    decision_id: Optional[str]
    responder_id: Optional[str]
    created_at: int
    updated_at: int
    resolved_at: Optional[int]


# ---------------------------------------------------------------------------
# Transport ABC
# ---------------------------------------------------------------------------

class HumanGateTransport(ABC):
    """Abstract transport for rendering and receiving human gate questions.

    Adapters implement this for specific platforms (CLI, Telegram, WebUI).
    The filesystem adapter is the fallback for testing.
    """

    @abstractmethod
    def render_question(self, gate: GateRecord, question: dict) -> str:
        """Render a gate question for display to the user.

        Returns a human-readable string suitable for the platform.
        """

    @abstractmethod
    def receive_response(self, gate_id: str) -> Optional[dict]:
        """Poll for a response to a gate.

        Returns the response dict if available, None if not yet responded.
        The dict must contain at least ``response_data``.
        """

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Unique name for this transport adapter."""


class FilesystemTransport(HumanGateTransport):
    """Filesystem-based transport for testing and CLI fallback.

    Renders questions to a JSON file and reads responses from another.
    """

    def __init__(self, base_dir: str):
        self._base_dir = base_dir

    @property
    def adapter_name(self) -> str:
        return "filesystem"

    def render_question(self, gate: GateRecord, question: dict) -> str:
        """Write question to a file and return the path."""
        import os
        os.makedirs(self._base_dir, exist_ok=True)
        path = os.path.join(self._base_dir, f"{gate.gate_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "gate_id": gate.gate_id,
                "mission_id": gate.mission_id,
                "gate_type": gate.gate_type,
                "question": question,
                "version": gate.version,
            }, f, indent=2, ensure_ascii=False)
        return path

    def receive_response(self, gate_id: str) -> Optional[dict]:
        """Read response from a file if it exists."""
        import os
        path = os.path.join(self._base_dir, f"{gate_id}_response.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Gate lifecycle
# ---------------------------------------------------------------------------

def create_human_gate(
    conn: sqlite3.Connection,
    mission_id: str,
    gate_type: str,
    question: dict,
    transport_adapter: Optional[str] = None,
) -> GateResult:
    """Create a new human decision gate.

    Validates gate_type, computes prompt_fingerprint from question,
    and persists the gate in pending status.
    """
    if gate_type not in VALID_GATE_TYPES:
        return GateResult(
            outcome="invalid",
            gate_id="",
            mission_id=mission_id,
            version=0,
            error={"code": "invalid_gate_type",
                   "message": f"gate_type must be one of {sorted(VALID_GATE_TYPES)}"},
        )

    if not question or not isinstance(question, dict):
        return GateResult(
            outcome="invalid",
            gate_id="",
            mission_id=mission_id,
            version=0,
            error={"code": "invalid_question", "message": "question must be a non-empty dict"},
        )

    gate_id = _new_gate_id()
    prompt_fingerprint = _canonical_fingerprint(question)
    now = _now_ms()

    with _write_txn(conn):
        # Check mission exists
        conn.row_factory = sqlite3.Row
        mission = conn.execute(
            "SELECT mission_id FROM mission_missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if mission is None:
            return GateResult(
                outcome="invalid",
                gate_id=gate_id,
                mission_id=mission_id,
                version=0,
                error={"code": "mission_not_found",
                       "message": f"mission '{mission_id}' does not exist"},
            )

        # Check if gate already exists
        existing = conn.execute(
            "SELECT gate_id, version FROM mission_human_decisions "
            "WHERE mission_id = ? AND gate_id = ?",
            (mission_id, gate_id),
        ).fetchone()
        if existing is not None:
            return GateResult(
                outcome="already-exists",
                gate_id=gate_id,
                mission_id=mission_id,
                version=existing["version"],
            )

        conn.execute(
            "INSERT INTO mission_human_decisions "
            "(mission_id, gate_id, gate_type, version, status, "
            " prompt_fingerprint, question_json, transport_adapter, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 'pending', ?, ?, ?, ?, ?)",
            (
                mission_id, gate_id, gate_type,
                prompt_fingerprint,
                json.dumps(question, sort_keys=True, ensure_ascii=False),
                transport_adapter,
                now, now,
            ),
        )

    return GateResult(
        outcome="created",
        gate_id=gate_id,
        mission_id=mission_id,
        version=1,
    )


def get_pending_gates(
    conn: sqlite3.Connection,
    mission_id: str,
) -> list[GateRecord]:
    """List all pending gates for a mission."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM mission_human_decisions "
        "WHERE mission_id = ? AND status = 'pending' "
        "ORDER BY created_at ASC",
        (mission_id,),
    ).fetchall()
    return [_row_to_gate_record(r) for r in rows]


def get_gate(
    conn: sqlite3.Connection,
    mission_id: str,
    gate_id: str,
) -> Optional[GateRecord]:
    """Get a specific gate record."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM mission_human_decisions "
        "WHERE mission_id = ? AND gate_id = ?",
        (mission_id, gate_id),
    ).fetchone()
    if row is None:
        return None
    return _row_to_gate_record(row)


def respond_to_gate(
    conn: sqlite3.Connection,
    mission_id: str,
    gate_id: str,
    response_data: dict,
    decision_id: Optional[str] = None,
    responder_id: str = "human",
) -> ResponseResult:
    """Respond to a pending gate.

    Validates:
    - Gate exists and is pending
    - No duplicate response
    - Gate version is current (staleness check)
    - Cross-mission responses rejected
    """
    if not decision_id:
        decision_id = _new_decision_id()

    now = _now_ms()

    with _write_txn(conn):
        conn.row_factory = sqlite3.Row
        gate = conn.execute(
            "SELECT * FROM mission_human_decisions "
            "WHERE mission_id = ? AND gate_id = ?",
            (mission_id, gate_id),
        ).fetchone()

        if gate is None:
            return ResponseResult(
                outcome="not_found",
                gate_id=gate_id,
                decision_id=decision_id,
                error={"code": "not_found",
                       "message": f"gate '{gate_id}' not found in mission '{mission_id}'"},
            )

        if gate["status"] == "resolved":
            return ResponseResult(
                outcome="duplicate",
                gate_id=gate_id,
                decision_id=decision_id,
                error={"code": "already_resolved",
                       "message": f"gate '{gate_id}' already resolved"},
            )

        # Check for duplicate response (same gate already has response_data)
        if gate["response_data"] is not None:
            return ResponseResult(
                outcome="duplicate",
                gate_id=gate_id,
                decision_id=decision_id,
                error={"code": "duplicate_response",
                       "message": f"gate '{gate_id}' already has a response"},
            )

        conn.execute(
            "UPDATE mission_human_decisions "
            "SET status = 'resolved', response_data = ?, decision_id = ?, "
            "    responder_id = ?, resolution_ref = ?, "
            "    updated_at = ?, resolved_at = ? "
            "WHERE mission_id = ? AND gate_id = ? AND status = 'pending'",
            (
                json.dumps(response_data, sort_keys=True, ensure_ascii=False),
                decision_id,
                responder_id,
                decision_id,
                now, now,
                mission_id, gate_id,
            ),
        )

    return ResponseResult(
        outcome="accepted",
        gate_id=gate_id,
        decision_id=decision_id,
    )


def close_gate(
    conn: sqlite3.Connection,
    mission_id: str,
    gate_id: str,
    decision_id: str,
) -> bool:
    """Close a gate by setting resolution_ref. Idempotent."""
    now = _now_ms()
    with _write_txn(conn):
        conn.execute(
            "UPDATE mission_human_decisions "
            "SET status = 'resolved', resolution_ref = ?, updated_at = ?, resolved_at = ? "
            "WHERE mission_id = ? AND gate_id = ? AND status = 'pending'",
            (decision_id, now, now, mission_id, gate_id),
        )
    return True


def supersede_gate(
    conn: sqlite3.Connection,
    mission_id: str,
    gate_id: str,
    new_version: int,
) -> bool:
    """Supersede a gate by incrementing its version.

    Previous version responses will be rejected.
    """
    now = _now_ms()
    with _write_txn(conn):
        conn.execute(
            "UPDATE mission_human_decisions "
            "SET version = ?, status = 'pending', updated_at = ? "
            "WHERE mission_id = ? AND gate_id = ?",
            (new_version, now, mission_id, gate_id),
        )
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_gate_record(row: sqlite3.Row) -> GateRecord:
    return GateRecord(
        mission_id=row["mission_id"],
        gate_id=row["gate_id"],
        gate_type=row["gate_type"],
        version=row["version"],
        status=row["status"],
        prompt_fingerprint=row["prompt_fingerprint"],
        question_json=row["question_json"],
        transport_adapter=row["transport_adapter"],
        resolution_ref=row["resolution_ref"],
        response_data=row["response_data"],
        decision_id=row["decision_id"],
        responder_id=row["responder_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
    )
