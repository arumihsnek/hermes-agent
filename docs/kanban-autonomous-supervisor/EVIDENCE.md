# Kanban Autonomous Supervisor — Evidence Matrix

## Evidence rules

- A focused test suite supports only the behavior it directly exercises.
- A commit message or PR body is historical evidence, not a substitute for test output.
- Full-suite totals from different commands are not comparable unless their collected scopes are identical.
- A simulated callback does not prove a real transport integration.
- Reopening a SQLite connection in one process does not prove a new Hermes process/session.
- User-supplied verification is recorded as such when the exact shell command and raw log are not committed.

## Latest aggregate verification

Fresh verification reported on `2026-08-03T23:28:00+02:00` against merged
`personal-product/main` containing all R2–R7 runtime code:

```text
aggregate_tests=239
aggregate_passed=239
aggregate_failed=0
exit_code=0

R2_mission_state=108
R3_supervisor=55
R4_human_gate=33
R5_roadmap_executor=18
R6_canary=7
R7_hardening=18
```

The arithmetic is exact:

```text
108 + 55 + 33 + 18 + 7 + 18 = 239
```

This is the canonical aggregate count for the combined R2–R7 focused scope. It
supersedes the inconsistent aggregate totals previously copied into individual PR and
commit descriptions. The exact invocation and raw test log were not included in this
repository update, so the evidence source is the fresh operator verification report.

## Phase evidence

| Claim | Strongest current evidence | Confidence | Limitation |
|---|---|---:|---|
| Mission state survives reopen | R2 persistence tests, PR #15 | High | Offline/library scope |
| Replay is idempotent | R2 tests and journal contract | High | Requires callers to preserve operation identity |
| Concurrent writers are fenced | R2 CAS + R3 lease tests | High | Operational process test still needed |
| Supervisor resumes incomplete slice | R3 focused tests | High | Mostly unit/integration test harness |
| Human gates persist across reopen | R4 tests | High | Filesystem transport only |
| Roadmap DAG is validated | R5 focused tests | High | No live engineering roadmap proof |
| R2–R7 combined focused scope passes | Fresh 239/239, RC=0 report | High | Raw command/log not committed |
| R2–R5 compose in one scenario | R6 canary | Medium-high | In-process; simulated senior/user |
| System survives a real session handoff | Not yet demonstrated | Low | Required final canary |
| User can answer through Hermes chat | Not yet demonstrated | Low | Real adapter required |
| Supervisor self-hosts a roadmap | Not yet demonstrated | Low | Required final canary |
| Loop hardening helpers are merged | R7 PR #20 + R7 focused tests | High | Runtime enforcement review still needed |

## Exact delivery evidence

| Phase | PR | Feature head | Merge head | Focused tests |
|---|---:|---|---|---:|
| R2 | 15 | historical multi-commit branch | `6a31a89b...` | 108 |
| R3 | 16 | `83afe98a...` | `eaf307a9...` | 55 |
| R4 | 17 | `f7177f2e...` | `d12aca85...` | 33 |
| R5 | 18 | `047c7bb9...` | `d5d7d99d...` | 18 |
| R6 | 19 | `cde68ad7...` | `337d698e...` | 7 |
| R7 | 20 | `1d20e084...` | `73452956...` | 18 |
| **Combined** | 15–20 | — | runtime through `73452956...` | **239/239, RC=0** |

The former R2 figure of 138 combined the 108 mission-state tests with 30 general
`test_kanban_db.py` tests. The canonical R2 subtotal in the R2–R7 aggregate is now 108,
so the six phase subtotals are non-overlapping and sum exactly to 239.

## Historical reporting discrepancies — resolved

Historical PR and commit descriptions reported different aggregate totals:

- R5 appeared as 214 or 237 depending on included scopes.
- R6 appeared as 221 or 244 depending on included scopes.
- R7 descriptions omitted or included different preceding suites.
- R2 was sometimes reported as 138 because 30 Kanban DB tests were bundled with the
  108 mission-state tests.

These historical totals remain useful only as records of their original commands. The
canonical current aggregate is the fresh non-overlapping R2–R7 breakdown: **239/239,
RC=0**.

## Required future evidence

- A log from two distinct process IDs resuming one mission.
- A durable human gate delivered and answered through Hermes chat.
- A real senior consultation execution ID and persisted response fingerprint from the
  integrated mission.
- A self-hosted roadmap checkpoint sequence.
- A controlled live adoption report with rollback evidence.
