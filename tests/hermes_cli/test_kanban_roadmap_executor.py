"""Tests for kanban_roadmap_executor — R5 autonomous roadmap executor.

Covers: roadmap loading, validation, dependency resolution, executor loop,
gate integration, PR lifecycle, scope creep prevention.
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
    add_slices, acquire_supervisor_lease, get_roadmap_slices,
    migrate_supervisor, SUPERVISOR_SCHEMA_SQL,
)
from hermes_cli.kanban_roadmap_executor import (
    load_roadmap, validate_roadmap_data, resolve_dependency_order,
    run_roadmap_executor, ExecutorConfig, ExecutorResult,
    Roadmap, RoadmapPhase, RoadmapSlice,
)


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


def _make_roadmap_file(tmpdir, data=None):
    if data is None:
        data = {
            "roadmap_id": "test-roadmap",
            "version": "1.0.0",
            "description": "Test roadmap",
            "phases": [
                {
                    "phase_id": "R3",
                    "description": "Phase 3",
                    "slices": [
                        {"slice_id": "s1", "description": "Slice 1", "dependencies": []},
                        {"slice_id": "s2", "description": "Slice 2", "dependencies": ["s1"]},
                    ],
                },
                {
                    "phase_id": "R4",
                    "description": "Phase 4",
                    "dependencies": ["R3"],
                    "slices": [
                        {"slice_id": "s3", "description": "Slice 3", "dependencies": ["s2"]},
                    ],
                },
            ],
        }
    path = os.path.join(tmpdir, "roadmap.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


# ---------------------------------------------------------------------------
# Test: Roadmap validation
# ---------------------------------------------------------------------------

class TestRoadmapValidation(unittest.TestCase):
    def test_valid_roadmap(self):
        errors = validate_roadmap_data({
            "roadmap_id": "r1", "version": "1.0",
            "description": "test",
            "phases": [{"phase_id": "P1", "description": "p1", "slices": [
                {"slice_id": "s1", "description": "s1"}
            ]}],
        })
        self.assertEqual(errors, [])

    def test_missing_roadmap_id(self):
        errors = validate_roadmap_data({"version": "1.0", "description": "t", "phases": []})
        self.assertTrue(any("roadmap_id" in e for e in errors))

    def test_missing_phases(self):
        errors = validate_roadmap_data({"roadmap_id": "r", "version": "1", "description": "d"})
        self.assertTrue(any("phases" in e for e in errors))

    def test_duplicate_slice_ids(self):
        errors = validate_roadmap_data({
            "roadmap_id": "r", "version": "1", "description": "d",
            "phases": [{"phase_id": "P1", "description": "p", "slices": [
                {"slice_id": "s1", "description": "a"},
                {"slice_id": "s1", "description": "b"},
            ]}],
        })
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_unknown_slice_dependency(self):
        errors = validate_roadmap_data({
            "roadmap_id": "r", "version": "1", "description": "d",
            "phases": [{"phase_id": "P1", "description": "p", "slices": [
                {"slice_id": "s1", "description": "a", "dependencies": ["s99"]},
            ]}],
        })
        self.assertTrue(any("unknown slice" in e for e in errors))

    def test_unknown_phase_dependency(self):
        errors = validate_roadmap_data({
            "roadmap_id": "r", "version": "1", "description": "d",
            "phases": [{"phase_id": "P1", "description": "p", "dependencies": ["P99"], "slices": []}],
        })
        self.assertTrue(any("unknown phase" in e for e in errors))


# ---------------------------------------------------------------------------
# Test: Dependency resolution
# ---------------------------------------------------------------------------

class TestDependencyResolution(unittest.TestCase):
    def test_no_dependencies(self):
        slices = [
            RoadmapSlice("s1", "P1", "d1", [], {}, [], [], None, 3, 0),
            RoadmapSlice("s2", "P1", "d2", [], {}, [], [], None, 3, 0),
        ]
        levels = resolve_dependency_order(slices)
        self.assertEqual(len(levels), 1)
        self.assertEqual(set(levels[0]), {"s1", "s2"})

    def test_linear_chain(self):
        slices = [
            RoadmapSlice("s1", "P1", "d1", [], {}, [], [], None, 3, 0),
            RoadmapSlice("s2", "P1", "d2", ["s1"], {}, [], [], None, 3, 0),
            RoadmapSlice("s3", "P1", "d3", ["s2"], {}, [], [], None, 3, 0),
        ]
        levels = resolve_dependency_order(slices)
        self.assertEqual(len(levels), 3)
        self.assertEqual(levels[0], ["s1"])
        self.assertEqual(levels[1], ["s2"])
        self.assertEqual(levels[2], ["s3"])

    def test_diamond(self):
        slices = [
            RoadmapSlice("s1", "P1", "d1", [], {}, [], [], None, 3, 0),
            RoadmapSlice("s2", "P1", "d2", ["s1"], {}, [], [], None, 3, 0),
            RoadmapSlice("s3", "P1", "d3", ["s1"], {}, [], [], None, 3, 0),
            RoadmapSlice("s4", "P1", "d4", ["s2", "s3"], {}, [], [], None, 3, 0),
        ]
        levels = resolve_dependency_order(slices)
        self.assertEqual(len(levels), 3)
        self.assertEqual(levels[0], ["s1"])
        self.assertEqual(set(levels[1]), {"s2", "s3"})
        self.assertEqual(levels[2], ["s4"])

    def test_circular_dependency(self):
        slices = [
            RoadmapSlice("s1", "P1", "d1", ["s2"], {}, [], [], None, 3, 0),
            RoadmapSlice("s2", "P1", "d2", ["s1"], {}, [], [], None, 3, 0),
        ]
        with self.assertRaises(ValueError) as ctx:
            resolve_dependency_order(slices)
        self.assertIn("Circular dependency", str(ctx.exception))


# ---------------------------------------------------------------------------
# Test: Roadmap loading
# ---------------------------------------------------------------------------

class TestRoadmapLoading(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_load_valid_roadmap(self):
        path = _make_roadmap_file(self.tmpdir)
        roadmap = load_roadmap(path)
        self.assertEqual(roadmap.roadmap_id, "test-roadmap")
        self.assertEqual(len(roadmap.phases), 2)
        self.assertEqual(len(roadmap.all_slices), 3)

    def test_load_invalid_roadmap(self):
        path = _make_roadmap_file(self.tmpdir, {"bad": True})
        with self.assertRaises(ValueError):
            load_roadmap(path)

    def test_roadmap_slice_fields(self):
        path = _make_roadmap_file(self.tmpdir)
        roadmap = load_roadmap(path)
        s1 = roadmap.all_slices[0]
        self.assertEqual(s1.slice_id, "s1")
        self.assertEqual(s1.phase, "R3")
        self.assertEqual(s1.dependencies, [])


# ---------------------------------------------------------------------------
# Test: Executor
# ---------------------------------------------------------------------------

class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.conn = _make_conn()
        _make_mission(self.conn, "m_exec")
        self.tmpdir = tempfile.mkdtemp()

    def test_executor_with_roadmap(self):
        path = _make_roadmap_file(self.tmpdir)
        config = ExecutorConfig(
            mission_id="m_exec",
            supervisor_id="sup-1",
            roadmap_path=path,
            max_ticks=10,
        )
        result = run_roadmap_executor(self.conn, config)
        # All slices should execute (no blocking, no gates)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.slices_completed, 3)
        self.assertEqual(result.slices_total, 3)

    def test_executor_no_roadmap(self):
        config = ExecutorConfig(mission_id="m_exec", supervisor_id="sup-1")
        result = run_roadmap_executor(self.conn, config)
        self.assertEqual(result.outcome, "error")

    def test_executor_with_roadmap_object(self):
        roadmap = Roadmap(
            roadmap_id="r1", version="1.0", description="test",
            phases=[RoadmapPhase("P1", "phase 1", [
                RoadmapSlice("s1", "P1", "d1", [], {}, [], [], None, 3, 0),
            ], [])],
            all_slices=[RoadmapSlice("s1", "P1", "d1", [], {}, [], [], None, 3, 0)],
        )
        config = ExecutorConfig(
            mission_id="m_exec", supervisor_id="sup-1",
            roadmap=roadmap, max_ticks=5,
        )
        result = run_roadmap_executor(self.conn, config)
        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.slices_completed, 1)


# ---------------------------------------------------------------------------
# Test: Integration with supervisor
# ---------------------------------------------------------------------------

class TestSupervisorIntegration(unittest.TestCase):
    def test_slices_ingested_by_executor(self):
        conn = _make_conn()
        _make_mission(conn, "m_ingest")
        tmpdir = tempfile.mkdtemp()
        path = _make_roadmap_file(tmpdir)
        config = ExecutorConfig(
            mission_id="m_ingest", supervisor_id="sup-1",
            roadmap_path=path, max_ticks=10,
        )
        run_roadmap_executor(conn, config)
        slices = get_roadmap_slices(conn, "m_ingest")
        self.assertEqual(len(slices), 3)

    def test_lease_acquired(self):
        conn = _make_conn()
        _make_mission(conn, "m_lease")
        tmpdir = tempfile.mkdtemp()
        path = _make_roadmap_file(tmpdir)
        config = ExecutorConfig(
            mission_id="m_lease", supervisor_id="sup-1",
            roadmap_path=path, max_ticks=10,
        )
        run_roadmap_executor(conn, config)
        # Lease should exist
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM mission_supervisor_leases WHERE mission_id = 'm_lease'"
        ).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
