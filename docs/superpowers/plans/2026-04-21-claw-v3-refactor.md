# Claw v3 Refactor — Plan Definitivo

> **For agentic workers:** Execute PRs in strict order. Each PR must pass its acceptance criteria before starting the next. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose god-objects (`brain.py`, `bot.py`) and add durable execution, typed artifacts, and observability — without losing any existing capability.

**Summary:** Claw v2 works. The problem is not missing features but structural debt: `brain.py` (1,246 LOC, 5 responsibilities), `bot.py` (1,026 LOC, ~36 handlers), zero E2E tests. This plan adds safety nets first, then decomposes surgically.

**Primary bet:** `evals + trace context + idempotency` before any decomposition. Every PR must prove: "same input → same observable decision → same events → same result or difference explained."

**Constraints:**
- Single developer, system runs 24/7 in production
- No runtime shadow mode or production dual-run — test-time parity checks are OK (see PR#2)
- No Pydantic — `dataclasses` + `__post_init__` validation only
- No Strangler Fig coexistence periods — clean cuts validated by snapshot tests
- Bug found during refactor → separate PR, merge, rebase, continue

**Sources:** Original Plan v3, Gemma 4 API review, Gemma 4 think-mode review (AI Studio), Claude Opus analysis, self-audit v1, self-audit v2. Consolidated 2026-04-21.

**Existing infrastructure (do not duplicate):**
- `claw_v2/tracing.py` — already has `new_trace_context()`, `new_trace_id()`, `new_span_id()`, `TRACE_KEYS`
- `claw_v2/observe.py` — schema already has `trace_id`, `root_trace_id`, `span_id`, `parent_span_id`, `job_id`, `artifact_id`; `emit()` signature is `(event_type, *, lane=, provider=, model=, trace_id=, ..., payload=)` (keyword-only args, NOT positional dict)
- `claw_v2/eval.py` — already has `EvalCase`, `EvalResult`, `EvalSuiteResult`, `EvalHarness` using `LLMRouter`
- `claw_v2/llm.py:17` — already has `LLMRouter` class used by pipeline/evals/main

---

## Success Criteria

- [ ] `brain.py` ≤ 300 LOC, no approval/voting/persistence logic inside
- [ ] Every agent action has a typed lifecycle: `plan → execute → verify → outcome`
- [ ] Long-running tasks survive `kill -9` without duplicating external side effects
- [ ] "Why did Claw do X?" answerable via CLI with job_id, inputs, plan, approvals, tool calls, artifacts
- [ ] New agent = 1 definition file, 0 changes to existing code
- [ ] E2E test: Telegram → Handler → Skill → Approval → Execute passes
- [ ] Files touched or created by v3 ≤ 250 LOC (see "Future Decomposition Backlog" for exempt files)
- [ ] Core behavior identical Mac/Linux/Docker; platform-dependent capabilities degrade explicitly, never fail silently

---

## Zones That Must Not Change

| Module | Reason | Exception |
|--------|--------|-----------|
| `memory.py` core logic | Cohesive despite 1,547 LOC; corruption risk to facts/embeddings DB | PR#4 may add `jobs` table via standard migration pattern |
| `agents.py` personalities | Agent definitions (Hex, Rook, Alma, Lux, Kairos) must stay intact | Refactor HOW they execute, not WHAT they are |
| SQLite schema | Isolate logic errors from persistence errors in early PRs | Allowed migrations: PR#0.6 `idempotency_keys` table, PR#3 `artifacts` table, PR#4 `jobs` table. Each must specify owner PR, columns, indices, rollback SQL, and bump `PRAGMA user_version` |
| Approval HMAC + expiration | Security-critical; replay attacks if weakened | Only tighten, never loosen |

---

## PR#0 — Safety Net (Expanded)

**Goal:** Capture current behavior before touching anything.

**Files:**
- Create: `tests/test_e2e_snapshots.py`
- Create: `tests/fixtures/` (redacted real inputs)
- Create: `tests/conftest.py` (shared fixtures)

**Scope:** Not just `brain.handle_message`. Cover:
- [ ] Telegram command dispatch (all ~36 commands (26 explicit + ~10 from handler unpacks))
- [ ] Approval flow (request → approve/deny → execute)
- [ ] Pipeline/coordinator orchestration
- [ ] Memory lookup and fact retrieval
- [ ] Agent dispatch and routing decisions
- [ ] Tool execution dry-run

**Testing approach — Semantic snapshots (not text):**
```python
@dataclass
class BehaviorSnapshot:
    intent: str
    required_approval: bool
    selected_agent: str | None
    risk_lane: str
    tool_plan: list[str]
    emitted_events: list[str]
    artifact_types: list[str]
```
Compare decisions, not LLM text output. Text is fragile; decisions are stable.

**5 E2E tests minimum:**
- [ ] Simple message → brain → response
- [ ] Command with approval → approve → execute
- [ ] Pipeline multi-step → all steps complete
- [ ] Agent delegation → correct agent selected
- [ ] Error path → graceful degradation

Use fakes only at external boundaries: Telegram API, LLM provider, browser/terminal destructive execution. No partial internal mocks.

**Acceptance criteria:**
- [ ] `pytest tests/test_e2e_snapshots.py` passes with current code
- [ ] Snapshots capture decisions, not raw text
- [ ] All ~40 commands have at least 1 snapshot fixture

**Commit:** `feat(tests): add semantic snapshot safety net for v3 refactor`

---

## PR#0.5 — Extend Existing Eval Harness

**Goal:** Measure brain decision quality before decomposition. `brain.py` contains emergent undocumented behavior — evals catch what unit tests miss.

**IMPORTANT:** `claw_v2/eval.py` already exists with `EvalCase`, `EvalResult`, `EvalSuiteResult`, and `EvalHarness`. Do NOT create a parallel `evals.py`. Extend the existing module.

**Files:**
- Modify: `claw_v2/eval.py` (add BehaviorSnapshot comparison mode)
- Create: `tests/test_behavior_evals.py`
- Create: `evals/` (eval cases as JSON/JSONL — no PyYAML dependency)

**Scope:**
- [ ] Add `BehaviorEvalCase` to existing `eval.py` — compares semantic snapshots, not just substrings
- [ ] 10 baseline eval cases from production logs (redacted), stored as `.jsonl` in `evals/`
- [ ] Pass/fail with diff on intent, agent, risk_lane, tool_plan
- [ ] `pytest tests/test_behavior_evals.py` runs as part of CI

**Key rule:** Evals must pass before AND after every subsequent PR. If evals regress, the PR does not merge.

**Acceptance criteria:**
- [ ] 10 behavior eval cases pass against current brain
- [ ] Extension to existing eval.py is < 100 LOC added
- [ ] `make evals` target works
- [ ] Eval cases are JSON/JSONL (no YAML, no new dependencies)

**Commit:** `feat(evals): extend EvalHarness with semantic BehaviorSnapshot comparison`

---

## PR#0.6 — Trace Propagation Gap Audit + Idempotency Store

**Goal:** Audit existing trace propagation for gaps, then add idempotency for external side effects. Zero new dependencies.

**Context:** `claw_v2/tracing.py` already provides `new_trace_context()`, `new_trace_id()`, `new_span_id()`. `observe.py` schema already has `trace_id`, `span_id`, `job_id`, `artifact_id` columns. `observe.emit()` uses keyword-only args. The infrastructure exists — this PR finds where it's not wired up and adds idempotency.

**Files:**
- Audit: all `observe.emit()` call sites — flag any missing `trace_id`/`span_id` propagation
- Modify: call sites that don't propagate trace context (wire existing `tracing.py` helpers)
- Create: `claw_v2/idempotency.py` (~50 LOC)
- Modify: `claw_v2/memory.py` (add `idempotency_keys` table via standard migration)
- Create: `tests/test_idempotency.py`
- Create: `tests/test_trace_propagation.py` (assert every emit() in production paths has trace_id)

**Phase 1 — Trace gap audit:**
- [ ] Grep all `observe.emit()` calls, classify as "has trace_id" vs "missing"
- [ ] Wire `tracing.new_trace_context()` into missing call sites
- [ ] Add `test_trace_propagation.py` that asserts no emit() call lacks trace_id in E2E paths

**Phase 2 — Idempotency store:**

**Migration (explicit):**
```sql
-- Owner: PR#0.6 | Rollback: DROP TABLE IF EXISTS idempotency_keys
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    result TEXT
);
-- Bump PRAGMA user_version
```

**Idempotency keys for all external side effects:**
```python
@idempotent(key_fn=lambda ctx: f"{ctx.job_id}:{ctx.step}")
async def send_telegram(chat_id, text): ...
```
Covers: Telegram send, GitHub PR/comment, Linear, terminal execute, file modify, browser action.

**Acceptance criteria:**
- [ ] Every `observe.emit()` in production paths propagates trace_id (verified by test)
- [ ] Duplicate side effect calls with same key are no-ops
- [ ] Existing `observe.emit()` signature and call sites unchanged
- [ ] All E2E snapshots from PR#0 still pass

**Commit:** `feat(tracing): close trace propagation gaps and add idempotency store`

---

## PR#0.7 — Migration Discipline

**Goal:** Establish schema migration contract before any PR adds tables. Prevents ad-hoc migrations from conflicting.

**Files:**
- Modify: `claw_v2/memory.py` (formalize `PRAGMA user_version` tracking in `_migrate()`)
- Create: `tests/test_migration_discipline.py`
- Create: `docs/schema-migrations.md` (living doc: table owner, version, rollback SQL)

**Rules for all v3 migrations:**
- [ ] Every new table has an owner PR documented in `docs/schema-migrations.md`
- [ ] Every migration bumps `PRAGMA user_version` by 1
- [ ] Every migration has rollback SQL (DROP TABLE or ALTER TABLE DROP COLUMN)
- [ ] WAL mode confirmed (`PRAGMA journal_mode=wal`) — already enabled, but test it
- [ ] `busy_timeout` set to ≥ 5000ms for concurrent access during daemon restarts
- [ ] Migration test: create DB at version N, run migration, assert version N+1 and schema correct
- [ ] Migration test: rollback from N+1 to N works

**Planned migrations (registry):**
| PR | Table | user_version bump | Rollback |
|----|-------|-------------------|----------|
| PR#0.6 | `idempotency_keys` | +1 | `DROP TABLE IF EXISTS idempotency_keys` |
| PR#3 | `artifacts` | +1 | `DROP TABLE IF EXISTS artifacts` |
| PR#4 | `jobs`, `job_steps` | +2 | `DROP TABLE IF EXISTS job_steps; DROP TABLE IF EXISTS jobs` |

**Acceptance criteria:**
- [ ] `PRAGMA user_version` tracked and incremented by `_migrate()`
- [ ] `docs/schema-migrations.md` lists all planned tables
- [ ] Migration tests pass forward and rollback
- [ ] `busy_timeout` ≥ 5000ms verified

**Commit:** `feat(migrations): formalize schema migration discipline with version tracking`

---

## PR#1 — Brain Decomposition (1,246 → ~300 LOC)

**Goal:** Extract 4 services from `brain.py`. Keep `Brain.handle_message()` as the public facade — callers don't change.

**Files:**
- Create: `claw_v2/context_assembler.py`
- Create: `claw_v2/tool_dispatcher.py`
- Create: `claw_v2/verification_engine.py`
- Create: `claw_v2/brain_response.py` (brain-specific LLM call orchestration — NOT a new router; uses existing `LLMRouter` from `llm.py`)
- Modify: `claw_v2/brain.py` (becomes thin orchestrator)
- Create: `tests/test_context_assembler.py`
- Create: `tests/test_tool_dispatcher.py`
- Create: `tests/test_verification_engine.py`
- Create: `tests/test_brain_response.py`

**IMPORTANT:** `claw_v2/llm.py` already contains `LLMRouter` used by pipeline/evals/main. Do NOT create a competing `llm_router.py`. The extracted service handles brain-specific response orchestration (prompt assembly, retry logic) and delegates to the existing `LLMRouter`.

**Extraction rules:**
- [ ] `Brain.handle_message()` signature does not change
- [ ] Each service is a plain class with explicit constructor args (no globals, no singletons)
- [ ] No logic changes — pure structural move
- [ ] No bug fixes in this PR (separate PR if found)

**Risk: LLM behavioral drift.**
Changing prompt assembly order or context format can change LLM decisions even if code is "equivalent." Mitigation: evals from PR#0.5 must pass with zero regressions.

**Acceptance criteria:**
- [ ] `brain.py` ≤ 300 LOC
- [ ] All PR#0 snapshots pass
- [ ] All PR#0.5 evals pass
- [ ] No approval/voting/persistence logic in brain.py
- [ ] Each extracted service < 250 LOC

**Commit:** `refactor(brain): extract context, dispatch, verification, routing services`

---

## PR#2 — HandlerRegistry for bot.py

**Goal:** Replace ~40 hardcoded handlers with declarative registry.

**Files:**
- Create: `claw_v2/handler_registry.py`
- Modify: `claw_v2/bot.py` (becomes ~200 LOC wiring)
- Modify: `claw_v2/bot_commands.py`
- Create: `tests/test_handler_registry.py`

**Pattern:**
```python
@handler(command="research", tier=2, description="Deep research with sources")
async def handle_research(ctx: BotContext) -> None: ...
```

**Test-time parity check (not runtime shadow mode):**
Before cutting over, `test_handler_registry.py` must include a comparison test that runs the full command inventory through both the old dispatcher and the new registry, asserting identical routing, tier, and approval for each command. This is a test, not a production dual-run.

**Acceptance criteria:**
- [ ] `bot.py` ≤ 200 LOC
- [ ] All ~40 commands routed through registry
- [ ] Each command preserves its tier/approval level (verified by parity test)
- [ ] All PR#0 snapshots pass
- [ ] New command = 1 decorated function, 0 changes to bot.py

**Commit:** `refactor(bot): declarative HandlerRegistry replaces hardcoded dispatch`

---

## PR#3 — Typed Artifacts (Full)

**Goal:** Formalize the data contracts that PR#0.6 sketched.

**Files:**
- Create: `claw_v2/artifacts.py`
- Modify: `claw_v2/types.py`
- Modify: `claw_v2/brain.py` (emit artifacts)
- Modify: `claw_v2/pipeline.py`
- Create: `tests/test_artifacts.py`

**Dataclasses:**
- [ ] `PlanArtifact` — intent, tool_plan, risk assessment
- [ ] `ExecutionArtifact` — tool calls, results, timing
- [ ] `VerificationArtifact` — checks performed, pass/fail
- [ ] `ApprovalArtifact` — who, when, scope, HMAC
- [ ] `JobArtifact` — state transitions, checkpoints

Each artifact has `schema_version: int` for forward compatibility.

**Migration (explicit):**
```sql
-- Owner: PR#3 | Rollback: DROP TABLE IF EXISTS artifacts
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    schema_version INTEGER DEFAULT 1,
    job_id TEXT,
    trace_id TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_artifacts_trace ON artifacts(trace_id);
-- Bump PRAGMA user_version
```

**"Why did Claw do X?" interface:**
- [ ] Create: `claw_v2/artifact_store.py` — query layer over artifacts table
- [ ] Extend existing `/trace` command (bot.py:301) to also show artifacts for a trace_id
- [ ] Add `/why <job_id|trace_id>` command returning: inputs, plan, approvals, tool calls, artifacts, decision gates, errors
- [ ] Response format: structured JSON or formatted Telegram message

**Acceptance criteria:**
- [ ] All brain/pipeline outputs wrapped in typed artifacts
- [ ] Artifacts persist to SQLite via artifact_store.py
- [ ] `/why <id>` returns full decision chain
- [ ] All snapshots and evals pass

**Commit:** `feat(artifacts): typed artifacts with artifact_store and /why command`

---

## PR#4 — Durable JobService

**Goal:** Long-running tasks survive process restart. No duplicate side effects.

**Files:**
- Create: `claw_v2/jobs.py`
- Modify: `claw_v2/pipeline.py`
- Modify: `claw_v2/main.py`
- Modify: `claw_v2/bot.py` (add /jobs, /job_status, /job_cancel)
- Create: `tests/test_jobs.py`

**State machine:**
```
queued → running → waiting_approval → retrying → completed
                                    ↘ failed
                                    ↘ cancelled
```

**Key rules:**
- [ ] States: `queued`, `running`, `waiting_approval`, `retrying`, `completed`, `failed`, `cancelled`
- [ ] Each step has idempotency key (from PR#0.6)
- [ ] Optimistic locking on job rows (version column)
- [ ] Wrap existing pipeline/NLM jobs first, then migrate
- [ ] Do NOT rewrite workflows in this PR

**Transactional boundaries (critical — current pipeline has side-effect-before-persist bugs):**
Current pipeline calls `linear.update_status()` before `_save_run()` (pipeline.py:68 vs :94). If crash happens between, state is lost but side effect already fired. Fix:
- [ ] Step journal: persist step intent BEFORE executing side effect
- [ ] Each step gets `attempt_id` — idempotency key = `{job_id}:{step}:{attempt_id}`
- [ ] Classify steps: pure (retry safe), reservable (idempotent external), confirmed (non-reversible), compensable (has undo)
- [ ] Chaos tests: `kill -9` between EVERY pair of consecutive steps, not just "during running"

**JobStep model (persisted per step):**
```python
@dataclass(slots=True)
class JobStep:
    id: str
    job_id: str
    name: str
    state: Literal["pending", "running", "succeeded", "failed", "skipped"]
    attempt_id: str              # unique per attempt, part of idempotency key
    idempotency_key: str         # f"{job_id}:{name}:{attempt_id}"
    step_class: Literal["pure", "reservable", "confirmed", "compensable"]
    side_effect_ref: str | None  # e.g. "telegram:msg:12345" or "github:pr:67"
    result_artifact_id: str | None
    started_at: float | None
    completed_at: float | None
```

**Migration (explicit — bumps user_version per PR#0.7 discipline):**
```sql
-- Owner: PR#4 | Rollback: DROP TABLE IF EXISTS job_steps; DROP TABLE IF EXISTS jobs
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'queued',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS job_steps (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt_id TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    step_class TEXT NOT NULL DEFAULT 'pure',
    side_effect_ref TEXT,
    result_artifact_id TEXT,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
-- Bump PRAGMA user_version +2
```

**Acceptance criteria:**
- [ ] `kill -9` between any two steps does not lose or duplicate jobs
- [ ] `/jobs` command lists active jobs
- [ ] Pipeline and NLM workflows run as durable jobs
- [ ] Side effects never fire without prior step journal entry
- [ ] All snapshots and evals pass

**Commit:** `feat(jobs): durable JobService with state machine and idempotent steps`

---

## PR#5 — OpenTelemetry as Sink

**Goal:** Add OTel as an additional backend. `observe.py` remains the domain API.

**Files:**
- Modify: `claw_v2/observe.py` (add OTel exporter)
- Create: `claw_v2/telemetry.py`
- Modify: `requirements.txt`
- Create: `tests/test_telemetry.py`

**Key rules:**
- [ ] `observe.emit()` API does not change
- [ ] OTel receives traces/spans/metrics
- [ ] `observe.py` event store remains queryable for audit ("why did Claw do X?")
- [ ] OTel is for technical debugging; observe.py is for product audit
- [ ] Sampling controlled — not everything needs a trace

**Acceptance criteria:**
- [ ] OTel spans visible in Jaeger/console exporter
- [ ] `observe.py` query API unchanged
- [ ] All snapshots and evals pass

**Commit:** `feat(telemetry): add OpenTelemetry exporter behind observe.py`

---

## PR#6 — ProcessManager

**Goal:** Abstract platform-specific process management.

**Files:**
- Create: `claw_v2/process_manager.py`
- Modify: `claw_v2/daemon.py`
- Modify: `claw_v2/main.py`
- Create: `tests/test_process_manager.py`

**Scope:**
- [ ] Strategy pattern: `LaunchdManager`, `SystemdManager`, `DockerManager`
- [ ] Unified health/readiness/liveness checks
- [ ] Graceful shutdown with cleanup
- [ ] Portability test suite

**Acceptance criteria:**
- [ ] Health checks work on macOS
- [ ] Graceful shutdown completes without orphan processes
- [ ] All snapshots and evals pass

**Commit:** `feat(process): cross-platform ProcessManager with health checks`

---

## PR#7 — Capability Registry + Eval Expansion

**Goal:** Route work by capability, not hardcoded agent names. Expand eval coverage.

**Files:**
- Create: `claw_v2/capability_registry.py`
- Modify: `claw_v2/agents.py` (declarative capability manifests)
- Modify: `claw_v2/coordinator.py`
- Expand: `evals/` (30+ eval cases)
- Create: `tests/test_capability_registry.py`

**Scope:**
- [ ] Each agent declares capabilities in its definition
- [ ] Coordinator routes by capability match, not name
- [ ] Eval suite expanded to 30+ cases with explicit pass/fail thresholds
- [ ] Eval regression blocks merge

**Acceptance criteria:**
- [ ] Agent routing uses capability registry
- [ ] 30+ evals pass with documented thresholds
- [ ] New agent discoverable/routable without touching `KNOWN_AGENTS`

**Commit:** `feat(registry): capability-based agent routing and expanded evals`

---

## PR#8 — Claw-Core VPS (ADR Required First)

**Goal:** Validate Core/Edge split. ADR must be written and approved before any code.

**Prerequisites:**
- [ ] **ADR written** in `docs/decisions/` covering: protocol, latency budget, auth, retries, backpressure, degraded mode, secret management, version skew
- [ ] ADR approved by Hector

**Architecture:** Core (VPS) handles brain/LLM/memory. Edge (Mac) handles Telegram, browser, Computer Use, terminal.

**Key rules:**
- [ ] Core does not know about macOS
- [ ] Edge capabilities appear as `unavailable`/`degraded` when Mac is off, never fail ambiguously
- [ ] A2A protocol versionado with auth, retries, backpressure
- [ ] Contract tests Core ↔ Edge

**Acceptance criteria:**
- [ ] Core runs on VPS with Mac disconnected (degraded mode)
- [ ] Telegram messages still processed (text-only, no Computer Use)
- [ ] Reconnection restores full capabilities
- [ ] All evals pass in both modes

**Commit:** `feat(core): Claw-Core VPS spike with Edge degraded mode`

---

## Rollback Criteria (Any PR)

Revert immediately if:
1. **Eval regression** — any eval case that passed before now fails
2. **Latency degradation** — brain response time increases >20%
3. **Context loss** — agent forgets system instructions it previously remembered
4. **Asyncio deadlock** — `RuntimeError: Event loop is closed` or freezes not present before
5. **Side effect duplication** — Telegram messages or GitHub actions sent twice
6. **Approval bypass** — any action executes without required approval

---

## Risk Matrix

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM behavioral drift from prompt restructuring | Critical | Semantic evals (PR#0.5) before and after every PR |
| Side effect duplication after crash | High | Idempotency keys (PR#0.6) on all external actions |
| Approval security weakened during refactor | Critical | Never modify HMAC/expiration logic; only tighten |
| Race conditions in SQLite job state | High | Optimistic locking with version column (PR#4) |
| Core/Edge network partition | Medium | Explicit degraded mode, capabilities as unavailable |
| PII/secrets in traces/artifacts | Medium | Define redaction rules and TTLs before PR#5 |

---

## Anti-Patterns to Avoid

1. **Big Bang** — Never merge PR#1 + PR#2 + PR#3 together
2. **Bug fix during refactor** — Separate PR, merge, rebase, continue
3. **Over-abstraction** — No `AbstractRouter` for a single `LLMRouter`
4. **Multiple feature flags** — 1 global flag maximum, prefer clean cuts
5. **Runtime shadow/dual-run** — Too complex for solo developer; use test-time parity checks instead
6. **Pydantic for internal boundaries** — `dataclasses` + `__post_init__` is enough
7. **Strangler Fig coexistence** — No weeks of parallel paths; cut and validate

---

## Future Decomposition Backlog (Out of Scope for v3)

Files >250 LOC not addressed by this plan. Each needs its own dedicated PR after v3 stabilizes:

| File | Current LOC | Decomposition Strategy |
|------|-------------|----------------------|
| `bot_helpers.py` | 1,559 | Extract per-domain helpers (browse_helpers, nlm_helpers, pipeline_helpers) |
| `memory.py` | 1,547 | Extract `FactStore`, `EmbeddingStore`, `DreamEngine` — requires careful migration |
| `wiki.py` | 1,519 | Extract `WikiParser`, `WikiStore`, `WikiSearch` |
| `main.py` | 979 | Extract `AppBootstrap`, `ServiceWiring` |
| `kairos.py` | 875 | Extract `ScheduleEngine`, `KairosActions` |
| `tools.py` | 864 | Extract per-category tool modules (file_tools, web_tools, system_tools) |

These are **not blocked** on v3. They can proceed independently once v3 PRs 0–4 land and the safety net is in place. Do not attempt these during v3 to avoid scope creep.
