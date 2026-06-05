# Dr. Strange Runtime Hardening - Implementation Spec

date: 2026-06-04
last_updated: 2026-06-05
status: active - PR 0-4 and PR 6 complete; PR 5 gated; PR 7 next
author: Codex, reviewed against local main and GitHub PR state
related:
  - docs/superpowers/specs/2026-05-15-dr-strange-wave-0-design.md
  - docs/superpowers/specs/2026-05-15-dr-strange-agentspec-integration-design.md
  - docs/superpowers/specs/2026-05-01-petri-evidence-verifier-design.md

## Decision

Do this work, but do it in the corrected order below. Dr. Strange does not need a greenfield Hermes clone. It already has the main building blocks: workspace startup context, durable jobs, playbooks, generated skills, scheduler, coordinator, verifier paths, and graph projection. The required work is to turn those pieces into governed runtime contracts with temporal isolation.

The first safety target is not memory optimization. The first target is preventing daemon tick from executing slow or autonomous work inline, preventing control-path LLM calls from inheriting 300 second defaults, and stopping generated CodeSkills from becoming active without review.

## Implementation Status

Updated on 2026-06-05 after merging PR #70.

Current main:

- `origin/main` = `711ff7ea8a478642e1b084dcc5d4299c50612342`

Completed:

- PR 0 / P0: complete via #61.
- PR 1A: complete via #63.
- PR 1B-a: complete via #64.
- PR 1B-b: complete via #65.
- PR 1B-c: complete via #66.
- PR 2: complete via #67.
- PR 3: complete via #68.
- PR 4: complete via #69.
- PR 6: complete via #70, merge commit `711ff7ea8a478642e1b084dcc5d4299c50612342`.

Pending / gated:

- PR 5 - Prompt Capsule enforce: gated until 7 days of shadow telemetry or a representative replay suite proves no loss of required boot facts.
- PR 7 - Memory and retention governor: next implementable block from current `main`.
- PR 8 - Verification wiring hardening: pending.
- PR 9 - PropertyGraph lock and watermark: pending.

Implementation drift from the original plan:

- PR 1A / PR 1B were delivered as focused `JobService` + background-runner slices instead of a single generic `ScheduledJob(run_mode="agent_job")` and `claw_v2/job_runner.py` abstraction. The accepted runtime contract is now encoded by PR #70 architecture invariants.
- PR 3 completed the CodeSkill hotfix contract: generated CodeSkills land in `pending_review`, execution is restricted to `active`, sensitive generation is blocked before router/file writes, and governance events are auditable without raw prompt/tag leakage. Explicit `approve_skill()` / `reject_skill()` APIs remain future review-surface work unless a later block needs them.
- PR 6 also migrated `wiki_scrape` out of inline scheduler execution into `scheduler.wiki_scrape` background execution, closing a remaining slow-job gap discovered while adding the invariants.

## Verified Repository Facts

Historical baseline checked on 2026-06-04:

- `origin/main` is `a145b77 Merge PR #62: Baseline daemon-mediated commits 2026-06-02`.
- GitHub PR #61 is still open, mergeable, and clean: `fix/stop-the-bleeding-2026-06-02` at `e735634de275fdf87a2f06c9d388f8854ee01771`, base `main` at `a145b77fd10bc4aed084a2b87cd57a6b5123a70b`.
- PR #61 touches safety-sensitive files: `claw_v2/approval.py`, `claw_v2/bot.py`, `claw_v2/bot_helpers.py`, `claw_v2/chat_api.py`, `claw_v2/container.py`, `claw_v2/kairos.py`, `claw_v2/main.py`, `claw_v2/pipeline.py`, and tests.

These baseline facts are superseded for implementation tracking by the 2026-06-05 status above.

Repo-grounded code facts:

