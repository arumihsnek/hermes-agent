"""Offline transaction, replay, and conflict tests for durable K9 import."""

import json

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_planner_import import import_planner_envelope


def _envelope():
    return {
        "schema_version": "planner-dag/v1",
        "subtasks": [
            {"id": "build", "title": "Build artifact", "role": "worker"},
            {"id": "review", "title": "Review artifact", "role": "reviewer"},
        ],
        "dependencies": [{"subtask_id": "review", "depends_on": "build"}],
        "roles": {"worker": {"tier": "cheap"}, "reviewer": {"tier": "standard"}},
        "acceptance": {"build": {"required": True}, "review": {"required": True}},
        "review_policy": {"build": {"reviewer_role": "reviewer"}, "review": {}},
        "batch_groups": [],
        "evidence_expectations": {"build": ["artifact"], "review": ["review_ref"]},
        "estimated_model_tier": {"build": "cheap", "review": "standard"},
        "risk_classification": {"build": "low", "review": "medium"},
        "closure_policy": {"allow_partial": False, "required_review_roles": {"build": ["reviewer"]}},
    }


def _counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in (
            "tasks", "task_links", "task_events",
            "kanban_planner_imports", "kanban_planner_task_map",
        )
    }


def test_import_creates_todo_graph_and_durable_audit_without_execution(tmp_path):
    db_path = tmp_path / "import.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        result = import_planner_envelope(conn, _envelope(), import_id="import-1", board="default")
        assert result.status == "created"
        assert len(result.task_map) == 2
        rows = conn.execute("SELECT id, status, workspace_path FROM tasks ORDER BY id").fetchall()
        assert [row["status"] for row in rows] == ["todo", "todo"]
        assert all(row["workspace_path"] is None for row in rows)
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'planner_imported'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'created'").fetchone()[0] == 2


def test_repeating_same_import_is_noop_and_returns_mapping(tmp_path):
    db_path = tmp_path / "replay.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        first = import_planner_envelope(conn, _envelope(), import_id="same", board="default")
        before = _counts(conn)
        second = import_planner_envelope(conn, _envelope(), import_id="same", board="default")
        assert first.status == "created"
        assert second.status == "already-imported"
        assert second.task_map == first.task_map
        assert _counts(conn) == before


def test_same_identity_with_changed_content_is_conflict_without_mutation(tmp_path):
    db_path = tmp_path / "conflict.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        import_planner_envelope(conn, _envelope(), import_id="same", board="default")
        before = _counts(conn)
        changed = _envelope()
        changed["subtasks"][0]["title"] = "Changed artifact"
        result = import_planner_envelope(conn, changed, import_id="same", board="default")
        assert result.status == "conflict"
        assert _counts(conn) == before


def test_canonical_json_key_order_replays_same_import(tmp_path):
    db_path = tmp_path / "canonical.db"
    kb.init_db(db_path=db_path)
    first_envelope = _envelope()
    second_envelope = json.loads(json.dumps(first_envelope, sort_keys=True))
    with kb.connect(db_path=db_path) as conn:
        first = import_planner_envelope(conn, first_envelope, import_id="same", board="default")
        second = import_planner_envelope(conn, second_envelope, import_id="same", board="default")
        assert first.fingerprint == second.fingerprint
        assert second.status == "already-imported"


def test_event_failure_rolls_back_import_graph_and_record(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"
    kb.init_db(db_path=db_path)
    original_append = kb._append_event

    def fail_import_event(conn, task_id, kind, payload=None, **kwargs):
        if kind == "planner_imported":
            raise RuntimeError("injected event failure")
        return original_append(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_import_event)
    with kb.connect(db_path=db_path) as conn:
        result = import_planner_envelope(conn, _envelope(), import_id="rollback", board="default")
        assert result.status == "failed"
        assert _counts(conn) == {"tasks": 0, "task_links": 0, "task_events": 0, "kanban_planner_imports": 0, "kanban_planner_task_map": 0}


def test_missing_explicit_board_is_rejected_before_writes(tmp_path):
    db_path = tmp_path / "destination.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        result = import_planner_envelope(conn, _envelope(), import_id="x", board=None)
        assert result.status == "rejected"
        assert result.error_code == "BOARD_REQUIRED"
        assert _counts(conn)["tasks"] == 0
