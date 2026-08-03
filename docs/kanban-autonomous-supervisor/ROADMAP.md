# Kanban Autonomous Supervisor — Current Roadmap

> Canonical current roadmap  
> Updated: 2026-08-03  
> Published main at update: `337d698e1affa5a0195da641a74e8a1fa42ac3a8`

## North-star outcome

A user supplies a versioned roadmap. Hermes executes it across as many sessions as
necessary, persists all material state, consults a senior reviewer at declared gates,
asks the user only for irreducible decisions, resumes automatically, and finishes through
review and integration in the personal repository.

## Two status dimensions

A phase may be implemented without the whole mission being operationally accepted.

| Phase | Implementation | Operational proof |
|---|---:|---:|
| R0 Investigation | Complete | Complete |
| R1 Contract | Complete | Complete |
| R2 Mission state | Merged | Library proof complete |
| R3 Supervisor | Merged | Library proof complete |
| R4 Human-decision core | Merged | Real chat transport not proven |
| R5 Roadmap executor | Merged | Self-hosting not proven |
| R6 Integrated canary | Merged | Component-level; real senior/chat/process not proven |
| R7 Adoption hardening | Open PR #20 | Pending |

## R0 — Investigation

### Outcome

Established that K9 already owns durable task/DAG state and must be referenced rather
than duplicated. Identified the missing canonical mission state and resumable supervisor.

### Status

Complete, historical.

## R1 — Contract

### Outcome

Frozen `kanban-mission-state/v1`, fixtures, structural validation and semantic invariants
in the control plane.

### Status

Complete, historical.

## R2 — Durable mission state

### Outcome

SQLite mission state and operation journal with CAS, canonical fingerprints, replay,
conflict, validation, rollback and additive migration.

### Status

Merged in personal PR #15.

## R3 — Durable supervisor

### Outcome

Persistent slices, supervisor progress, lease TTL, fencing epoch, one-slice-per-tick
execution, crash recovery and outcome classification.

### Status

Merged in personal PR #16.

## R4 — Durable human decisions

### Outcome

Versioned durable gate lifecycle and transport-neutral interface.

### Status

Merged in personal PR #17.

### Remaining operational gate

Implement and prove a real adapter that presents a gate in the active Hermes conversation
and validates the reply after a session restart.

## R5 — Autonomous roadmap executor

### Outcome

JSON roadmap loader, dependency resolver, gate-aware executor and bounded roadmap scope.

### Status

Merged in personal PR #18.

### Remaining operational gate

Use the executor as the durable source of truth for a real multi-phase engineering
roadmap, including exact-head review and personal PR actions.

## R6 — Integrated canary

### Outcome

One test scenario exercises R2–R5 together, including database reopen and lease recovery.

### Status

Merged in personal PR #19.

### Interpretation

R6 proves component integration. It does not yet prove:

- a second operating-system process or new Hermes session;
- a live `codex-senior-consult` call;
- a user response arriving through Hermes chat;
- autonomous Git/PR execution;
- self-hosting of this roadmap.

## R7 — Adoption and hardening

### Current delivery

PR #20 is open with:

- exponential backoff;
- repeated-error fingerprinting;
- correction and identical-failure bounds;
- per-session tick budget;
- fresh-install checks;
- non-destructive mission test;
- additive rollback-safety tests.

### Required before phase completion

- [ ] PR #20 reviewed against this specification.
- [ ] P0 and P1 findings equal zero.
- [ ] R2–R6 regression suite remains green.
- [ ] Loop limits are actually enforced in execution paths, not only defined and tested
      as constants/helpers.
- [ ] Failure fingerprints are persisted or otherwise used to stop repeated loops.
- [ ] Backoff is applied by the runtime loop.
- [ ] Operational start/status/resume/stop interface is documented.
- [ ] Real Hermes chat transport is implemented or explicitly split into a follow-up with
      the mission still marked incomplete.
- [ ] True process/session recovery canary passes.
- [ ] Self-hosting canary passes.
- [ ] Controlled live non-destructive adoption passes.
- [ ] Rollback is rehearsed and documented.

## Final acceptance canaries

### A. Real process handoff

1. Process A creates and advances a mission.
2. Process A terminates after persisting an active slice.
3. Process B opens the same database.
4. Process B obtains a newer fencing epoch.
5. Process B resumes without duplicating the prior operation.

### B. Real human gate

1. Supervisor creates a durable gate.
2. Hermes renders it in the current user conversation.
3. The session ends.
4. A later session receives the user's decision.
5. The decision is bound to the same mission/gate version.
6. The supervisor resumes automatically.

### C. Real senior gate

1. Roadmap declares a senior consultation.
2. A real consultation bundle is generated from the exact head.
3. The response is fingerprinted and persisted.
4. Findings are locally accepted or rejected.
5. Execution continues from the persisted decision.

### D. Self-hosting

The roadmap executor durably advances at least one real engineering roadmap across two
sessions, including a human gate and final review.

### E. Controlled adoption

Run a non-destructive mission against the actual Hermes installation with:

- backups;
- explicit rollback;
- observability;
- no protected-data mutation;
- no upstream write;
- exact post-run evidence.

## Completion definition

The project is complete only when all five final canaries pass and the documentation,
manifest and checkpoints identify the exact accepted heads and evidence.
