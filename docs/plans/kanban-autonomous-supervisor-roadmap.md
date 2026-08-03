# Kanban Autonomous Supervisor Roadmap

> Mission: `kanban-autonomous-supervisor`  
> Updated: `2026-08-03T23:15:00+02:00`  
> Repository main: `163b8dd488b77d4cf0ba19ab04d47e42a9c03bac`  
> Runtime implementation head: `734529568fa094ceabbe95aaf0bbc8c0e7e1abad`  
> Current phase: operational acceptance  
> Implementation phases R2–R7 merged: **yes**  
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
| R7 | Adoption and hardening | Merged | personal PR #20, merge `73452956...` |

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

R2–R7 are merged and provide a strong library-level implementation. R6 demonstrates that
R2–R5 compose in one test scenario, and R7 adds loop-hardening helpers and adoption tests.
However, the integrated canary still simulates senior consultation and human response,
and its restart is a database reopen inside one test process.

Therefore the original objective is not yet fully demonstrated. The final acceptance work
must still prove:

- actual runtime use of loop bounds, failure fingerprints and backoff;
- a real Hermes chat transport for human gates;
- a real `codex-senior-consult` call bound to mission state;
- recovery by a distinct process or Hermes session;
- self-hosted execution of a real roadmap;
- operational start, status, resume and stop controls;
- controlled live non-destructive adoption and rollback.

## Next safe action

Audit the merged R7 runtime against the complete acceptance criteria in the canonical
[`ROADMAP.md`](../kanban-autonomous-supervisor/ROADMAP.md), then execute the real chat,
process-handoff, self-hosting and controlled-adoption canaries. Do not declare the mission
complete solely because all implementation PRs are merged.
