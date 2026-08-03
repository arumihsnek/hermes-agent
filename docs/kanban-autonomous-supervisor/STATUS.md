# Current Project Status

> Status snapshot: `2026-08-03T23:28:00+02:00`  
> Repository: `arumihsnek/hermes-agent`  
> Verification target: merged `personal-product/main`  
> Repository `main` observed before this documentation update: `ed5622aa3926805167ceea42c70c21fde1def73c`  
> Runtime implementation head: `734529568fa094ceabbe95aaf0bbc8c0e7e1abad`  
> Latest runtime PR: `#20 feat: R7 adoption hardening`

The repository-head field is an observation, not a self-updating invariant. Documentation
merges advance `main`; the stable runtime identity for R2–R7 is the runtime implementation
head above.

## Objective

Hermes must be able to execute a versioned long-running roadmap with durable state,
survive interruption, prevent duplicate execution, consult a senior reviewer at declared
gates, ask the user only for irreducible human decisions, resume after the answer, and
finish through review and personal-repository integration without an external supervisor
coordinating every phase.

## Latest verification

Fresh verification on merged `personal-product/main` reports:

```text
239 passed
0 failed
RC=0
```

Non-overlapping phase breakdown:

| Phase | Suite | Passed |
|---|---|---:|
| R2 | mission-state | 108 |
| R3 | supervisor | 55 |
| R4 | human gate | 33 |
| R5 | roadmap executor | 18 |
| R6 | canary | 7 |
| R7 | hardening | 18 |
| **Total** | **R2–R7 focused aggregate** | **239** |

This is now the canonical aggregate count. The former R2 total of 138 included 30
additional Kanban DB tests and therefore must not be added to the R3–R7 subtotals.

## Phase status

| Phase | Purpose | Delivery status | Evidence |
|---|---|---:|---|
| R0 | Investigate ownership and continuation gap | Complete, historical | control-plane reports |
| R1 | Freeze `kanban-mission-state/v1` contract | Complete, historical | control-plane schema and fixtures |
| R2 | Durable mission-state backend | Merged | PR #15, merge `6a31a89b...`, 108 focused |
| R3 | Reentrant durable supervisor | Merged | PR #16, merge `eaf307a9...`, 55 focused |
| R4 | Durable human-decision core | Merged | PR #17, merge `d12aca85...`, 33 focused |
| R5 | Autonomous roadmap executor | Merged | PR #18, merge `d5d7d99d...`, 18 focused |
| R6 | Integrated component canary | Merged | PR #19, merge `337d698e...`, 7 focused |
| R7 | Adoption hardening | Merged | PR #20, merge `73452956...`, 18 focused |

Implementation progress for the R3–R7 roadmap is **5 merged phases out of 5**. The
combined R2–R7 focused aggregate is **239/239, RC=0**. Mission acceptance nevertheless
remains incomplete until the real-world gates below are demonstrated.

## What exists on published `main`

### Durable mission state — R2

- SQLite current-state and operation-journal persistence.
- Generation compare-and-swap.
- Idempotent replay and conflict detection by operation identity.
- Canonical finite JSON fingerprints.
- R1 structural and semantic validation.
- Automatic additive migration from `kanban_db.connect()`.

### Durable supervisor — R3

- One-slice-per-tick supervisor loop.
- SQLite lease with monotonic fencing epoch.
- Durable roadmap slices and supervisor state.
- Crash/incomplete-slice recovery.
- Blocked, human-gate, queue-exhausted, failed, skipped, and completed outcomes.
- Retry limits and terminal-state protection.

### Human-decision core — R4

- Durable gate creation, pending and resolved lifecycle.
- Gate versioning and stale-response rejection.
- Duplicate and cross-mission response rejection.
- Transport abstraction.
- Filesystem transport suitable for tests and fallback workflows.

### Roadmap executor — R5

- Versioned JSON roadmap loading.
- DAG validation and deterministic dependency ordering.
- Circular and missing dependency rejection.
- Supervisor orchestration and phase progress reporting.
- Declared senior and human gates.
- Scope bounded by the loaded roadmap.

### Integrated canary — R6

- Exercises R2–R5 in one reproducible test scenario.
- Reopens the SQLite database and reacquires a lease.
- Tests replay protection, competing supervisors, blocker resume, human-gate resume,
  queue exhaustion, and final consistency.
- It is a **component-level canary**, not proof of live autonomous operation.

### Adoption hardening — R7

- Correction and identical-failure limits.
- Exponential backoff with a maximum cap.
- Error fingerprinting.
- Per-session tick budget.
- Fresh-install migration checks.
- Non-destructive mission test.
- Additive rollback-safety tests.

R7 is part of the runtime implementation at merge commit
`734529568fa094ceabbe95aaf0bbc8c0e7e1abad`.

## What is still unproven

These are acceptance gaps, not optional polish:

1. A real Hermes chat adapter that renders a durable gate to the user and ingests the
   reply without a manual file handoff.
2. A real `codex-senior-consult` invocation bound to the mission state in the integrated
   canary; R6 currently simulates the resolution.
3. Termination of one Hermes process/session and continuation by another process/session,
   rather than only closing and reopening a database connection in one test process.
4. Self-hosting: the supervisor itself must durably execute and advance a real roadmap.
5. Operational entrypoint and observability for starting, inspecting, stopping, and
   resuming a mission.
6. A controlled non-destructive deployment against the actual Hermes installation.
7. Exact-head review and personal PR lifecycle driven by the supervisor rather than by
   the surrounding session.

## Kanban state

### As a library

Advanced and internally consistent for isolated orchestration experiments. R2–R7 are
merged and the combined focused scope passes 239/239 with RC=0.

### As an autonomous product feature

Not yet fully adopted. The engine exists, but real chat delivery, process-level handoff,
self-hosting, and live operational integration are not demonstrated.

### As a documentation system

This directory is the canonical status set. Historical aggregate-count discrepancies are
resolved by `EVIDENCE.md`, which records the exact non-overlapping 239-test breakdown.

## Immediate next actions

1. Audit whether R7 loop limits, fingerprints, and backoff are enforced by runtime control flow rather than only exposed as helpers.
2. Implement or validate a real Hermes conversation transport for human gates.
3. Run a true two-session/process recovery canary.
4. Run a self-hosting canary where the roadmap executor advances a real roadmap.
5. Perform controlled live adoption with rollback and observability.
6. Record the exact accepted operational head in a final checkpoint.
