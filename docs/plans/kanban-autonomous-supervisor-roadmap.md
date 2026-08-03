# Kanban Autonomous Supervisor Roadmap

> mission_id: kanban-autonomous-supervisor
> mission_generation: 0
> version: 1.0.0
> created_at: 2026-08-03
> schema: kanban-mission-state/v1

## Status

| Phase | Status | PR | Merge |
|-------|--------|----|-------|
| R3 | active | — | — |
| R4 | planned | — | — |
| R5 | planned | — | — |
| R6 | planned | — | — |
| R7 | planned | — | — |

## Prior Work

- R0: Investigative. Validated entity ownership matrix, design A/B comparison. Committed in my-hermes-config.
- R1: Schema-only contract. `kanban-mission-state/v1` frozen. Fixtures, tests, JSON Schema. No runtime.
- R2: Durable backend in hermes-agent. `hermes_cli/kanban_mission_state.py` (1054 LOC). Tables: `mission_missions`, `mission_journal`. API: `create_mission`, `get_mission`, `compare_and_transition`, `list_journal`. CAS generation, idempotency, R1 validation. Integrated into `kanban_db.connect()`. PR #15 merged. 138/138 focused tests pass.

## Key Design Decisions (frozen)

1. **K9 referenced, not duplicated.** Mission-state stores `(board, import_id, envelope_fingerprint)`. K9 internal DAG/tasks/links never copied.
2. **Control-plane owns mission-state.** Schema, policy, fixtures in my-hermes-config. Runtime in hermes-agent.
3. **Schema v1 frozen.** No breaking changes without explicit version bump.
4. **R1 vocabulary enforced.** Outcomes: `applied`, `replayed`, `stale_generation`, `conflict`, `invalid`. No synonyms.
5. **SQLite same-DB.** Mission tables alongside K9 tables. Lazy import for circular dependency avoidance.

---

## R3 — Durable Supervisor Minimum

### objective
Build the minimal reentrant supervisor loop that can persistently advance a roadmap of slices, survive session restarts, prevent duplicate execution, and handle blockers/terminal states.

### facts
- R2 provides durable mission-state persistence (create, get, transition, journal) in `hermes_cli/kanban_mission_state.py`
- R2 migration runs automatically in `kanban_db.connect()` via lazy import
- No coordinator loop, lease, or roadmap-loader exists yet
- K9 planner import exists (`hermes_cli/kanban_planner_import.py`) but is not required for R3 (R3 slices are roadmap-defined, not K9-card-defined)
- `codex-senior-consult` integration exists and works (build-bundle, preflight, consult)
- The supervisor must NOT depend on a live Hermes session or conversation context

### hypotheses
- H1: Slices can be self-contained durable objects without K9 card references (validated: roadmap slices carry their own material)
- H2: A lease table with TTL is sufficient to prevent two supervisors on the same mission (validated: SQLite BEGIN IMMEDIATE + lease expiry check is atomic)
- H3: One material mutation per transaction is enforceable at the supervisor level (validated: each slice maps to exactly one `compare_and_transition` call)
- H4: The supervisor can be reentrant by persisting state to SQLite before each action (validated: state recovery from DB on restart)

### decisions
- D1: Slices are roadmap-defined objects with their own schema, NOT K9 cards
- D2: Lease uses SQLite `mission_leases` table with `expires_at` TTL AND monotonically increasing `fencing_epoch` (integer). Every durable write must transactionally verify `(mission_id, owner, fencing_epoch, unexpired lease)`.
- D3: Supervisor state persisted in `mission_supervisor_state` table (current_slice_id, last_completed_slice, error_count, fencing_epoch)
- D4: One supervisor per mission enforced by lease acquisition with TTL renewal AND fencing epoch validation on every write
- D5: Outcome classification: `success`, `blocked`, `human_gate`, `queue_exhausted`, `failed`, `skipped`
- D6: Slices ingested via programmatic Python API (`add_slices(conn, mission_id, slices)`) — no YAML/JSON file dependency in R3
- D7: Slice format is JSON (stdlib `json` module, no new dependencies). YAML deferred to R5.
- D8: Transaction boundaries: supervisor writes (lease, state, slice) use one IMMEDIATE txn. R2 transitions use their own txn via `compare_and_transition`. Crash recovery checks consistency between both.

