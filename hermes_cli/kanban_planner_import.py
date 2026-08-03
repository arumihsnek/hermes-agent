"""Durable, non-executing import of validated planner-dag/v1 envelopes."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Any, Mapping

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_planner import (
    PlannerTaskSpecs,
    PlannerValidationError,
    _canonical_json,
    _validate_envelope,
    materialize_task_specs,
)


FINGERPRINT_ALGORITHM = "planner-dag/v1+canon-json/sha256-v1"


@dataclass(frozen=True)
class PlannerImportResult:
    status: str
    import_id: str
    board: str | None
    fingerprint: str | None = None
    task_map: dict[str, str] | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "import_id": self.import_id,
            "board": self.board,
            "fingerprint": self.fingerprint,
            "task_map": dict(self.task_map or {}),
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class PlannerActivationResult:
    """Durable scheduler projection result for one explicit import."""

    status: str
    import_id: str
    board: str | None
    promoted_task_ids: tuple[str, ...] = ()
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "import_id": self.import_id,
            "board": self.board,
            "promoted_task_ids": list(self.promoted_task_ids),
            "error_code": self.error_code,
        }


class _Rejected(Exception):
    def __init__(self, code: str):
        self.code = code


def _result_rejected(import_id: str, board: str | None, code: str) -> PlannerImportResult:
    return PlannerImportResult(
        status="rejected", import_id=import_id, board=board, error_code=code
    )


def _resolve_destination(board: str | None, project_id: str | None) -> tuple[str, str | None]:
    if not isinstance(board, str) or not board.strip():
        raise _Rejected("BOARD_REQUIRED")
    try:
        normalized_board = kb._normalize_board_slug(board)
    except ValueError:
        raise _Rejected("BOARD_NOT_FOUND")
    if not normalized_board:
        raise _Rejected("BOARD_REQUIRED")
    if normalized_board != kb.DEFAULT_BOARD and not kb.board_exists(normalized_board):
        raise _Rejected("BOARD_NOT_FOUND")

    metadata_project = kb.read_board_metadata(normalized_board).get("project_id")
    metadata_project = str(metadata_project).strip() if metadata_project else None
    if project_id is not None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise _Rejected("PROJECT_NOT_FOUND")
        project_id = project_id.strip()
        if metadata_project and project_id != metadata_project:
            raise _Rejected("PROJECT_SCOPE_CONFLICT")
    else:
        project_id = metadata_project

    if project_id:
        # Do not create an empty projects.db merely to discover that a project
        # is missing. Import validates an existing project and never creates
        # project state as a side effect.
        from hermes_cli import projects_db

        project_db = projects_db.projects_db_path()
        if not project_db.exists():
            raise _Rejected("PROJECT_NOT_FOUND")
        with projects_db.connect_closing(project_db) as project_conn:
            project = projects_db.get_project(project_conn, project_id)
        if project is None or getattr(project, "archived", False):
            raise _Rejected("PROJECT_NOT_FOUND")
        project_id = project.id
    return normalized_board, project_id


def _fingerprint(envelope: Mapping[str, Any]) -> str:
    canonical = _canonical_json(envelope).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _existing_mapping(conn, board: str, import_id: str, planner_ids: list[str]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT planner_task_id, task_id FROM kanban_planner_task_map "
        "WHERE board = ? AND import_id = ?",
        (board, import_id),
    ).fetchall()
    mapping = {row["planner_task_id"]: row["task_id"] for row in rows}
    if set(mapping) != set(planner_ids):
        raise RuntimeError("planner import mapping is incomplete")
    return {planner_id: mapping[planner_id] for planner_id in planner_ids}


def _allocate_task_ids(conn, specs: PlannerTaskSpecs) -> dict[str, str]:
    allocated: dict[str, str] = {}
    used: set[str] = set()
    for task in specs.tasks:
        task_id = kb._new_task_id()
        while task_id in used or conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            task_id = kb._new_task_id()
        used.add(task_id)
        allocated[task.id] = task_id
    return allocated


def import_planner_envelope(
    conn,
    envelope: Mapping[str, Any],
    *,
    import_id: str,
    board: str | None,
    project_id: str | None = None,
    created_by: str = "planner-import",
) -> PlannerImportResult:
    """Import one validated planner graph atomically without dispatching it."""

    normalized_import_id = import_id.strip() if isinstance(import_id, str) else ""
    if not normalized_import_id:
        return _result_rejected("", board, "IMPORT_ID_REQUIRED")
    try:
        normalized_board, normalized_project = _resolve_destination(board, project_id)
        validated = _validate_envelope(envelope)
        specs = materialize_task_specs(validated)
        fingerprint = _fingerprint(validated)
    except PlannerValidationError:
        return _result_rejected(normalized_import_id, board, "PLANNER_INVALID")
    except _Rejected as exc:
        return _result_rejected(normalized_import_id, board, exc.code)
    except Exception:
        return _result_rejected(normalized_import_id, board, "IMPORT_MAPPING_FAILURE")

    planner_ids = [task.id for task in specs.tasks]
    try:
        with kb.write_txn(conn):
            existing = conn.execute(
                "SELECT schema_version, fingerprint_algorithm, fingerprint, project_id "
                "FROM kanban_planner_imports WHERE board = ? AND import_id = ?",
                (normalized_board, normalized_import_id),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["schema_version"] == validated["schema_version"]
                    and existing["fingerprint_algorithm"] == FINGERPRINT_ALGORITHM
                    and existing["fingerprint"] == fingerprint
                    and existing["project_id"] == normalized_project
                )
                if not same:
                    return PlannerImportResult(
                        status="conflict",
                        import_id=normalized_import_id,
                        board=normalized_board,
                        fingerprint=fingerprint,
                        error_code="IMPORT_CONFLICT",
                    )
                mapping = _existing_mapping(conn, normalized_board, normalized_import_id, planner_ids)
                return PlannerImportResult(
                    status="already-imported",
                    import_id=normalized_import_id,
                    board=normalized_board,
                    fingerprint=fingerprint,
                    task_map=mapping,
                    error_code="IMPORT_ALREADY_COMMITTED",
                )

            task_map = _allocate_task_ids(conn, specs)
            now = int(time.time())
            anchor_task_id = task_map[planner_ids[0]]
            conn.execute(
                "INSERT INTO kanban_planner_imports "
                "(board, import_id, schema_version, fingerprint_algorithm, fingerprint, "
                " project_id, status, task_count, anchor_task_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'committed', ?, ?, ?)",
                (
                    normalized_board,
                    normalized_import_id,
                    validated["schema_version"],
                    FINGERPRINT_ALGORITHM,
                    fingerprint,
                    normalized_project,
                    len(specs.tasks),
                    anchor_task_id,
                    now,
                ),
            )
            for task in specs.tasks:
                conn.execute(
                    "INSERT INTO tasks "
                    "(id, title, body, assignee, status, priority, created_by, created_at, "
                    " workspace_kind, workspace_path, project_id) "
                    "VALUES (?, ?, NULL, NULL, 'todo', 0, ?, ?, 'scratch', NULL, ?)",
                    (
                        task_map[task.id],
                        task.title,
                        created_by,
                        now,
                        normalized_project,
                    ),
                )
                conn.execute(
                    "INSERT INTO kanban_planner_task_map "
                    "(board, import_id, planner_task_id, task_id) VALUES (?, ?, ?, ?)",
                    (normalized_board, normalized_import_id, task.id, task_map[task.id]),
                )
                kb._append_event(
                    conn,
                    task_map[task.id],
                    "created",
                    {"source": "planner-import", "import_id": normalized_import_id, "planner_task_id": task.id},
                )
            for link in specs.links:
                parent_id = task_map[link.parent_id]
                child_id = task_map[link.child_id]
                conn.execute(
                    "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent_id, child_id),
                )
                kb._append_event(
                    conn,
                    child_id,
                    "linked",
                    {"source": "planner-import", "import_id": normalized_import_id, "parent": parent_id},
                )
            kb._append_event(
                conn,
                anchor_task_id,
                "planner_imported",
                {
                    "import_id": normalized_import_id,
                    "schema_version": validated["schema_version"],
                    "fingerprint_algorithm": FINGERPRINT_ALGORITHM,
                    "fingerprint": fingerprint,
                    "board": normalized_board,
                    "project_id": normalized_project,
                    "planner_task_ids": planner_ids,
                },
            )
    except Exception:
        return PlannerImportResult(
            status="failed",
            import_id=normalized_import_id,
            board=normalized_board,
            fingerprint=fingerprint,
            error_code="IMPORT_DB_FAILURE",
        )

    return PlannerImportResult(
        status="created",
        import_id=normalized_import_id,
        board=normalized_board,
        fingerprint=fingerprint,
        task_map=task_map,
    )


def activate_planner_import(
    conn,
    *,
    import_id: str,
    board: str | None,
) -> PlannerActivationResult:
    """Promote only this import's dependency frontier to ``ready``.

    Activation is an explicit scheduler boundary. It never claims, spawns, or
    dispatches a task and is idempotent after the frontier has been promoted.
    Parent completion is read from durable task status, so a new process can
    reconstruct the same frontier without in-memory planner state.
    """
    normalized_import_id = import_id.strip() if isinstance(import_id, str) else ""
    if not normalized_import_id:
        return PlannerActivationResult("rejected", "", board, error_code="IMPORT_ID_REQUIRED")
    try:
        normalized_board, _ = _resolve_destination(board, None)
    except _Rejected as exc:
        return PlannerActivationResult("rejected", normalized_import_id, board, error_code=exc.code)
    row = conn.execute(
        "SELECT status FROM kanban_planner_imports WHERE board = ? AND import_id = ?",
        (normalized_board, normalized_import_id),
    ).fetchone()
    if row is None:
        return PlannerActivationResult(
            "rejected", normalized_import_id, normalized_board, error_code="IMPORT_NOT_FOUND"
        )
    if row["status"] != "committed":
        return PlannerActivationResult(
            "rejected", normalized_import_id, normalized_board, error_code="IMPORT_NOT_COMMITTED"
        )

    promoted: list[str] = []
    try:
        with kb.write_txn(conn):
            mappings = conn.execute(
                "SELECT planner_task_id, task_id FROM kanban_planner_task_map "
                "WHERE board = ? AND import_id = ? ORDER BY planner_task_id",
                (normalized_board, normalized_import_id),
            ).fetchall()
            for mapping in mappings:
                task_id = mapping["task_id"]
                task = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if task is None or task["status"] != "todo":
                    continue
                parents = conn.execute(
                    "SELECT parent.status FROM task_links link "
                    "JOIN tasks parent ON parent.id = link.parent_id "
                    "WHERE link.child_id = ? ORDER BY link.parent_id",
                    (task_id,),
                ).fetchall()
                if not all(parent["status"] in {"done", "archived"} for parent in parents):
                    continue
                changed = conn.execute(
                    "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                    (task_id,),
                ).rowcount
                if changed:
                    kb._append_event(
                        conn,
                        task_id,
                        "promoted",
                        {"source": "planner-activate", "import_id": normalized_import_id},
                    )
                    promoted.append(task_id)
    except Exception:
        return PlannerActivationResult(
            "failed", normalized_import_id, normalized_board, error_code="ACTIVATION_DB_FAILURE"
        )
    return PlannerActivationResult(
        "activated" if promoted else "already-active",
        normalized_import_id,
        normalized_board,
        tuple(promoted),
    )
