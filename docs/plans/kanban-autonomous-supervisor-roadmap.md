# Kanban Autonomous Supervisor Roadmap

> Mission: `kanban-autonomous-supervisor`  
> Updated: `2026-08-03T22:57:00+02:00`  
> Published main: `337d698e1affa5a0195da641a74e8a1fa42ac3a8`  
> Current phase: R7  
> Mission complete: **no**

This file is the public roadmap mirror. The canonical documentation set is:

- [`../kanban-autonomous-supervisor/README.md`](../kanban-autonomous-supervisor/README.md)
- [`../kanban-autonomous-supervisor/STATUS.md`](../kanban-autonomous-supervisor/STATUS.md)
- [`../kanban-autonomous-supervisor/SPEC.md`](../kanban-autonomous-supervisor/SPEC.md)
- [`../kanban-autonomous-supervisor/ROADMAP.md`](../kanban-autonomous-supervisor/ROADMAP.md)
- [`../kanban-autonomous-supervisor/CHECKPOINTS.md`](../kanban-autonomous-supervisor/CHECKPOINTS.md)
- [`../kanban-autonomous-supervisor/EVIDENCE.md`](../kanban-autonomous-supervisor/EVIDENCE.md)
- [`../kanban-autonomous-supervisor/CHANGELOG.md`](../kanban-autonomous-supervisor/CHANGELOG.md)
- [`../kanban-autonomous-supervisor/MANIFEST.json`](../kanban-autonomous-supervisor/MANIFEST.json)

`r3-roadmap-bundle.json` remains historical architecture-consultation input and must not
be interpreted as live phase status.

## Phase status

| Phase | Objective | Status | Delivery |
|---|---|---|---|
| R0 | Investigate continuation and ownership gap | Complete | historical control-plane evidence |
| R1 | Freeze `kanban-mission-state/v1` | Complete | historical control-plane schema |
| R2 | Durable mission state and journal | Merged | personal PR #15, merge `6a31a89b...` |
| R3 | Durable reentrant supervisor | Merged | personal PR #16, merge `eaf307a9...` |
| R4 | Durable human-decision core | Merged | personal PR #17, merge `d12aca85...` |
| R5 | Autonomous JSON roadmap executor | Merged | personal PR #18, merge `d5d7d99d...` |
| R6 | Integrated component canary | Merged | personal PR #19, merge `337d698e...` |
| R7 | Adoption and hardening | Active | personal PR #20, head `1d20e084...` |

## Frozen architectural decisions

1. K9 owns durable task/DAG state; mission state references it and never duplicates it.
2. Runtime mission state is persisted in SQLite with generation CAS and an operation journal.
3. Supervisor writes require lease ownership and a valid fencing epoch.
4. Each supervisor tick executes at most one material slice.
5. `blocked`, `human_gate`, and `queue_exhausted` are not completion.
6. Human-decision persistence is transport-neutral.
7. Roadmaps are versioned JSON and define the allowed execution scope.
8. Official upstream is reference-only; all project writes target `arumihsnek/hermes-agent`.

## Current interpretation

R2–R6 are merged and provide a strong library-level implementation. R6 demonstrates that
those components compose in one test scenario, but its senior consultation and human
response are simulated and its restart is a database reopen inside a test process.

Therefore the original objective is not yet fully demonstrated. R7 and the final adoption
gates must still prove:

- actual runtime use of loop bounds and backoff;
- a real Hermes chat transport for human gates;
- a real `codex-senior-consult` call bound to mission state;
- recovery by a distinct process or Hermes session;
- self-hosted execution of a real roadmap;
- operational start, status, resume and stop controls;
- controlled live non-destructive adoption and rollback.

## Next safe action

Review PR #20 against the complete R7 acceptance criteria in the canonical
[`ROADMAP.md`](../kanban-autonomous-supervisor/ROADMAP.md). Do not declare the mission
complete solely from the focused hardening tests.