- `AgentWorkspace` already owns the SOUL / USER / MEMORY style layer. `claw_v2/workspace.py` defines `STABLE_CONTEXT_FILES` with `BOOT_PROTOCOL.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, `AGENTS.md`, `CLAUDE.md`, `BOOT.md`, `HEARTBEAT.md`, `TOOLS.md`, `MEMORY.md`, and `REQUIRED_FILES` for the critical subset.
- Startup context is large by construction: `_MAX_CONTEXT_CHARS_PER_FILE = 30_000`, `_MAX_DAILY_CONTEXT_CHARS_PER_FILE = 12_000`, `_MAX_STARTUP_CONTEXT_CHARS = 180_000`. `startup_context()` loads stable files, daily notes, config, memory facts, learning facts, recent session state, and task ledger snapshot.
- `StartupContextReport` reports loaded/missing/truncated files and total chars, but there is no formal `PromptManifest` with per-block trust, source, priority, hash, and budget.
- `JobService` already has the queue substrate. `agent_jobs` supports `queued`, `running`, `waiting_approval`, `retrying`, `completed`, `failed`, `cancelled`; records include `resume_key`, `attempts`, `max_attempts`, `checkpoints`, `result_json`, `worker_id`, and `next_run_at`. `claim_next()` already exists.
- `CronScheduler.run_due()` calls `job.handler()` directly. `ClawDaemon.tick()` runs stale-task reconciliation, orphan-job reconciliation, pending-verification reconciliation, scheduler, then heartbeat collection in one synchronous route.
- Pending-verification reconciliation visible in `daemon.tick()` is currently dry-run for the report path: `build_reconciliation_report()` documents that it does not mutate `agent_tasks`. There is also an opt-in drain behind `CLAW_PENDING_VERIFICATION_DRAIN_APPLY`, off by default.
- Scheduler registers potentially slow work inline: `kairos_tick`, `self_improve`, `auto_dream`, `learning_soul_suggestions`, `wiki_lint`, `wiki_confidence`, `wiki_research`, `wiki_scrape`, `skill_expand`, `a2a_process_inbox`, `perf_optimizer`, `pipeline_poll`, `pipeline_poll_merges`, scheduled sub-agent skills, and local render polling.
- `SkillRegistry` generated CodeSkills are executable Python files under `~/.claw/skills`. `Skill.status` defaults to `active`, `generate_skill()` registers without pending review, and `execute_skill()` runs only `active` skills. `skill_expand` cron calls `skill_registry.auto_expand()`.
- `PlaybookLoader` is a separate declarative Markdown mechanism under `claw_v2/playbooks/`, triggered by frontmatter keywords and injected only when matching.
- `LLMRouter.ask()` defaults to `timeout=300.0`. `AppConfig.provider_for_lane()` defaults `research` and `judge` to Codex in env defaults; `verifier` falls back to a critic provider, which is Codex when brain is Anthropic unless explicitly overridden.
- `CoordinatorService` already has `WorkerTask.timeout_seconds` and passes it to `router.ask()` when set. The gap is defaulting and enforcing timeouts by phase/role; synthesis and semantic distillation still call `router.ask()` without explicit timeout.
- `PropertyGraphProjection` has no explicit threading lock. `materialize()` materializes SQLite runtime data, including `observe_stream`, and `_materialize_observe_events()` scans `observe_stream ORDER BY id ASC` without a watermark.

## Corrected Wording

Use these wordings in follow-up docs and PR descriptions:

- Instead of "Falta JobRunner / JobWorker", say: "Falta un runtime generico de jobs sobre JobService: claim_next, executor registry, timeout, retry/fail, reaper y scheduler enqueue mode."
- Instead of "pending_verification_reconciliation puede llamar providers lentos", say: "En main, el reconciler visible parece dry-run; aun asi, no debe vivir en daemon.tick() porque cualquier evolucion hacia judge/verifier/provider calls bloquearia el control loop."
- Instead of "PR 4 - Skill Governance", say: "PR 3 - CodeSkill auto-activation guard; PR posterior - taxonomia Runbook/Playbook/CodeSkill."
- "Provider Role Policy despues del scheduler" is correct, but it must include explicit timeout per call-site, not just provider defaults.

## Non-goals

- Do not add a second queue beside `JobService`.
- Do not create `persona.py`, `memory_capsule.py`, or a parallel memory stack.
- Do not wire `PropertyGraphProjection.materialize()` into cron before lock and watermark exist.
- Do not combine all phases into one large PR.
- Do not implement new autonomous skill synthesis behavior before CodeSkill review gating.

## Core Invariants

1. No provider call, subprocess, git command, docker command, scraping call, verifier, judge, research operation, or generated-code expansion may run inline in `ClawDaemon.tick()`.
2. Control-path LLM calls must pass explicit `timeout <= 30.0`, or a static invariant test fails.
3. Codex is allowed for heavy coding only in async `agent_job` execution, not in synchronous daemon control path.
4. Generated CodeSkills must land in `pending_review`, not `active`.
5. `execute_skill()` must continue to execute only `active` skills.
6. `skill_expand` must either enqueue an `agent_job` or be disabled by flag; it must not run inline in cron.
7. `PropertyGraphProjection.materialize()` must not be scheduled as a full scan.
8. Startup context must expose a manifest before context budgets are enforced.

## PR Plan

### PR 0 - Merge or explicitly reject PR #61 first

Objective: avoid building runtime hardening on top of stale safety code.

Actions:

- Review PR #61 as the base operational-safety patch.
- Merge it if accepted, or explicitly close/rebase this implementation spec against the chosen alternative.
- Re-run focused safety tests touched by #61 before starting PR 1.

Acceptance:

- Local branch is rebased onto the chosen post-#61 `main`.
- The implementation PRs below do not modify the same safety paths blindly.

### PR 1A - Scheduler enqueue mode

Objective: make `CronScheduler.run_due()` return quickly for slow jobs by enqueuing durable `agent_jobs`.

Implementation:

- Extend `ScheduledJob` in `claw_v2/cron.py`:

```python
JobRunMode = Literal["inline", "agent_job"]
PayloadFactory = Callable[[], dict[str, Any]]

