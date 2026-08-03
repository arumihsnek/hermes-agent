# Kanban Autonomous Supervisor — Evidence Matrix

## Evidence rules

- A focused test suite supports only the behavior it directly exercises.
- A commit message or PR body is historical evidence, not a substitute for test output.
- Full-suite totals from different commands are not comparable.
- A simulated callback does not prove a real transport integration.
- Reopening a SQLite connection in one process does not prove a new Hermes process/session.

## Phase evidence

| Claim | Strongest current evidence | Confidence | Limitation |
|---|---|---:|---|
| Mission state survives reopen | R2 persistence tests, PR #15 | High | Offline/library scope |
| Replay is idempotent | R2 tests and journal contract | High | Requires callers to preserve operation identity |
| Concurrent writers are fenced | R2 CAS + R3 lease tests | High | Operational process test still needed |
| Supervisor resumes incomplete slice | R3 focused tests | High | Mostly unit/integration test harness |
| Human gates persist across reopen | R4 tests | High | Filesystem transport only |
| Roadmap DAG is validated | R5 focused tests | High | No live engineering roadmap proof |
| R2–R5 compose in one scenario | R6 canary | Medium-high | In-process; simulated senior/user |
| System survives a real session handoff | Not yet demonstrated | Low | Required final canary |
| User can answer through Hermes chat | Not yet demonstrated | Low | Real adapter required |
| Supervisor self-hosts a roadmap | Not yet demonstrated | Low | Required final canary |
| Loop hardening is implemented | R7 PR #20 | Medium | Open PR; runtime enforcement review needed |

## Exact delivery evidence

| Phase | PR | Feature head | Merge head | Focused tests |
|---|---:|---|---|---:|
| R2 | 15 | historical multi-commit branch | `6a31a89b...` | 138 |
| R3 | 16 | `83afe98a...` | `eaf307a9...` | 55 |
| R4 | 17 | `f7177f2e...` | `d12aca85...` | 33 |
| R5 | 18 | `047c7bb9...` | `d5d7d99d...` | 18 |
| R6 | 19 | `cde68ad7...` | `337d698e...` | 7 |
| R7 | 20 | `1d20e084...` | not merged | 18 reported |

## Historical reporting discrepancies

Historical PR and commit descriptions report different aggregate totals:

- R5 appears as 214, 237, or other totals depending on included scopes.
- R6 appears as 221 or 244 depending on included scopes.
- R7 reports a suite excluding the R6 canary in its description.

These differences are not treated as product regressions by themselves. The canonical
evidence is the exact focused suite plus the exact command recorded in future checkpoints.

## Required future evidence

- A log from two distinct process IDs resuming one mission.
- A durable human gate delivered and answered through Hermes chat.
- A real senior consultation execution ID and persisted response fingerprint from the
  integrated mission.
- A self-hosted roadmap checkpoint sequence.
- A controlled live adoption report with rollback evidence.