### exact_write_set
- `hermes_cli/kanban_supervisor.py` (new — supervisor loop, lease, roadmap loader, slice executor)
- `tests/hermes_cli/test_kanban_supervisor.py` (new — comprehensive test suite)
- `docs/superpowers/plans/kanban-autonomous-supervisor-roadmap.md` (this file)
- `docs/superpowers/checkpoints/kanban-long-runtime-r3.md` (checkpoint at completion)

### exact_non_write_set
- `hermes_cli/kanban_mission_state.py` (untouched — R2 API is the foundation)
- `hermes_cli/kanban_db.py` (only if new tables need migration — lazy import pattern)
- `hermes_cli/kanban.py` (no CLI changes in R3)
- `tools/kanban_tools.py` (no tool changes in R3)
- All K9 tables and planner code
- Live Hermes configuration, gateway, or sessions
- `tests/hermes_cli/test_kanban_mission_state.py` (R2 tests unchanged)

### dependencies
- R2 mission-state API (`create_mission`, `get_mission`, `compare_and_transition`, `list_journal`)
- `kanban_db.connect()` (for SQLite connection with WAL + K9 + mission-state migration)
- Python stdlib: `sqlite3`, `secrets`, `time`, `json`, `hashlib`, `logging`
- No new external dependencies

### migration_impact
- New table: `mission_supervisor_leases` (lease management)
- New table: `mission_roadmap_slices` (roadmap slice definitions)
- New table: `mission_supervisor_state` (supervisor runtime state with fencing_epoch)
- All via `CREATE TABLE IF NOT EXISTS` — idempotent, safe on existing DBs
- Migration called from `kanban_db.connect()` via lazy import (same pattern as R2)
- Zero impact on K9 tables or R2 mission-state tables

## Transaction Boundaries (BF2 resolution)

Each supervisor operation has an explicit authority model:

| Operation | Transaction | Tables Modified | Authority |
|-----------|------------|-----------------|-----------|
| acquire_lease | IMMEDIATE txn | mission_supervisor_leases | First writer wins (INSERT OR IGNORE) |
| renew_lease | IMMEDIATE txn | mission_supervisor_leases | Owner match + epoch match + unexpired |
| add_slices | IMMEDIATE txn | mission_roadmap_slices | Mission exists check |
| run_supervisor_tick | Two txns | (1) supervisor state (2) R2 mission_state | Fencing epoch verified in each |
| mark_slice_completed | One IMMEDIATE txn | mission_roadmap_slices + R2 mission_journal (via compare_and_transition) | Fencing epoch verified before R2 call |
| crash_recovery | Read-only | All tables | Consistency check: detect orphaned state |

**Crash recovery rules:**
1. If `mission_supervisor_state` shows `current_slice_id` set but slice not completed → slice is incomplete, re-execute
2. If lease expired and no new lease acquired → safe to acquire new lease
3. If lease owned by another supervisor AND not expired → wait or abort
4. If slice marked completed but R2 journal has no matching operation → state is inconsistent, log and block
*** End Patch

### public_api
```python
# Roadmap management
def load_roadmap(conn, roadmap) -> LoadResult
def get_roadmap_slices(conn, mission_id) -> list[SliceRecord]

# Supervisor lifecycle
def acquire_supervisor_lease(conn, mission_id, supervisor_id, ttl_seconds) -> LeaseResult
def renew_supervisor_lease(conn, mission_id, supervisor_id) -> LeaseResult
def release_supervisor_lease(conn, mission_id, supervisor_id) -> None

# Supervisor loop
def run_supervisor_tick(conn, mission_id, supervisor_id, executor_fn) -> TickResult
def get_next_slice(conn, mission_id) -> Optional[SliceRecord]
def mark_slice_completed(conn, mission_id, slice_id, outcome, evidence) -> TransitionResult
def mark_slice_blocked(conn, mission_id, slice_id, blocker) -> TransitionResult

# Recovery
def recover_supervisor_state(conn, mission_id) -> Optional[SupervisorState]
def detect_incomplete_slices(conn, mission_id) -> list[SliceRecord]
```

