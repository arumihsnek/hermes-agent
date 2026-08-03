"""Tests for kanban_supervisor hardening — R7 adoption and hardening.

Covers: backoff computation, error fingerprinting, loop protection,
fresh install, real mission test.
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

from hermes_cli.kanban_mission_state import create_mission, SCHEMA_VERSION
from hermes_cli.kanban_supervisor import (
    SUPERVISOR_SCHEMA_SQL, migrate_supervisor,
    add_slices, acquire_supervisor_lease, release_supervisor_lease,
    get_roadmap_slices, get_next_slice,
    run_supervisor_tick, recover_supervisor_state,
    validate_slice,
    DEFAULT_MAX_ATTEMPTS, MAX_CORRECTIONS_PER_FINDING,
    MAX_IDENTICAL_FAILURES, MAX_TICKS_PER_SESSION,
    BACKOFF_BASE_S, BACKOFF_MAX_S,
    _compute_backoff, _fingerprint_error,
)
from hermes_cli.kanban_mission_state import canonical_fingerprint


def _make_conn():
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
    return conn


def _make_mission(conn, mission_id="m_test"):
    state = {
        "document_type": "mission_state", "schema_version": SCHEMA_VERSION,
        "mission_id": mission_id, "generation": 0, "status": "active",
        "phase": "execution",
        "identity": {
            "board": "test", "tenant": "default", "repository": "test/repo",
            "branch": "main", "base_sha": "a" * 40, "head_sha": "b" * 40,
            "tree_sha": "c" * 40, "plan_fingerprint": "d" * 64,
            "checkpoint_fingerprint": "e" * 64,
        },
        "planner_import": None, "card_ref": None, "execution_ref": None,
        "evidence_refs": [], "decision_refs": [], "consultation_refs": [],
        "active_blocker": None, "active_human_gate": None,
        "queue_exhausted": None, "completion": None,
        "next_safe_action": None, "last_operation": None,
    }
    result = create_mission(conn, state=state, operation_id="op_create")
    assert result.outcome == "created"


# ---------------------------------------------------------------------------
# Test: Backoff computation
# ---------------------------------------------------------------------------

class TestBackoffComputation(unittest.TestCase):
    def test_zero_attempt(self):
        delay = _compute_backoff(0)
        self.assertEqual(delay, BACKOFF_BASE_S)

    def test_exponential_growth(self):
        d0 = _compute_backoff(0)
        d1 = _compute_backoff(1)
        d2 = _compute_backoff(2)
        self.assertEqual(d1, d0 * 2)
        self.assertEqual(d2, d0 * 4)

    def test_max_cap(self):
        delay = _compute_backoff(100)
        self.assertEqual(delay, BACKOFF_MAX_S)

    def test_backoff_positive(self):
        for i in range(10):
            self.assertGreater(_compute_backoff(i), 0)


# ---------------------------------------------------------------------------
# Test: Error fingerprinting
# ---------------------------------------------------------------------------

class TestErrorFingerprinting(unittest.TestCase):
    def test_same_errors_same_fingerprint(self):
        e1 = {"code": "timeout", "message": "took too long"}
        e2 = {"code": "timeout", "message": "took too long"}
        self.assertEqual(_fingerprint_error(e1), _fingerprint_error(e2))

    def test_different_errors_different_fingerprint(self):
        e1 = {"code": "timeout"}
        e2 = {"code": "connection_refused"}
        self.assertNotEqual(_fingerprint_error(e1), _fingerprint_error(e2))

    def test_string_error(self):
        fp = _fingerprint_error("simple error")
        self.assertIsInstance(fp, str)


# ---------------------------------------------------------------------------
# Test: Constants are reasonable
# ---------------------------------------------------------------------------

class TestHardeningConstants(unittest.TestCase):
    def test_max_attempts_positive(self):
        self.assertGreater(DEFAULT_MAX_ATTEMPTS, 0)

    def test_max_corrections_positive(self):
        self.assertGreater(MAX_CORRECTIONS_PER_FINDING, 0)

    def test_max_identical_failures_positive(self):
        self.assertGreater(MAX_IDENTICAL_FAILURES, 0)

    def test_backoff_positive(self):
        self.assertGreater(BACKOFF_BASE_S, 0)
        self.assertGreater(BACKOFF_MAX_S, BACKOFF_BASE_S)

    def test_max_ticks_reasonable(self):
        self.assertGreater(MAX_TICKS_PER_SESSION, 10)


# ---------------------------------------------------------------------------
# Test: Fresh install
# ---------------------------------------------------------------------------

class TestFreshInstall(unittest.TestCase):
    def test_migration_on_fresh_db(self):
        conn = _make_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        self.assertIn("mission_missions", table_names)
        self.assertIn("mission_journal", table_names)
        self.assertIn("mission_supervisor_leases", table_names)
        self.assertIn("mission_roadmap_slices", table_names)
        self.assertIn("mission_supervisor_state", table_names)

    def test_migration_idempotent(self):
        conn = _make_conn()
        migrate_supervisor(conn)  # Run again
        # Should not raise

    def test_create_mission_on_fresh_db(self):
        conn = _make_conn()
        _make_mission(conn, "m_fresh")
        from hermes_cli.kanban_mission_state import get_mission
        mission = get_mission(conn, "m_fresh")
        self.assertIsNotNone(mission)


# ---------------------------------------------------------------------------
# Test: Real mission (non-destructive)
# ---------------------------------------------------------------------------

class TestRealMission(unittest.TestCase):
    def test_non_destructive_mission(self):
        """Run a real non-destructive mission through the full pipeline."""
        conn = _make_conn()
        _make_mission(conn, "m_real")

        # Ingest slices
        slices = [
            {"slice_id": "step-1", "phase": "setup", "description": "Initialize"},
            {"slice_id": "step-2", "phase": "exec", "description": "Execute",
             "dependencies": ["step-1"]},
            {"slice_id": "step-3", "phase": "verify", "description": "Verify",
             "dependencies": ["step-2"]},
        ]
        result = add_slices(conn, "m_real", slices)
        self.assertEqual(result.outcome, "added")

        # Acquire lease
        lease = acquire_supervisor_lease(conn, "m_real", "sup-real")
        self.assertEqual(lease.outcome, "acquired")

        # Execute all slices
        for _ in range(3):
            result = run_supervisor_tick(
                conn, "m_real", "sup-real",
                lambda s, e: {"outcome": "success", "evidence": {"ok": True}},
            )
            self.assertEqual(result.outcome, "slice_executed")

        # Verify completion
        all_slices = get_roadmap_slices(conn, "m_real")
        completed = [s for s in all_slices if s.status == "completed"]
        self.assertEqual(len(completed), 3)

        # Verify supervisor state
        state = recover_supervisor_state(conn, "m_real")
        self.assertIsNotNone(state)
        self.assertEqual(state.total_completed, 3)
        self.assertEqual(state.total_failed, 0)
        self.assertEqual(state.total_blocked, 0)


# ---------------------------------------------------------------------------
# Test: Rollback safety
# ---------------------------------------------------------------------------

class TestRollbackSafety(unittest.TestCase):
    def test_new_tables_are_additive(self):
        """New tables should not affect existing K9 tables."""
        conn = _make_conn()
        # K9 tables should still work
        conn.execute(
            "INSERT INTO mission_missions (mission_id, schema_version, status, "
            "phase, generation, state_json, state_fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("m_k9", SCHEMA_VERSION, "active", "planning", 0,
             "{}", "abc", 1000, 1000),
        )
        row = conn.execute(
            "SELECT * FROM mission_missions WHERE mission_id = 'm_k9'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_supervisor_tables_separate(self):
        """Supervisor tables should not conflict with R2 tables."""
        conn = _make_conn()
        _make_mission(conn, "m_sep")
        acquire_supervisor_lease(conn, "m_sep", "sup-1")
        # Both R2 and R3 data coexist
        from hermes_cli.kanban_mission_state import get_mission
        mission = get_mission(conn, "m_sep")
        self.assertIsNotNone(mission)
        conn.row_factory = sqlite3.Row
        lease = conn.execute(
            "SELECT * FROM mission_supervisor_leases WHERE mission_id = 'm_sep'"
        ).fetchone()
        self.assertIsNotNone(lease)


if __name__ == "__main__":
    unittest.main()
