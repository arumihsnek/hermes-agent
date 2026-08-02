# Durable planner import v1

Status: accepted architecture for K9 durable import.

Senior architecture gate: `56765da1-95ca-4d08-baa9-08be5aadffbf` (`codex-senior-consult-response/v3`, `accept`).

This contract defines persistence of a validated `planner-dag/v1` envelope. It
does not execute tasks, activate providers, create workspaces, or dispatch
workers.

## API boundary

The importer receives a validated envelope, required non-empty `import_id`,
required explicit existing `board`, and optional `project_id`. The envelope
does not select its destination. The CLI must require `--import-id` and
`--board`; dry-run remains a separate read-only command.

## Identity and replay

- Hermes `tasks.id` remains an internally generated task ID.
- `(board, import_id)` is the durable operation identity.
- The fingerprint is SHA-256 over the complete validated envelope using
  canonical UTF-8 JSON (`sort_keys=true`, compact separators), labelled
  `planner-dag/v1+canon-json/sha256-v1`.
- The importer stores one planner-subtask-to-internal-task mapping per
  `(board, import_id)`.
- Same identity and same algorithm/fingerprint/schema/project returns
  `already-imported` without writes.
- Same identity with any differing identity field returns `conflict` without
  writes.
- A failed transaction leaves no import record, so retry after rollback is a
  fresh `created` attempt. A retry after a lost response returns
  `already-imported`.
- Same content with a different `import_id` is a distinct operation.

Result codes are `created`, `already-imported`, `conflict`, `rejected`, and
`failed`. Stable CLI error codes are `PLANNER_INVALID`, `IMPORT_ID_REQUIRED`,
`BOARD_REQUIRED`, `BOARD_NOT_FOUND`, `PROJECT_NOT_FOUND`,
`PROJECT_SCOPE_CONFLICT`, `IMPORT_CONFLICT`, `IMPORT_ALREADY_COMMITTED`,
`IMPORT_DB_FAILURE`, `IMPORT_SCHEMA_UNSUPPORTED`, and
`IMPORT_MAPPING_FAILURE`.

## Destination and workspace

The board is explicit and must already exist (`default` is the only implicit
historical board, but import still requires the explicit value). The importer
never uses current-board, environment, or default fallback. An optional
project must resolve in the active `projects.db`; an omitted project may
inherit an existing board metadata project. A mismatch, missing project, or
ambiguous destination is rejected. Projects are never created and unresolved
projects are never silently dropped.

Imported tasks persist `workspace_kind='scratch'` and a null
`workspace_path`. Workspace directories and worktrees remain dispatcher-owned
and are not created by import.

## Durable schema migration v1

The additive migration creates these board-local tables:

```sql
CREATE TABLE kanban_planner_imports (
  board TEXT NOT NULL,
  import_id TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  fingerprint_algorithm TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  project_id TEXT,
  status TEXT NOT NULL CHECK(status = 'committed'),
  task_count INTEGER NOT NULL,
  anchor_task_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (board, import_id),
  UNIQUE (board, import_id, fingerprint_algorithm, fingerprint)
);

CREATE TABLE kanban_planner_task_map (
  board TEXT NOT NULL,
  import_id TEXT NOT NULL,
  planner_task_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  PRIMARY KEY (board, import_id, planner_task_id),
  UNIQUE (board, import_id, task_id),
  FOREIGN KEY (board, import_id)
    REFERENCES kanban_planner_imports(board, import_id),
  FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE RESTRICT
);
```

Migration is additive and idempotent for both empty and existing databases.
No real user database is modified by tests.

## Transaction and audit

Validation, canonicalization, explicit destination validation, project
validation, and all mapping decisions happen before `BEGIN IMMEDIATE`. One
`write_txn` then inserts the import record, all tasks, mappings, links, and
events. Any task, mapping, link, uniqueness, foreign-key, or event failure
rolls back every import row.

Tasks emit normal `created` and `linked` events. One `planner_imported` event
is anchored to the first subtask in envelope order and contains only import
identity, schema, algorithm, fingerprint, board, project, and planner IDs.
No envelope body, token, credential, or secret is emitted. The durable source
of truth is `task_events`; post-commit lifecycle hooks are optional observers.

## Lifecycle boundary

All imported tasks start in `todo`, including dependency roots and tasks named
in a completion snapshot. Import never marks tasks done, promotes tasks to
ready, creates a run, invokes `resolve_workspace`, or dispatches a worker.
An explicit later activation/scheduler operation owns frontier calculation and
promotion. Reimport never overwrites progress; changed content under the same
identity is `conflict`.

## Required acceptance tests

The implementation MUST cover:

- empty and existing DB migration, repeated initialization;
- canonical JSON equivalence, same import replay, changed-content conflict,
  and concurrent same-import attempts;
- explicit board/project success, missing/ambiguous/mismatched destination,
  and no project creation or fallback;
- injected failure at task, mapping, link, and event writes with no partial
  tasks, mappings, links, events, or import record;
- all tasks `todo`, no workspace creation, no worker/provider calls, and
  reopen/retry recovery after a lost response;
- dry-run remains DB-free and unchanged;
- scoped K8/K9 regression tests, compilation, diff check, and symlink scan.

## Non-goals

Provider live tests, provider activation, automatic project/workspace
creation, worker dispatch, activation UX, global pytest cleanup, and broad
lifecycle redesign are outside this contract.
