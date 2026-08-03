# Kanban Autonomous Supervisor — Documentation Index

> Canonical documentation set for the long-running Kanban mission supervisor in
> `arumihsnek/hermes-agent`.
>
> Snapshot time: `2026-08-03T23:28:00+02:00`  
> Verification target: merged `personal-product/main`  
> Runtime implementation head: `734529568fa094ceabbe95aaf0bbc8c0e7e1abad`  
> Latest verification: `239/239 passed, RC=0`

## Read this first

1. [`STATUS.md`](STATUS.md) — what is implemented, merged, active, and still unproven.
2. [`SPEC.md`](SPEC.md) — current technical contract, invariants, modules, tables, and APIs.
3. [`ROADMAP.md`](ROADMAP.md) — where the project came from and the remaining acceptance gates.
4. [`CHECKPOINTS-CURRENT.md`](CHECKPOINTS-CURRENT.md) — current durable checkpoint and exact 239-test verification.
5. [`CHECKPOINTS.md`](CHECKPOINTS.md) — historical phase-by-phase ledger retained for continuity.
6. [`EVIDENCE.md`](EVIDENCE.md) — claim-to-test and claim-to-commit matrix.
7. [`CHANGELOG.md`](CHANGELOG.md) — chronological history.
8. [`MANIFEST.json`](MANIFEST.json) — machine-readable project state and document manifest.

## Source-of-truth order

When documents disagree, use this precedence:

1. Code and migrations in the exact published runtime commit.
2. Durable SQLite state for a concrete mission instance.
3. Git commits and merged PR metadata.
4. [`MANIFEST.json`](MANIFEST.json).
5. [`CHECKPOINTS-CURRENT.md`](CHECKPOINTS-CURRENT.md).
6. [`STATUS.md`](STATUS.md).
7. [`SPEC.md`](SPEC.md).
8. [`ROADMAP.md`](ROADMAP.md).
9. Historical phase reports and PR descriptions.

PR descriptions and commit messages are evidence, but they are not normative APIs.
Historical full-suite totals are not directly comparable unless the exact pytest command
and collected test set are identical.

## Current executive summary

- R2 durable mission state: merged.
- R3 durable supervisor: merged.
- R4 durable human-decision core: merged.
- R5 roadmap executor: merged.
- R6 component-level end-to-end canary: merged.
- R7 adoption hardening: merged in PR #20.
- Fresh combined focused verification: **239/239 passed, RC=0**.
- The implementation roadmap R2–R7 is merged, but the original objective is **not yet fully accepted** because the published canary simulates senior consultation and human response inside a test process. A real Hermes chat transport, a true process/session handoff, self-hosting, and controlled operational adoption remain explicit acceptance gates.

## Canonical test breakdown

```text
R2 mission-state       108
R3 supervisor           55
R4 human gate           33
R5 roadmap executor     18
R6 canary                 7
R7 hardening             18
                        ---
Total                   239
```

The former R2 figure of 138 included 30 additional Kanban DB tests. It is retained as
historical evidence but is not used in the non-overlapping R2–R7 aggregate.

## Repository boundaries

- Writable product repository: `arumihsnek/hermes-agent`.
- Official upstream reference: `NousResearch/hermes-agent`.
- No upstream review or merge is required for this personal project.
- Control-plane policy and the historical R1 JSON Schema live outside this product
  repository; this repository implements and tests the runtime contract.

## Historical documents

- `docs/plans/kanban-autonomous-supervisor-roadmap.md` is maintained as the public
  phase roadmap and links back to this canonical set.
- `docs/plans/r3-roadmap-bundle.json` is a historical architecture-consultation bundle.
  It must not be treated as live status.
- `CHECKPOINTS.md` is retained as the original cumulative ledger. New resumptions should
  use `CHECKPOINTS-CURRENT.md` as the active checkpoint.