### failure_modes
1. **Two supervisors compete for lease** → lease TTL + atomic acquire prevents overlap
2. **Supervisor crashes mid-slice** → lease expires, next supervisor detects incomplete slice
3. **Slice produces same error repeatedly** → max_attempts_per_finding limit, backoff, queue_exhausted transition
4. **Roadmap has circular dependencies** → validated at load time (topological sort)
5. **Slice references nonexistent dependency** → load-time validation rejects
6. **SQLite lock contention** → WAL mode + BEGIN IMMEDIATE (proven in K9)
7. **Replay after crash** → operation_id idempotency from R2 journal

### security_constraints
- Supervisor lease requires unique supervisor_id (no anonymous supervisors)
- Lease TTL hard-capped at 1 hour (configurable but bounded)
- No file system writes outside the database
- No network calls in the supervisor core (delegation is via executor callback)
- Max slices per roadmap bounded (prevent infinite roadmap creation)

### tests
1. **crash_recovery**: supervisor crashes mid-slice → lease expires → new supervisor recovers → detects incomplete → continues
2. **two_supervisor_race**: two processes try to acquire lease → exactly one succeeds → other gets `lease_held`
3. **replay_idempotency**: same slice completed twice with same operation_id → second is replayed, not duplicated
4. **queue_exhaustion**: slice fails max_attempts times → transitions to queue_exhausted → does NOT become completed
5. **blocker_resume**: slice blocked → blocker resolved → slice retried → succeeds
6. **terminal_state**: completed mission → no further slices dispatched
7. **fault_injection**: random failures during slice execution → consistent state after each failure
8. **lease_expiry**: lease not renewed → expires → another supervisor can acquire
9. **roadmap_validation**: circular deps, missing deps, empty roadmap all rejected
10. **slice_dependency_order**: slices execute in dependency order
11. **outcome_classification**: each outcome type correctly persisted and queryable
12. **supervisor_state_recovery**: state table correctly reflects progress after restart

### acceptance_criteria
- [ ] `run_supervisor_tick` processes exactly one slice per call
- [ ] Lease prevents two concurrent supervisors on the same mission
- [ ] Crash recovery detects incomplete slices and resumes correctly
- [ ] Operation_id replay is idempotent (same result, no duplication)
- [ ] queue_exhausted does NOT transition to completed
- [ ] Blocker → resolution → retry → success flow works end-to-end
- [ ] All 12+ test categories pass
- [ ] Migration is idempotent (multiple `connect()` calls safe)
- [ ] Zero changes to R2 mission-state module
- [ ] `py_compile` passes on all new files
- [ ] `git diff --check` passes

### stop_conditions
- Would require modifying `kanban_mission_state.py` (R2 is frozen)
- Would require network calls in supervisor core
- Would require changes to live Hermes gateway
- Circular dependency detected in module imports
- Migration would corrupt existing K9 data

### rollback
- Revert commits on branch `codex/kanban-autonomous-supervisor-r3`
- New tables are additive; no rollback migration needed
- R2 mission-state remains functional regardless

### next_phase
R4 — Durable Human Decision Channel

---

## R4 — Durable Human Decision Channel

### objective
Implement a transport-neutral channel for creating, persisting, displaying, receiving, validating, and resolving human decision gates — decoupled from any specific messaging platform.

### facts
- R3 provides the supervisor loop with human_gate status support AND programmatic slice ingestion
- The R1 schema defines `active_human_gate` with `gate_id`, `gate_type`, `version`, `status`, `prompt_fingerprint`, `resolution_ref`
- `next_safe_action` for human_gate is fixed: `{action: "await_human", executable: false, ...}`
- Current human interaction: CLI prompts, Telegram messages, WebUI — all platform-specific
- No durable question/answer mechanism exists today

### hypotheses
- H1: A `human_decisions` table can persist gate lifecycle independently of transport
- H2: Transport adapters can be registered as plugins without core changes
- H3: Decision_id validation prevents stale or cross-mission responses

### decisions
- D1: `human_decisions` table stores gate questions and responses
- D2: Transport is an ABC with `render_question` and `receive_response` methods
- D3: At least one adapter ships (CLI/filesystem fallback for testing)
- D4: Gates are versioned — responses to superseded versions are rejected

### exact_write_set
- `hermes_cli/kanban_human_gate.py` (new — gate lifecycle, transport ABC)
- `hermes_cli/kanban_human_gate_cli.py` (new — CLI adapter)
- `tests/hermes_cli/test_kanban_human_gate.py` (new)

