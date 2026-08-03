# Kanban Autonomous Supervisor — Documentation Index

> Canonical documentation set for the long-running Kanban mission supervisor in
> `arumihsnek/hermes-agent`.
>
> Snapshot time: `2026-08-03T23:15:00+02:00`  
> Repository `main`: `163b8dd488b77d4cf0ba19ab04d47e42a9c03bac`  
> Runtime implementation head: `734529568fa094ceabbe95aaf0bbc8c0e7e1abad`  
> Latest delivery: R7 merged by PR #20 at `734529568fa094ceabbe95aaf0bbc8c0e7e1abad`

## Read this first

1. [`STATUS.md`](STATUS.md) — what is implemented, merged, active, and still unproven.
2. [`SPEC.md`](SPEC.md) — current technical contract, invariants, modules, tables, and APIs.
3. [`ROADMAP.md`](ROADMAP.md) — where the project came from and the remaining acceptance gates.
4. [`CHECKPOINTS.md`](CHECKPOINTS.md) — phase-by-phase durable ledger with PRs and SHAs.
5. [`EVIDENCE.md`](EVIDENCE.md) — claim-to-test and claim-to-commit matrix.
6. [`CHANGELOG.md`](CHANGELOG.md) — chronological history.
7. [`MANIFEST.json`](MANIFEST.json) — machine-readable project state and document manifest.

## Source-of-truth order

When documents disagree, use this precedence:

1. Code and migrations in the exact published commit.
2. Durable SQLite state for a concrete mission instance.
3. Git commits and merged PR metadata.
4. [`MANIFEST.json`](MANIFEST.json).
5. [`STATUS.md`](STATUS.md).
6. [`SPEC.md`](SPEC.md).
7. [`ROADMAP.md`](ROADMAP.md).
8. Historical phase reports and PR descriptions.

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
- The implementation roadmap R2–R7 is merged, but the original objective is **not yet fully accepted** because the published canary
  simulates senior consultation and human response inside a test process. A real Hermes
  chat transport, a true process/session handoff, and controlled operational adoption
  remain explicit acceptance gates.

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
