"""Schema and migration contracts for the K9 durable planner importer."""

import sqlite3

import pytest

from hermes_cli import kanban_db as kb


def _table_names(conn):
    return {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def test_fresh_schema_has_import_and_mapping_tables_with_constraints(tmp_path):
    db_path = tmp_path / "fresh.db"
    kb.init_db(db_path=db_path)

    with kb.connect(db_path=db_path) as conn:
        assert {"kanban_planner_imports", "kanban_planner_task_map"} <= _table_names(conn)
        import_pk = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(kanban_planner_imports)")
        }
        mapping_pk = {
            row["name"]: row["pk"]
            for row in conn.execute("PRAGMA table_info(kanban_planner_task_map)")
        }
        assert import_pk["board"] == 1
        assert import_pk["import_id"] == 2
        assert mapping_pk["board"] == 1
        assert mapping_pk["import_id"] == 2
        assert mapping_pk["planner_task_id"] == 3
        assert {row["table"] for row in conn.execute(
            "PRAGMA foreign_key_list(kanban_planner_task_map)"
        )} == {"kanban_planner_imports", "tasks"}


def test_existing_database_migration_is_additive_and_repeatable(tmp_path):
    db_path = tmp_path / "existing.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        conn.execute("DROP TABLE kanban_planner_task_map")
        conn.execute("DROP TABLE kanban_planner_imports")

    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(db_path=db_path)
    kb.init_db(db_path=db_path)

    with kb.connect(db_path=db_path) as conn:
        assert {"kanban_planner_imports", "kanban_planner_task_map"} <= _table_names(conn)


def test_import_and_mapping_keys_reject_duplicates_and_orphans(tmp_path):
    db_path = tmp_path / "constraints.db"
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        conn.execute(
            "INSERT INTO kanban_planner_imports "
            "(board, import_id, schema_version, fingerprint_algorithm, "
            " fingerprint, project_id, status, task_count, anchor_task_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("default", "imp-1", "planner-dag/v1", "alg", "fp", None, "committed", 1, "t1", 1),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO kanban_planner_imports "
                "(board, import_id, schema_version, fingerprint_algorithm, "
                " fingerprint, project_id, status, task_count, anchor_task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("default", "imp-1", "planner-dag/v1", "alg", "other", None, "committed", 1, "t1", 2),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO kanban_planner_task_map "
                "(board, import_id, planner_task_id, task_id) VALUES (?, ?, ?, ?)",
                ("default", "imp-1", "planner-1", "missing-task"),
            )
