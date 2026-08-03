# Kanban Autonomous Supervisor — Changelog

This changelog records the personal product line in `arumihsnek/hermes-agent`. It does not
claim changes were accepted by the official upstream repository.

## 2026-08-03 — R7 adoption hardening proposed

- PR #20 opened from `codex/kanban-adoption-hardening`.
- Head: `1d20e08490c3e7b6def43a9e57b0e3cd47c127a4`.
- Adds correction and identical-failure limits, exponential backoff, error
  fingerprinting, session tick budget, fresh-install checks, a non-destructive mission
  test, and rollback-safety checks.
- Reported focused tests: 18/18.
- Status: **open; not part of `main`**.

## 2026-08-03 — R6 integrated component canary

- PR #19 merged.
- Feature commit: `cde68ad75fe6bf200de306fb5f60092faee2ba55`.
- Merge commit: `337d698e1affa5a0195da641a74e8a1fa42ac3a8`.
- Adds `tests/hermes_cli/test_kanban_e2e_canary.py`.
- Exercises mission creation, lease reacquisition, incomplete-slice recovery, replay
  protection, two-supervisor competition, blocker state, human gate state, queue
  exhaustion and final consistency.
- Focused canary: 7/7.
- Important limitation: consultation and user response are simulated inside the test;
  this is not proof of real chat or process-level self-hosting.

## 2026-08-03 — R5 autonomous roadmap executor

- PR #18 merged.
- Feature commit: `047c7bb9046e715f00a6ccd3f4f0e587780958b7`.
- Merge commit: `d5d7d99d987b639613a0cc93069d3079442ad758`.
- Adds `hermes_cli/kanban_roadmap_executor.py`.
- Adds JSON roadmap loading, DAG ordering, cycle detection, declared gates, phase
  progress and scope bounding.
- Focused tests: 18/18.

## 2026-08-03 — R4 durable human-decision channel

- PR #17 merged.
- Feature commit: `4aa8c43ee69f6017d65f1c6509d652eeb4185a62`.
- Follow-up encoding fix: `f7177f2ec919d55773fc809aead523b006adeb29`.
- Merge commit: `d12aca851c67dc1c1056beec9cb39d0af08e442c`.
- Adds `hermes_cli/kanban_human_gate.py` and durable gate migration.
- Adds versioned gate lifecycle, cross-mission rejection, duplicate rejection, restart
  persistence and filesystem transport.
- Focused tests: 33/33.

## 2026-08-03 — R3 durable supervisor

- PR #16 merged.
- Roadmap commit: `b04fbe04c56408d3c07d0264d34cf9a22e115888`.
- Feature commit: `83afe98aee3d6f78e8b96abdaf02fb56997ee019`.
- Merge commit: `eaf307a9fe489ebe7f5b9a38781c7aef010b8002`.
- Adds `hermes_cli/kanban_supervisor.py` and three additive tables.
- Adds lease fencing, slice persistence, crash recovery, dependency ordering and outcome
  classification.
- Senior consultation `af3aab04-8d37-45b0-af7f-7f1ce0549fb0` produced four blocking
  findings; all four were adopted.
- Focused supervisor tests: 55/55.

## 2026-08-03 — R2 durable mission state

- PR #15 merged.
- Merge commit: `6a31a89b0668d255e7e110d3e029da92a4c794db`.
- Adds `hermes_cli/kanban_mission_state.py`.
- Adds mission current state, operation journal, generation CAS, canonical fingerprint,
  replay/conflict handling, R1 validation and automatic migration.
- Focused tests: 108 mission-state + 30 Kanban DB = 138/138.

## R1 — Contract freeze

- Historical control-plane phase.
- Frozen `kanban-mission-state/v1` vocabulary, JSON Schema, semantic rules and fixtures.
- No product runtime was added in this phase.

## R0 — Investigation

- Historical control-plane phase.
- Identified the continuation gap, ownership boundaries, K9 reuse requirements and risks
  of duplicating durable task state.

## Evidence policy

Full-suite totals in historical PR descriptions differ because the collected test scope
changed between runs. This changelog treats exact focused suites and exact commit/PR
identity as the stable phase evidence.