### exact_non_write_set
- `hermes_cli/kanban_mission_state.py` (untouched)
- `hermes_cli/kanban_supervisor.py` (untouched — consumes gate API but doesn't change)
- Live Hermes gateway, Telegram, or messaging platforms

### migration_impact
- New table: `mission_human_decisions`
- Idempotent migration via lazy import pattern

### public_api
```python
class HumanGateTransport(ABC):
    def render_question(self, gate) -> str
    def receive_response(self, gate_id) -> Optional[HumanResponse]

def create_human_gate(conn, mission_id, gate) -> GateResult
def respond_to_gate(conn, gate_id, response) -> ResponseResult
def get_pending_gates(conn, mission_id) -> list[GateRecord]
def close_gate(conn, gate_id, decision_id) -> CloseResult
```

### tests
- create gate → render question → valid response → gate closed
- duplicate response → rejected
- response to superseded version → rejected
- cross-mission response → rejected
- gate persists across session restart
- two concurrent responses → exactly one accepted
- stale gate (mission moved on) → rejected

### acceptance_criteria
- [ ] Transport ABC defined with ≥1 adapter
- [ ] Gate lifecycle (create → pending → resolved) works
- [ ] Version-based staleness detection works
- [ ] Cross-mission gate responses rejected
- [ ] All test categories pass
- [ ] Zero changes to R2 or R3 modules

### stop_conditions
- Transport requires platform-specific secrets
- Gate resolution requires modifying mission-state schema

### rollback
- Revert commits. New tables additive.

### next_phase
R5 — Autonomous Roadmap Executor

---

## R5 — Autonomous Roadmap Executor

### objective
Build the mechanism that loads a versioned machine-readable roadmap, resolves dependencies, selects next slices, invokes senior consult at declared gates, executes TDD, manages PRs, and advances through phases autonomously.

### facts
- R3 provides the supervisor loop
- R4 provides the human decision channel
- Roadmap format needs to be defined (YAML/JSON with phases, slices, dependencies, gates)
- Senior consult integration exists (`codex-senior-consult`)
- PR management via `gh` CLI exists

### hypotheses
- H1: Roadmaps can be represented as directed acyclic graphs of slices
- H2: The supervisor can select and execute slices without human guidance for deterministic work
- H3: Senior consult can be invoked automatically at declared gates

### decisions
- D1: Roadmap format is JSON (stdlib) with explicit phase/slice/dependency/gate structure. YAML optional convenience wrapper in R5+.
- D2: Executor wraps the supervisor loop with roadmap-aware selection logic
- D3: Senior consult gates are declared in the roadmap and invoked automatically
- D4: PR creation uses `gh` CLI through the executor callback

### exact_write_set
- `hermes_cli/kanban_roadmap_executor.py` (new — roadmap loader, DAG resolver, executor)
- `hermes_cli/kanban_roadmap_schema.py` (new — roadmap validation)
- `tests/hermes_cli/test_kanban_roadmap_executor.py` (new)

### migration_impact
- No new tables (uses R3 tables)
- Roadmap files are versioned in the repo, not in SQLite

### public_api
```python
def load_roadmap_from_file(path) -> Roadmap
def validate_roadmap(roadmap) -> list[str]
def resolve_dependency_order(roadmap) -> list[list[str]]
def run_roadmap_executor(conn, roadmap_path, supervisor_id, config) -> ExecutorResult
```

### tests
- load valid roadmap → parse → validate
- detect circular dependency → reject
- resolve execution order respecting dependencies
- invoke senior consult at declared gate
- skip non-executable slices (human_gate, blocked)
- PR lifecycle (create → CI check → merge)
- replan after queue_exhausted
- scope creep prevention (no additions beyond roadmap)

### acceptance_criteria
- [ ] Roadmap loads and validates
- [ ] Dependencies resolved in correct order
- [ ] Senior consult invoked at declared gates
- [ ] PR lifecycle works (personal repo only)
- [ ] Replan after queue_exhausted works
- [ ] All test categories pass

### stop_conditions
- Roadmap format becomes too complex for validation
- PR operations require credentials not available

### rollback
- Revert commits. No schema changes.

### next_phase
R6 — End-to-End Canary

---

## R6 — End-to-End Canary

### objective
Demonstrate the complete supervisor lifecycle end-to-end: create mission → load roadmap → execute slices → handle blocker → consult senior → create human gate → receive response → handle queue exhaustion → replan → complete all phases → exact-head review → open PR → validate CI → merge → verify HEAD ancestry.

### facts
- R3-R5 implement all components
- Canary uses a synthetic test roadmap (no real product work)
- Canary runs without modifying live Hermes

### hypotheses
- H1: The full lifecycle can be demonstrated in a single test run
- H2: Session restart mid-canary can be simulated and recovered from
- H3: All states (planned → active → blocked → human_gate → queue_exhausted → completed) can be exercised

### exact_write_set
- `tests/canary/test_supervisor_canary.py` (new — end-to-end canary test)
- `tests/canary/fixtures/canary_roadmap.yaml` (new — test roadmap)
- `docs/superpowers/checkpoints/kanban-long-runtime-r6.md` (checkpoint)

### exact_non_write_set
- All R3-R5 implementation files (read-only, exercised by tests)

### tests
The canary itself is the test. Must exercise all 22 steps from the mission specification.

### acceptance_criteria
- [ ] All 22 canary steps pass
- [ ] Session restart recovery demonstrated
- [ ] Senior consult binding verified
- [ ] Human gate lifecycle complete
- [ ] Queue exhaustion + replan demonstrated
- [ ] Exact-head review passes
- [ ] Personal PR lifecycle complete
- [ ] HEAD ancestry of personal main verified

### stop_conditions
- Any canary step fails irreproducibly
- Requires live secrets or external services

### rollback
- Canary is test-only. Revert has no production impact.

### next_phase
R7 — Adoption and Hardening

---

## R7 — Adoption and Hardening

### objective
Document operation, recovery, observability, and limits. Add loop protection, per-slice budget, backoff, error handling, security policies. Integrate with Hermes entrypoint safely. Test upgrade from R2 and fresh installation. Run a real non-destructive mission.

### facts
- R3-R6 implement and validate the supervisor system
- Live Hermes integration requires careful gating
- Documentation must cover operators, not just developers

### exact_write_set
- `docs/superpowers/supervisor-operations.md` (new — operational guide)
- `docs/superpowers/supervisor-recovery.md` (new — recovery procedures)
- `hermes_cli/kanban_supervisor.py` (patch — add budget, backoff, loop protection)
- `tests/hermes_cli/test_kanban_supervisor.py` (patch — add hardening tests)
- `tests/canary/test_real_mission_canary.py` (new — real non-destructive mission test)

### migration_impact
- Possible schema additions for budget/backoff tracking
- All additive, idempotent

### acceptance_criteria
- [ ] Operational documentation complete
- [ ] Recovery documentation complete
- [ ] Loop protection prevents infinite retries
- [ ] Per-slice budget enforced
- [ ] Backoff policy works
- [ ] Fresh installation test passes
- [ ] Upgrade from R2 test passes
- [ ] Real non-destructive mission completes

### stop_conditions
- Integration would require modifying core Hermes modules
- Documentation scope becomes unbounded

### rollback
- Hardening is additive. Core R3-R5 unchanged.

### next_phase
Mission complete. Self-hosting demonstrated.

---

## Transition to Self-Hosting

After R6 validation:
1. Create a durable mission representing the remaining roadmap
2. Import R4-R7 as planned slices
3. Let the R3 supervisor select and persist next slices
4. Demonstrate session restart continuation
5. Use the supervisor as source of truth for progress

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| SQLite WAL contention under load | Medium | Proven pattern from K9; BEGIN IMMEDIATE |
| Lease TTL too short for long slices | Medium | Renewable lease with heartbeat |
| Senior consult timeout during gate | Low | Retry with replacement; not blocking |
| Roadmap format too rigid | Medium | YAML with extension points |
| Migration breaks existing DB | High | Idempotent CREATE TABLE IF NOT EXISTS only |

## Fingerprint

```
roadmap_version: 1.0.0
created: 2026-08-03T00:00:00Z
phases: 5 (R3-R7)
total_slices_estimated: 25-35
base_sha: 6a31a89b0668d255e7e110d3e029da92a4c794db
```