@dataclass(slots=True)
class ScheduledJob:
    name: str
    interval_seconds: int | None
    handler: JobHandler | None = None
    daily_at: str | None = None
    timezone: str | None = None
    last_run_at: float = 0.0
    runs: int = 0
    metadata: dict = field(default_factory=dict)
    run_mode: JobRunMode = "inline"
    job_kind: str | None = None
    payload_factory: PayloadFactory | None = None
    timeout_seconds: float | None = None
    max_attempts: int = 3
    skip_if: Callable[[], str | None] | None = None
```

- Add optional `job_service` and `observe` dependencies to `CronScheduler`.
- For `run_mode="inline"`, keep current handler behavior and compatibility.
- For `run_mode="agent_job"`:
  - evaluate `skip_if` before enqueue;
  - require `job_kind`;
  - call `payload_factory()` or use `{}`;
  - enqueue into `JobService` with metadata including `scheduled_job`, `timeout_seconds`, and due timestamp;
  - use a `resume_key` of `scheduled:{job.name}:{due_bucket}` unless the job overrides it;
  - emit `scheduled_job_enqueued`;
  - return without running the handler.
- Convert only the highest-risk cron jobs in this PR:
  - `self_improve`
  - `wiki_research`
  - `wiki_scrape`
  - `skill_expand`
  - `perf_optimizer`
  - scheduled sub-agent jobs

Defer low-risk or already bounded jobs unless they are trivial to move:

- `heartbeat`, `daily_metrics`, and `task_board_cleanup` may stay inline.
- `pipeline_poll` can stay inline in PR 1A only if a test proves its current path is bounded; otherwise enqueue it.
- `local_render_jobs` already claims durable jobs and may stay as a poller until PR 1B.

Telemetry:

- `scheduled_job_enqueued`
- `scheduled_job_skipped`
- `scheduled_job_error`
- `scheduled_job_enqueue_duplicate`

Tests:

- Existing scheduler tests still pass for inline jobs.
- A `ScheduledJob(run_mode="agent_job")` enqueues a job and does not call its handler.
- Skip reason emits `scheduled_job_skipped` and does not enqueue.
- A fake 90-second handler registered as `agent_job` does not block `daemon.tick()`.
- Duplicate resume key returns/skips the existing active job instead of enqueueing unbounded duplicates.

### PR 1B - Minimal JobRunner

Objective: execute queued jobs outside daemon tick, using the existing `JobService.claim_next()` API.

Implementation note, 2026-06-05:

The generic synchronous `JobRunner.tick()` pseudocode below is retained only
as historical design context. The implemented PR 1A/1B/PR 6 train landed
focused `JobService` + background-runner slices instead.

The pseudocode must not be read as a reliable hard-timeout mechanism: a
synchronous executor running in the same thread cannot enforce timeout if the
executor blocks before returning. Current landed behavior relies on moving slow
scheduler/control-path work out of `daemon.tick()`, explicit provider timeouts,
retry/reclaim semantics, and architecture invariant tests.

Hard/preemptive in-process timeout remains out of scope for this train unless
a later PR introduces process/thread isolation or cooperative cancellation.

Implementation:

- Add `claw_v2/job_runner.py`.
- Define:

```python
@dataclass(slots=True)
class JobExecutionContext:
    worker_id: str
    observe: Any | None
    timeout_seconds: float

