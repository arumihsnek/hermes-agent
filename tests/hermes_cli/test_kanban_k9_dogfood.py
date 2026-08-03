"""Offline end-to-end dogfood for the durable K9 planner boundary."""

from __future__ import annotations

import json

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_planner_import import activate_planner_import, import_planner_envelope


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
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("tasks", "task_links", "task_events", "kanban_planner_imports", "kanban_planner_task_map")
    }


def test_k9_dogfood_replay_conflict_activation_and_recovery(tmp_path):
    db_path = tmp_path / "dogfood.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        first = import_planner_envelope(conn, _envelope(), import_id="dogfood", board="default")
        replay = import_planner_envelope(conn, json.loads(json.dumps(_envelope(), sort_keys=True)), import_id="dogfood", board="default")
        changed = _envelope()
        changed["subtasks"][0]["title"] = "Divergent artifact"
        conflict = import_planner_envelope(conn, changed, import_id="dogfood", board="default")
        assert first.status == "created"
        assert replay.status == "already-imported"
        assert conflict.status == "conflict"
        root_id = first.task_map["build"]
        child_id = first.task_map["review"]
        activated = activate_planner_import(conn, import_id="dogfood", board="default")
        assert activated.status == "activated"
        assert activated.promoted_task_ids == (root_id,)
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (child_id,)).fetchone()[0] == "todo"

    # Reopen simulates process restart; completion and frontier reconstruction
    # use only durable SQLite state and never a retained Python planner object.
    with kb.connect(db_path=db_path) as conn:
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (root_id,))
        activated = activate_planner_import(conn, import_id="dogfood", board="default")
        assert activated.promoted_task_ids == (child_id,)
        assert activate_planner_import(conn, import_id="dogfood", board="default").status == "already-active"
        assert _counts(conn) == {"tasks": 2, "task_links": 1, "task_events": 6, "kanban_planner_imports": 1, "kanban_planner_task_map": 2}


def test_k9_dogfood_import_rollback_on_audit_fault(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"
    kb.init_db(db_path=db_path)
    original = kb._append_event

    def fail_import_event(conn, task_id, kind, payload=None, **kwargs):
        if kind == "planner_imported":
            raise RuntimeError("fault injection: audit unavailable")
        return original(conn, task_id, kind, payload, **kwargs)

    monkeypatch.setattr(kb, "_append_event", fail_import_event)
    with kb.connect(db_path=db_path) as conn:
        result = import_planner_envelope(conn, _envelope(), import_id="rollback", board="default")
        assert result.status == "failed"
        assert _counts(conn) == {"tasks": 0, "task_links": 0, "task_events": 0, "kanban_planner_imports": 0, "kanban_planner_task_map": 0}


def test_k9_dogfood_activation_rollback_on_promoted_event_fault(tmp_path, monkeypatch):
    db_path = tmp_path / "activation-rollback.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        imported = import_planner_envelope(conn, _envelope(), import_id="activation-rollback", board="default")
        original = kb._append_event

        def fail_promoted_event(conn, task_id, kind, payload=None, **kwargs):
            if kind == "promoted":
                raise RuntimeError("fault injection: promotion audit unavailable")
            return original(conn, task_id, kind, payload, **kwargs)

        monkeypatch.setattr(kb, "_append_event", fail_promoted_event)
        result = activate_planner_import(conn, import_id="activation-rollback", board="default")
        assert result is not None
        assert conn.execute("SELECT status FROM tasks WHERE id = ?", (imported.task_map["build"],)).fetchone()[0] == "todo"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'promoted'").fetchone()[0] == 0
