# Kanban Autonomous Supervisor — Current Specification

> Document status: current product specification  
> Spec version: `1.1.0`  
> Runtime contract: `kanban-mission-state/v1` plus additive supervisor,
> human-gate, and roadmap-executor schemas.

## 1. Purpose

The system provides durable orchestration for long-running Kanban missions. It separates:

- mission lifecycle state;
- task/DAG persistence;
- supervisor execution state;
- human decisions;
- roadmap definition and dependency resolution;
- consultation and review evidence.

It must remain resumable without relying on conversation history.

## 2. Non-goals

- Copying the K9 task DAG into mission state.
- A resident coordinator that mutates state without leases.
- Treating `queue_exhausted` as successful completion.
- Depending on a specific messaging transport in the core.
- Writing to the official `NousResearch/hermes-agent` repository.
- Mutating live Hermes data during tests.
- Hiding or normalizing incompatible replay requests.

## 3. Ownership model

| Concern | Owner |
|---|---|
| R1 vocabulary, semantic policy and historical JSON Schema | control plane |
| Runtime mission persistence | `hermes_cli/kanban_mission_state.py` |
| K9 task graph and planner-import persistence | existing Kanban/K9 modules |
| Supervisor lease, slices and progress | `hermes_cli/kanban_supervisor.py` |
| Human-gate lifecycle | `hermes_cli/kanban_human_gate.py` |
| Roadmap parsing and orchestration | `hermes_cli/kanban_roadmap_executor.py` |
| Operational adoption and loop limits | R7 hardening |

K9 is referenced through stable identifiers and fingerprints. Its task/link rows are not
duplicated into mission state.

## 4. Persistence model

### R2 tables

- `mission_missions`: canonical current state for each mission.
- `mission_journal`: append-only operation identity and result journal.

### R3 tables

- `mission_supervisor_leases`: current owner, expiry, and fencing epoch.
- `mission_roadmap_slices`: durable slice definitions, attempts, outcomes, and evidence.
- `mission_supervisor_state`: current slice and aggregate supervisor progress.

### R4 table

- `mission_human_decisions`: versioned durable questions and accepted resolution.

All migrations are additive and idempotent. They run lazily from `kanban_db.connect()`.

## 5. Core invariants

1. **Canonical identity:** every mission has a stable `mission_id`.
2. **CAS generation:** successful mission transitions advance exactly one generation.
3. **Operation idempotency:** identical replay returns the original result without a
   second mutation.
4. **Conflict detection:** the same operation ID with materially different input is
   rejected.
5. **Finite canonical JSON:** NaN and infinities are rejected at any depth.
6. **R1 validation:** accepted state must satisfy the frozen vocabulary and semantic
   bindings.
7. **Single active supervisor:** lease ownership and fencing epoch protect every durable
   supervisor write.
8. **Expired-owner fencing:** an old owner cannot write after another supervisor obtains a
   newer epoch.
9. **One slice per tick:** each supervisor tick selects at most one material slice.
10. **Dependency safety:** a slice is runnable only after all declared dependencies are
    terminal-successful.
11. **No accidental completion:** blocked, human-gate and queue-exhausted states are not
    completion.
12. **Gate versioning:** replies to superseded gates are rejected.
13. **Cross-mission isolation:** a decision for one mission cannot resolve another.
14. **Roadmap authority:** execution may not invent slices outside the loaded roadmap.
15. **Bounded looping:** repeated identical failures must eventually back off and escalate.

## 6. Public runtime surfaces

### Mission state

- `create_mission`
- `get_mission`
- `compare_and_transition`
- `list_journal`
- `migrate_mission_state`
- `canonical_fingerprint`

### Supervisor

- `migrate_supervisor`
- `add_slices`
- `get_roadmap_slices`
- `get_next_slice`
- `acquire_supervisor_lease`
- `renew_supervisor_lease`
- `release_supervisor_lease`
- `run_supervisor_tick`
- `recover_supervisor_state`
- `detect_incomplete_slices`
- slice outcome markers

### Human gates

- `migrate_human_gate`
- `create_human_gate`
- `respond_to_gate`
- `get_pending_gates`
- `get_gate`
- transport abstraction and filesystem adapter

### Roadmap executor

- `load_roadmap`
- `validate_roadmap_data`
- `resolve_dependency_order`
- `run_roadmap_executor`
- `ExecutorConfig`
- `ExecutorResult`

## 7. Required outcome vocabulary

Mission transition results preserve the R1 vocabulary:

- `applied`
- `replayed`
- `stale_generation`
- `conflict`
- `invalid`

Supervisor slice states include:

- `pending`
- `active`
- `completed`
- `blocked`
- `human_gate`
- `queue_exhausted`
- `failed`
- `skipped`

Synonyms must not silently replace normative values.

## 8. Recovery contract

On startup or resume:

1. Open the same durable SQLite database.
2. Load mission state and supervisor state.
3. Validate repository/head identity when the slice depends on Git.
4. Acquire a new or renewed supervisor lease.
5. Compare the lease fencing epoch with persisted supervisor state.
6. Detect an active slice without a terminal outcome.
7. Re-execute only through an idempotent operation boundary.
8. Never repeat a completed operation.
9. Stop at blocker, human gate, queue exhaustion, terminal state, or bounded-loop limit.
10. Persist `next_safe_action` before yielding control.

## 9. Human-decision contract

A durable human gate must include:

- mission identity;
- gate identity and version;
- type;
- structured prompt;
- prompt fingerprint;
- allowed response shape or options;
- recommendation and impact when rendered to a person;
- status;
- accepted resolution and decision identity;
- exact condition for resumption.

The current core provides storage and a transport abstraction. A real Hermes conversation
adapter remains an acceptance requirement.

## 10. Roadmap contract

A roadmap is versioned JSON with:

- roadmap ID and version;
- phases;
- globally unique slice IDs;
- dependencies;
- material description;
- acceptance criteria;
- test commands;
- optional senior or human gate;
- retry limits and priority.

Loading must reject duplicate IDs, missing dependencies, circular dependencies, malformed
types, and incompatible replay.

## 11. Consultation and review

`codex-senior-consult` is advisory. A consultation must be bound to the exact mission,
question set, code head/tree when applicable, and response fingerprint. Hermes must adopt
or reject each material finding explicitly.

A simulated callback is sufficient for unit tests but not for final operational acceptance.

## 12. Security and safety

- No force-push.
- No destructive cleanup of protected worktrees.
- No live database migration during offline tests.
- No secrets in mission state, checkpoints, or PR bodies.
- No anonymous supervisor owner.
- TTL and attempt limits must be bounded.
- All personal writes target `arumihsnek/hermes-agent`.
- Official upstream is reference-only.

## 13. Acceptance levels

### Library acceptance

R2–R7 merged and focused tests passing.

### Integration acceptance

R7 is merged; integration acceptance still requires real transport, process-level resume, and self-hosting canaries.

### Operational acceptance

Requires controlled adoption in the live Hermes installation, observability, documented
rollback, and a successful non-destructive real mission.