JobExecutor = Callable[[JobRecord, JobExecutionContext], dict[str, Any]]
```

- Add `JobExecutorRegistry` as a small map: `kind -> executor + default_timeout_seconds`.
- Add `JobRunner.tick(limit: int = 1)`:
  - call `job_service.claim_next(worker_id=..., kinds=...)`;
  - find executor by `job.kind`;
  - execute with metadata timeout or executor default;
  - on success, `job_service.complete(job_id, result=...)`;
  - on exception, `job_service.fail(job_id, error=..., retry=True)`;
  - on timeout, `job_service.fail(job_id, error="timeout:...", retry=True)`.
- Add a stale-running reaper:
  - `JobService.reap_stale_running(older_than_seconds, kinds=None, retry_delay_seconds=...)`;
  - running jobs older than lease become `retrying` if attempts remain, otherwise `failed`.
- Add CLI entrypoint or runtime mode for the runner, e.g. `python -m claw_v2.job_runner --kinds scheduled.self_improve,scheduled.wiki_research`.

Executor registration in `main.py`:

- `scheduled.self_improve` -> existing `_self_improve_handler`
- `scheduled.wiki_research` -> `wiki.auto_research`
- `scheduled.wiki_scrape` -> `wiki.auto_scrape_sources`
- `scheduled.skill_expand` -> `skill_registry.auto_expand`, but PR 3 will make this safe or flag-disabled
- `scheduled.perf_optimizer` -> existing `_perf_optimizer_handler`
- `scheduled.sub_agent_skill` -> `sub_agents.run_skill(...)`

Tests:

- Runner claims only allowed kinds.
- Missing executor fails job with `executor_missing`.
- Executor success completes result JSON.
- Executor exception retries until max attempts, then fails.
- Timeout marks retry/fail and emits `job_runner_timeout`.
- Reaper moves stale `running` jobs back to `retrying` or `failed`.

### PR 2 - Provider Role Policy and explicit timeout invariants

Objective: separate role from lane and prevent Codex or 300-second defaults in control path.

Implementation:

- Add a role policy module, e.g. `claw_v2/provider_roles.py`.
- Define roles:
  - `control_judge`: synchronous control decision; no Codex; timeout <= 30.
  - `control_verifier`: synchronous verifier for user-facing mutation gates; no Codex unless explicitly configured and still timeout <= 30.
  - `critical_verifier`: strict verifier; timeout <= 60; fallback/defer on timeout.
  - `research_synthesis`: async or non-control research synthesis; timeout <= 120.
  - `heavy_coding`: Codex allowed only in `agent_job`.
- Keep `lane` for adapter behavior, but pass `role` through evidence/audit and route policy.
- Add helpers:

```python
def timeout_for_role(role: ProviderRole) -> float: ...
def provider_allowed_for_role(provider: str, role: ProviderRole, *, execution_context: str) -> bool: ...
```

- Update control-path call-sites to pass explicit timeout:
  - Kairos judge paths already use `_judge_timeout_seconds()`; keep invariant test around it.
  - Coordinator synthesis and semantic distillation must pass explicit timeouts.
  - Any verifier/judge call in bot/control flow must pass explicit timeout.
- Add Coordinator defaults:

```python
default_worker_timeout_seconds = 120.0
default_research_timeout_seconds = 90.0
default_verification_timeout_seconds = 60.0
default_implementation_timeout_seconds = 180.0
default_synthesis_timeout_seconds = 90.0
default_distillation_timeout_seconds = 60.0
```

- Preserve existing `WorkerTask.timeout_seconds` as an override.
- Fix `_inject_context()` to preserve `timeout_seconds`; current reconstruction drops it.

Tests:

- Static/AST test: no `router.ask(... lane="judge"|"verifier" ...)` in known control-path modules without explicit `timeout`.
- Static/AST test: no control-path call passes `timeout > 30` for `control_judge`/`control_verifier`.
- Coordinator worker without explicit timeout gets role/phase default.
- `_inject_context()` preserves timeout.
- Coordinator synthesis and distillation pass explicit timeout.
- Codex provider is rejected for synchronous control roles unless context is `agent_job` and role is `heavy_coding`.

### PR 3 - CodeSkill governance hotfix

Objective: stop autonomous generated Python from becoming active by default.

Implementation:

- Extend skill statuses:
  - `pending_review`
  - `active`
  - `deprecated`
  - `failed`
  - `rejected`
- Change `Skill.status` default to `pending_review`.
- Change `generate_skill()` to register generated skills with:
  - `status="pending_review"`
  - `tags` including `auto-generated` when produced by autonomous expansion
  - metadata fields if added: `generated_by`, `source_task`, `validation_hash`
- Keep `execute_skill()` restricted to `status == "active"`.
- Add explicit review API:
  - `approve_skill(name, reviewer, reason)`
  - `reject_skill(name, reviewer, reason)`
- Make `auto_expand()` return generated candidate names but never active skills.
- Add flag:
  - `CLAW_SKILL_AUTO_EXPAND_ENABLED=0` default
  - scheduler `skill_expand` enqueues only when enabled, otherwise emits skipped.
- Update Kairos `generate_skill` handler to create `pending_review` candidates only.

Tests:

- `generate_skill()` returns success but stored status is `pending_review`.
- `execute_skill()` rejects `pending_review`.
- `approve_skill()` transitions to `active`.
- `auto_expand()` cannot increase active skill count.
- `skill_expand` does not run inline in cron.

### PR 4 - Prompt Capsule manifest shadow

Objective: measure a governed startup-context manifest without changing prompt behavior.

Implementation:

- Add data types in `claw_v2/workspace.py` or `claw_v2/prompt_manifest.py`:

```python
@dataclass(slots=True)
class PromptBlock:
    block_id: str
    title: str
    source: str
    trust: Literal["system", "workspace", "user_profile", "memory", "session", "task_ledger", "generated"]
    priority: int
    budget_chars: int
    actual_chars: int
    included_chars: int
    sha256: str
    truncated: bool
    redacted: bool

@dataclass(slots=True)
class PromptManifest:
    mode: Literal["shadow", "enforce"]
    total_budget_chars: int
    total_actual_chars: int
    total_included_chars: int
    blocks: list[PromptBlock]
```

- Add env:
  - `CLAW_PROMPT_CAPSULE_MODE=shadow|enforce`, default `shadow`.
- In shadow:
  - build current startup context exactly as today;
  - build manifest and hypothetical capsule plan;
  - emit `prompt_capsule_shadow_diff`;
  - include manifest in `StartupContextReport.to_dict()`;
  - do not change system prompt text.
- Include per-block hashes after redaction/truncation.
- Trust and priority:
  - highest: `BOOT_PROTOCOL.md`, `IDENTITY.md`, `SOUL.md`, `USER.md`, `AGENTS.md`
  - medium: `TOOLS.md`, `BOOT.md`, `HEARTBEAT.md`, operational config
  - lower: `MEMORY.md`, daily notes, SQLite facts, session snapshots, task ledger snapshot

Tests:

- Startup context report includes `prompt_manifest`.
- Shadow mode returns byte-for-byte current context.
- Manifest includes stable block hashes, trust, source, priority, budget.
- Redaction happens before hashing/inclusion.
- `BOOT_PROTOCOL.md` missing still reports as missing as today.

### PR 5 - Prompt Capsule enforce

Objective: reduce startup context only after shadow telemetry shows acceptable recall.

Entry criteria:

- At least 7 days of shadow telemetry, or a representative local replay suite.
- No observed loss of required boot facts in startup context questions.

Implementation:

- Enforce total startup budget below the current 180k cap.
- Load daily notes, session history, DB facts, and task history on demand unless high priority.
- Guarantee `BOOT_PROTOCOL.md`, `SOUL.md`, `IDENTITY.md`, `USER.md`, and `AGENTS.md` are never truncated before lower-priority blocks.
- Add retrieval hooks for omitted lower-priority blocks.

Tests:

- Enforce mode respects total budget.
- Required files survive intact before lower-priority truncation.
- Startup context still emits `agent_startup_context` with manifest.
- Existing boot/context tests remain green.

### PR 6 - Architecture invariant tests

Objective: make the runtime contracts hard to regress.

Tests to add:

- Slow cron handlers marked `agent_job` do not block `daemon.tick()`.
- Known heavy cron names are not registered as inline.
- Control-path `judge`/`verifier` calls pass explicit timeout <= 30.
- Codex is not allowed for synchronous control roles.
- Generated CodeSkills are not active by default.
- `skill_expand` is not inline.
- `PropertyGraphProjection.materialize()` is not registered as a scheduled full scan.
- `StartupContextReport` includes `prompt_manifest`.

Implementation detail:

- Prefer AST/static tests for call-site invariants.
- Keep allowlists explicit and documented in the test file so intentional exceptions are reviewable.

### PR 7 - Memory and retention governor

Objective: classify memory by prompt residency.

Categories:

- `always_in_prompt`: boot identity, core operating contracts, explicit durable preferences.
- `retrieval_on_demand`: dated notes, old task details, low-confidence facts, long evidence.
- `never_in_prompt`: secrets, raw provider outputs, credentials, private dumps, untrusted content with instruction-shaped text.

Implementation:

- Add retention metadata to memory facts where available.
- Add startup filter that maps memory rows to one of the categories.
- Add retrieval path for omitted rows.
- Keep source, trust, confidence, and freshness in manifest.

Tests:

- Low-confidence or stale facts are not always injected.
- Secrets are never injected.
- Retrieval can surface omitted facts when explicitly requested.

### PR 8 - Verification wiring hardening

Objective: make verification depend on evidence, not self-claims.

Implementation:

- Audit verifier call-sites and `verification_status` transitions.
- Ensure coding verification profiles are connected where strict verification is expected.
- Ensure user-visible success claims require evidence manifests or verifier pass.
- Move any verification that can call provider/judge into bounded job execution unless it is a small `control_verifier` with explicit timeout.

Tests:

- A self-claim cannot promote `pending_verification` to `passed`.
- Missing external observation remains `pending_verification` or `blocked`.
- Provider timeout in verifier defers instead of blocking daemon.

### PR 9 - PropertyGraph lock and watermark

Objective: make graph materialization incremental and safe before scheduling it.

Implementation:

- Add a `threading.Lock` around `PropertyGraphProjection` connection operations or clearly document single-thread use and isolate per runner.
- Add graph metadata table:

```sql
CREATE TABLE IF NOT EXISTS graph_projection_state (
    source TEXT PRIMARY KEY,
    watermark TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- For `observe_stream`, store last processed `id`.
- Query `WHERE id > ? ORDER BY id ASC LIMIT ?`.
- For task/outcome/fact sources, use a monotonic chronological field as the
  primary watermark where one exists.
- Never schedule full materialization inline.

Watermark queries must use a monotonic chronological field, not string IDs.
For tables with UUID/string identifiers such as `agent_tasks.task_id`,
incremental materialization must use `updated_at` or `created_at` plus a stable
tie-breaker, for example:

```sql
WHERE updated_at > :last_updated_at
   OR (updated_at = :last_updated_at AND task_id > :last_task_id)
ORDER BY updated_at, task_id
```

Do not use `task_id > :last_task_id` as the primary watermark because UUID
ordering is lexicographic and not chronological.

Tests:

- Second materialize processes only rows after watermark.
- Concurrent materialize calls do not corrupt graph tables.
- `observe_stream` materialization uses bounded `LIMIT`.

## Migration Notes

- Existing active generated skills should not be silently deactivated in PR 3. Add a one-time audit command that lists active generated skills and asks for explicit review. New generated skills become `pending_review`.
- Existing cron persistence should survive adding `run_mode` fields. Job names remain stable.
- Existing `JobService` schema does not need a migration for PR 1A/1B unless stale reaper needs extra lease fields. Prefer deriving lease timeout from `started_at`, `updated_at`, metadata, and `attempts`.
- `ScheduledJob.handler` becomes optional only for `agent_job` mode. Inline jobs still require it.

## Rollback

- PR 1A: set converted jobs back to `run_mode="inline"` or disable their scheduler registration. Queued jobs remain visible in `agent_jobs`.
- PR 1B: stop the job runner process. Jobs remain `queued`/`retrying` until runner resumes.
- PR 2: revert role policy enforcement to warn-only by env flag, but keep explicit timeout call-site changes.
- PR 3: set `CLAW_SKILL_AUTO_EXPAND_ENABLED=0` and keep pending candidates inert.
- PR 4: shadow mode has no behavior change; disable event emission if noisy.
- PR 5: switch `CLAW_PROMPT_CAPSULE_MODE=shadow` to restore old prompt construction.
- PR 9: disable graph runner; existing graph tables are a projection and can be rebuilt.

## Recommended First Branch

Start after PR #61 is resolved:

`fix/runtime-hardening-scheduler-jobrunner`

First failing tests:

1. `tests/test_semantic_scheduler.py::test_agent_job_mode_enqueues_without_calling_handler`
2. `tests/test_daemon.py::test_slow_agent_job_cron_does_not_block_tick`
3. `tests/test_job_runner.py::test_runner_claims_next_and_completes`
4. `tests/test_job_runner.py::test_runner_timeout_retries_job`

Then implement PR 1A and PR 1B as separate commits or separate PRs, depending on review size.
