# Claw v2 — Internal Wiring

> Architectural reference for Claw / Dr. Strange to consult during refactors,
> debugging, and self-improvement. Not part of boot context. Read on demand.

---

## meta

```yaml
describes_commit: 3cca79f+pr1b-a-skill-expand-job
doc_version: 2.1
last_verified: 2026-06-04
verification_method: manual + grep cross-check
anchor_strategy: symbol_only  # path:symbol, no line numbers
audience: claw_v2  # consumed by the agent itself
```

If `git rev-parse HEAD` diverges substantially from `describes_commit`,
assume parts of this doc may be stale. The invariants below are the most
stable section; the layer detail decays fastest.

---

## 1. invariants

Non-negotiable. Any refactor that breaks one breaks operability even if
tests pass. Defend them.

```yaml
invariants:
  audit_trail:
    rule: Every decision emits an event
    examples: [dispatch_decision, llm_response, llm_fallback, llm_circuit_open,
               coordinator_*, tool_call, approval_pending, brain_turn_*,
               kairos_decide_failed, observation_window_freeze_*]
    why: Without trail, post-mortem debugging is impossible and self-improvement
         has no signal.

  no_silent_degrade:
    rule: Failure is visible to the agent
    examples:
      - CircuitBreaker opens explicitly per provider
      - Fallback is logged, not silent (anthropic ↔ openai only)
      - Sandbox violations raise PermissionError
      - Prompt-injection results in structured quarantine payload
      - Kairos errors emit kairos_decide_failed with classified error_kind
    why: Silent failure produces wrong actions taken with confidence.

  triple_and_gating:
    rule: Tool execution requires three independent authorizations in AND
    factors:
      - allowed_agent_classes  # who can see the tool
      - ToolPolicy.allowed_contexts  # from where it can be invoked
      - tier_check  # tier ≤ autoexec_max_tier OR approval_gate(...)
    why: Single-flag bypass is impossible by construction.

  kairos_external_mutation_gated:
    rule: Kairos handlers that mutate external state (post to social, push
          to a remote, send a real email) must either create a pending
          ApprovalManager record OR be opt-in via an explicit env flag.
    members:
      - _handle_auto_publish_social  # KAIROS_AUTO_PUBLISH_SOCIAL=1
      - _handle_auto_deploy          # KAIROS_AUTO_DEPLOY=1
    why: tick() runs inside system_approval_mode, which auto-approves any
         Tier 3 tool call with audit. Handlers that bypass ToolRegistry
         entirely (calling adapter.publish or subprocess directly) would
         escape every gate. The pending-record path forces a human action
         from Telegram before the side effect lands.

  scheduler_slow_jobs_off_tick:
    rule: CronScheduler handlers for LLM/subprocess/heavy autonomous jobs should
          enqueue durable agent_jobs and return quickly; execution belongs in a
          ClawDaemon background runner, not in daemon.tick()'s control path.
    migrated:
      - skill_expand -> scheduler.skill_expand  # PR1B-a, uses JobService + SkillExpandJobRunner
      - wiki_research -> scheduler.wiki_research  # PR1B-b, uses JobService + ScheduledBackgroundJobRunner
      - perf_optimizer -> scheduler.perf_optimizer  # PR1B-b, uses JobService + ScheduledBackgroundJobRunner
    pending_migration:
      - kairos_tick
    why: CronScheduler.run_due() still invokes handlers synchronously. Any
         provider call, code generation, verifier, subprocess, or research
         workload left inline can freeze the daemon tick and delay heartbeat /
         reconciliation observability.

  evidence_gate_meta_skip_sync_path:
    rule: The chain handle_text → _brain_text_response →
          _prepare_visible_brain_content → _record_evidence_gate_explicit_blocker
          must stay synchronous and on the same worker thread. The
          meta_introspection_guard (claw_v2/bot.py) uses
          `meta_introspection_context` (ContextVar in claw_v2/bot_helpers.py)
          to mark the turn as meta so the evidence-gate emits
          `evidence_gate_skipped_meta` and lets the brain reply pass through
          instead of pinning a failed `runtime=evidence_gate` row in the
          task ledger.
    enforced_by:
      - tests/test_meta_introspection_integration.py
        (test_complaint_no_evidence_gate_task + _via_asyncio_to_thread
        variant exercise the same-thread guarantee that asyncio.to_thread
        from telegram.py:1010 relies on)
    why: Converting any step to `async def` returns the coroutine before
         the `with` block exits, resetting the ContextVar before the gate
         reads it. Hector's complaints then become failed evidence_gate
         tasks again and the user sees the explicit_blocker template with
         internal IDs exposed (the exact 2026-05-17 P0-1 regression).

  final_render_brain_path_inside_meta_context:
    rule: When `_final_render` (claw_v2/bot.py) is applied to the brain
          path, it MUST run inside `_brain_text_response`, which itself
          runs inside the `with meta_introspection_context(...)` block
          opened by the meta_introspection_guard branch of
          `BotService.handle_text` (the one that captures
          `detect_meta_introspection_request` matches). Calling it from
          a caller frame after `_brain_text_response` returns is allowed
          for non-brain handlers (own ContextVar lifetime not relevant),
          but NEVER for the brain path on a meta turn.
    contract:
      - `_final_render` is render-then-sanitize only: NaturalLanguageRenderer.render
        followed by _sanitize_visible_chat_response.
      - It must NOT call _record_evidence_gate_explicit_blocker, touch
        task_ledger, emit evidence_gate_* events, or read
        current_meta_introspection_kind.
      - Both inner ops are idempotent regex transforms; the helper itself
        is idempotent (proven by tests/test_final_render_idempotency.py
        with adversarial inputs).
    enforced_by:
      - tests/test_final_render_idempotency.py
        (test_final_render_is_idempotent_on_adversarial_inputs +
         test_final_render_does_not_touch_evidence_gate +
         test_final_render_preserves_meta_skip_invariant)
    why: If gate logic creeps into `_final_render`, the gate would read
         the ContextVar from a caller frame outside the
         meta_introspection_context `with` block (ContextVar already
         reset in __exit__) and re-introduce the P0-1 regression — meta
         turns would create failed evidence_gate ledger rows again.
         Keeping the helper a pure formatter prevents that whole class
         of bug; the placement rule ensures the brain-path migration
         (P1-6 funnel) never breaks `evidence_gate_meta_skip_sync_path`.

  extract_verification_status_tolerant:
    rule: `_extract_verification_status` maps explicit verifier verdicts with
          markdown, prose, or separator noise to passed/failed/pending while
          preserving the exact legacy `Verification Status: passed` format.
    enforced_by:
      - tests/test_brain_tooluse_verify.py
    why: Coordinator checkpoints and session-state updates both depend on this
         parser. A verifier that says `**Verification Status:** passed.` should
         not downgrade a real pass to unknown because of formatting.

  verify_brain_tooluse_standalone:
    rule: `verify_brain_tooluse` verifies a brain tool-use turn by dispatching
          exactly one lane=`verifier` worker via `_dispatch_parallel`, carrying
          files_written, commands_run, and assistant claim evidence. It must not
          run coordinator research/synthesis phases and must default to pending
          when no explicit verdict is parsed or dispatch fails.
    enforced_by:
      - tests/test_brain_tooluse_verify.py
    why: Brain fallback tool-use already has concrete artifact evidence. The
         verifier primitive must score those artifacts directly; reusing the
         full coordinator cycle would verify an intermediate synthesis instead.

  brain_tooluse_verify_flag_gated:
    rule: The close path blocks substantive turns that ran without a passed
          verifier. PR2-B (2026-05-30): the blocker fires on
          `requires_verified_completion OR performed_mutation` (files_written /
          commands_run) REGARDLESS of the `BRAIN_TOOLUSE_VERIFY` flag — a
          Write/Edit/Bash turn closes failed/blocked, not completed_unverified,
          even with the flag off. Only read-only turns with no action-text and no
          error fall through to the conservative completed_unverified close.
          (This supersedes the prior flag-off-conservative behavior; the audit
          found 96% of the backlog had mutating tools while the text-only blocker
          almost never fired.) With the flag on, such a turn first calls
          `verify_brain_tooluse`; passed closes succeeded/passed, failed closes
          failed/failed, and pending falls through to the now mutation-aware
          blocker. If the coordinator is unavailable, verifier dispatch is
          skipped. Anthropic SDK tool hooks must persist minimal tool_input
          evidence (paths, commands, patterns) so the close path can derive
          files_written and commands_run from real tool effects without storing
          file contents. PR2-C (2026-05-30): the post-hoc reconciliation drain
          is the only path that resolves a `completed_unverified` row without a
          verifier pass, and only for the safe subset — read-only
          (`auto_close_as_unverified_lookup`), no error, past the 24h deadline.
          It transitions those rows to the existing terminal `status='cancelled'`
          with `verification_status='auto_closed_unverified_lookup'` (reuses an
          existing state — no schema migration, no new benign-success status;
          matches the established prod convention), so a substantive/mutating
          turn still never auto-closes as verified. The drain is OFF by default
          (`TaskLedger.drain_reconcilable_unverified(apply=False)`) with no
          daemon caller at this checkpoint; wiring the live transition is
          Checkpoint D. The drain summary telemetry exposes
          `scanned`/`scan_capped`/`limit` (the 100-row per-call scan cap,
          `RECONCILIATION_SCAN_LIMIT`); D must page or lift it so older
          read-only rows are not hidden behind the first page. C2: the apply
          path re-reads each row under the lock and re-runs the FULL read-only
          / no-error classification on fresh data before transitioning
          (fail-closed) — a row that gained a mutating tool or error between
          classify and apply is left for the human/verifier lane
          (`skipped_classification_changed`), distinct from a status/pending/
          overdue drift (`skipped_state_changed`). The batch rolls back on any
          mid-loop failure. D (2026-05-30): the drain runs with `apply=True`
          ONLY when `CLAW_PENDING_VERIFICATION_DRAIN_APPLY` (default OFF) is set,
          bounded by the drain's `max_scan` (daemon arg
          `pending_verification_drain_max_scan`, default 500; oldest-first,
          `limit+1` proves `scan_capped`) and `max_apply` (daemon arg
          `pending_verification_drain_max_apply`, default 10). PR1A
          (2026-06-04): daemon tick no longer calls the report or drain inline.
          It enqueues a `daemon.pending_verification_reconciliation` agent job
          through `JobService.enqueue` with resume key
          `daemon:pending_verification_reconciliation`; the
          `ClawDaemon.run_loop` starts a background
          `PendingVerificationReconciliationJobRunner` task that claims that
          kind with `claim_next`, emits the dry-run report, and applies the
          gated drain. The runner emits bounded lifecycle events
          (`daemon_reconciliation_job_started` / completed / failed) and
          reclaims stale `running` jobs of this kind before claiming so the
          active `resume_key` cannot block reconciliation forever after a
          process death or restart.
          Report/drain failures are contained in job retry/result handling, so
          scheduler / stale / orphan reconciliation stay out of the slow path.
    enforced_by:
      - tests/test_brain_tooluse_ledger.py
      - tests/test_completed_unverified_reconciliation.py
      - tests/test_daemon.py
      - tests/test_anthropic.py
    why: The signal that a turn needs verification must come from actual tool
         effects, not only a small allowlist of request text. The flag preserves
         rollout control because each verified turn spends an additional
         verifier-lane call.
```

---

## 2. message flow

```
Telegram → BotService.handle_text
   ↓
   Layer 1: pre-brain dispatchers (15 handlers in chain — see §5.1)
   Layer 2: CapabilityRouter (intent → chat | runtime_handoff | skill)
   Layer 3: CapabilityPreflight (binaries + sandbox policy)
   ↓
   BrainService → LLMRouter.ask(lane="brain")
   ├─ pre-hooks
   ├─ Adapter (Anthropic with session reuse for prefix cache)
   ├─ CircuitBreaker (opens per provider)
   ├─ Fallback (anthropic ↔ openai; codex no fallback — explicit)
   ├─ ObservationWindow gate (cost_per_hour, tool_calls_per_minute)
   ├─ post-hooks (sanitize)
   ↓
   Tool calls → ToolRegistry.execute
   ├─ allowed_agent_classes
   ├─ SandboxPolicy + DomainAllowlistEnforcer
   ├─ Tier 1/2: direct execute
   ├─ Tier 3: ApprovalGate → Telegram (raise ApprovalPending) | System (auto)
   ├─ sanitize_tool_output (anti prompt-injection)
   ↓
   Heavy tasks → TaskHandler.start_autonomous_task
   ├─ TaskLedger.create (SQLite ledger in data/claw.db)
   ├─ CoordinatorService — research → synthesis → impl → verify
   ├─ AgentLoop wrap (plan/exec/observe/verify/critique/replan)
   ├─ SubAgentService (assigned_agent → SOUL.md)
   ├─ ApprovalGate (tier 3)
   ├─ Verifier votes → _aggregate_verifier_votes → recommendation + risk
   ↓
   ObserveStream emits events at every layer (data/claw.db)
   ObservationWindowState gates / persists freeze state in
       data/observation_window.json (sibling of db_path).
```

---

## 3. lanes (LLMRouter)

```yaml
lanes:
  brain:    { tool_capable: true,  default: anthropic }
  worker:   { tool_capable: true,  default: anthropic }
  worker_heavy:
    tool_capable: true
    default: codex/gpt-5.5
    purpose: terminal/debugging/long tool runs
  verifier: { tool_capable: false, default: codex/gpt-5.5 read-only unless overridden }
  research: { tool_capable: false, default: codex/gpt-5.5 read-only }
  judge:    { tool_capable: false, default: codex/gpt-5.5 read-only }

NON_TOOL_LANES: [verifier, research, judge]
enforced_by:
  - LLMRouter._validate_lane_input  # blocks tool-loop config
  - CodexAdapter read-only sandbox for advisory lanes
```

### resilience

- `ProviderCircuitBreaker` (`claw_v2/retry_policy.py`) opens per provider after
  N failures, blocks calls until `opened_until`.
- Fallback chain:
  ```yaml
  anthropic: openai
  openai: anthropic
  codex:
    fallback_provider: null  # explicit — codex is ChatGPT subscription
  ```
- ObservationWindowState (`claw_v2/observation_window.py`) is an additional
  gate over LLM and tool execution: rolling 1h billable API cost, rolling 1min
  tool-call rate, hard denylist (git push -f, vercel --prod, gh release create,
  dynamic rm -rf). Subscription/local providers (`codex`, `ollama`, and
  `anthropic` when `CLAUDE_AUTH_MODE=subscription`) report notional costs only;
  those are ignored for budget freezes. Frozen state persists between restarts;
  `circuit_breaker:*` freezes auto-clear after `stale_freeze_seconds` (default
  3600s) since the rolling-window evidence has decayed by then. Manual freezes
  (manual_*) always require explicit unfreeze.

### provider-aware sessions

`BrainService.handle_message` (`claw_v2/brain.py`) consults
`memory.get_provider_session(session_id, provider)`. Local TTL 7200s;
Anthropic backend may evict earlier — `AdapterError` triggers retry with
fresh session.

### verifier consensus

`_aggregate_verifier_votes` (`claw_v2/brain.py`) reduces N votes to:

- `unanimous_approve`: ≥2 verifiers, all approve, risk ∈ {low, medium},
  no blockers, no missing_checks → `recommendation="approve"`.
- `single_verifier_approve`: 1 verifier, approve, risk=low, no blockers,
  no missing_checks → `recommendation="approve"`.
- otherwise → `consensus_status` ∈ {`disagreement`, `verifier_error`},
  `recommendation="needs_approval"`, risk forced to `high` (or `critical`).

**The `judge` lane is NOT invoked in this aggregator.** Judge is used in
`claw_v2/skills.py`, `claw_v2/learning.py`, and `claw_v2/kairos.py` — not
as a tiebreaker for brain consensus. Brain disagreement goes straight to
`needs_approval`.

---

## 4. tool distribution (ToolRegistry)

Central registry: `ToolRegistry` (`claw_v2/tools.py`) controls each
`ToolDefinition` along three independent axes (the triple-AND from §1).

### axis A — tier

```yaml
tiers:
  TIER_READ_ONLY: 1        # bypass approval, daemon-safe
  TIER_LOCAL_MUTATION: 2   # bypass approval, audited
  TIER_REQUIRES_APPROVAL: 3  # mandatory gate

autoexec_max_tier:
  rule: tier ≤ autoexec_max_tier → execute; else approval_gate(...)
  warning: Tier 3 ALWAYS calls approval_gate, even if autoexec_max_tier=3.
           autoexec_max_tier is a ceiling, never an override.
```

### axis B — allowed_agent_classes

Each tool declares its audience: `("researcher", "operator", "deployer")`.
`ToolRegistry.allowed_tools(agent_class)` filters per subagent.

### axis C — ToolPolicy

Orthogonal metadata:

```yaml
ToolPolicy:
  risk_level: [low, medium, high, critical]
  read_only: bool
  allowed_contexts: [telegram, daemon, brain, research, operator]
  requires_human: bool
  allowed_paths: [...]
  blocked_path_patterns: [...]  # SECRET_PATH_PATTERNS covers .env, *.pem, etc.
```

**Source of truth**: `claw_v2/config/tool_policies.json`. Loaded at module
import by `_load_tool_policies_from_config` (`claw_v2/tool_policy.py`),
fail-fast on schema/validation errors. The sentinel string
`"SECRET_PATH_PATTERNS"` in `blocked_path_patterns` expands to the
in-code tuple — secret patterns stay code-owned, not config-owned, so a
JSON edit cannot weaken the secret denylist. New tools or risk-level
changes require a JSON edit + tests + INTERNAL_WIRING bump.

### output sanitization

If `definition.ingests_external_content` is true, `sanitize_tool_output`
scans for prompt-injection. On `verdict=malicious`, returns structured
quarantine payload — never silently drops.

### sandbox

`sandbox_hook` (`claw_v2/sandbox.py`) validates each call against
`SandboxPolicy(workspace_root, capability_profile)` plus
`DomainAllowlistEnforcer` for network. Blocks with `PermissionError`.

### daemon auto-approve

`DAEMON_AUTO_APPROVE` (`claw_v2/tool_policy.py`) is a small set
(memory.read, wiki.search, git.status, etc.) the daemon may invoke without
human approval. Each member satisfies all four:

```yaml
- read_only: true
- risk_level: low
- "daemon" in allowed_contexts
- requires_human: false
```

---

## 5. dispatch layers

### 5.1 layer 1 — pre-brain dispatchers (15 handlers)

`BotService.handle_text` (`claw_v2/bot.py`) tries 15 handlers in order.
Each emits `dispatch_decision`. Order matters; no test enforces it. The
real call sites in `handle_text` (verified at HEAD `e218b07`):

| # | Handler symbol | Trigger / contract |
|---|---|---|
| 1 | `_handle_pending_computer_approval_response` | response to pending computer-use approval |
| 2 | `_maybe_handle_operational_alert` | "alertas operacionales" + parse |
| 3 | `_maybe_handle_boot_context_status` | boot context queries |
| 4 | `_maybe_handle_pending_tasks_query` | "tareas pendientes" / "pendientes" |
| 5 | `_maybe_handle_actionable_task_request` | runtime=Telegram + state-derived objective |
| 6 | `_maybe_handle_task_intent` | **gated OFF** by `CLAW_DISABLE_TASK_INTENT_ROUTER=1` (default) |
| 7 | `_maybe_handle_operational_status` | operational status questions |
| 8 | `_maybe_handle_change_status_question` | change-status questions |
| 9 | `_maybe_handle_capability_route` | classify_autonomy_intent → CRITICAL_TASK_KINDS gate |
| 10 | `_handle_pending_tool_approval_grant_response` | response to pending tool approval |
| 11 | `_handle_autonomy_grant_response` | "tienes autonomía", "full autonomy" |
| 12 | `_maybe_resolve_stateful_followup` | proceed-class continuation (state_handler) |
| 13 | `_maybe_handle_shortcut` | URL extraction, chrome browse, link review |
| 14 | `_nlm_handler.natural_language_response` | NotebookLM intent classifier |
| 15 | `_task_handler.maybe_run_coordinated_task` | coordinated autonomous task |

Then fallthrough to brain.

**Known fragility**: handler #5 vs #6 overlap; #6 is gated OFF for that
reason (`tests/test_dispatch_routing.py:121` codifies the over-capture as
xfail strict). The CRITICAL_TASK_KINDS list in #9 is hardcoded
(`{social_publish, pipeline_merge, deploy}`) — see TODO §7.

**dispatch_decision payload**: `handler`, `route` (intercepted | fall_through),
`reason`, `captured` (bool), `text_preview[:80]`, `text_len`, `session_id`.
Does NOT yet include the exact regex/intent label that matched (see Wave 2).

### 5.2 layer 2 — CapabilityRouter

`route_request` in `CapabilityRouter` (`claw_v2/capability_router.py`):

1. `classify_autonomy_intent(text)` → `AutonomyIntent`.
2. `route_request(intent, ...)` → `CapabilityRoute(route="chat" | "runtime_handoff" | "skill" | ...)`.
3. Hard rules:

```yaml
CRITICAL_TASK_KINDS:
  members: [social_publish, pipeline_merge, deploy]
  enforcement: requires_approval=true (no autoexec)
  TODO: move to config so self-improvement loop can extend at runtime.

sandbox_handoff:
  condition: current_environment="claude_code_sandbox" AND
             task_kind in _EXECUTION_REQUIRING_TASKS
  action: force runtime_handoff
```

### 5.3 layer 3 — CapabilityPreflight

`preflight_objective` in `CapabilityPreflight` (`claw_v2/capability_preflight.py`,
new in branch `feat/tactical-autonomy-fixes`). Returns `CapabilityPreflightResult`
with `task_kind`, `risk_tier`, `plan`, `checks: list[CommandPreflight]`,
`blockers: list[str]`, `allowed: bool = not blockers`.

Blocker reasons are legible: `command_not_found:poetry`,
`policy_blocked:codex:profile_violation`. Persisted by
`TaskHandler.record_blocked_task` into ledger as `error="; ".join(blockers)[:1000]`,
plus `metadata["blockers"]` and `artifacts["preflight"]`.

### 5.4 layer 4 — CoordinatorService

`CoordinatorService` (`claw_v2/coordinator.py`). Four phases sequential
within a task; tasks parallelizable across coordinator instances.

```yaml
phases: [research, synthesis, implementation, verification]
parallelism:
  max_workers: 4   # per CoordinatorService
  scope: across tasks; phases within a task are sequential.

scratch_dir: ~/.claw/scratch/<task_id>/
  persists: research/*.json, synthesis.md, implementation/*.json, verification/*.json
  resume: TaskLedger.list(statuses=("running",)) → _resume_autonomous_record
```

### 5.5 layer 5 — AgentLoop

`AgentLoop` (`claw_v2/agent_loop.py`):

```yaml
cycle: [plan, execute, observe, verify, critique, replan]
budget: max_iterations: int = 3   # ONLY guard today
TODO: add max_cost_usd, max_wallclock_s (Wave 2)
critic_runs_when: verdict != passed
on_exhaustion: outcome="exhausted", full history returned
```

### 5.6 layer 6 — SubAgentService

Named subagents (Alma, Hex, Lux, Rook, …) discovered by scanning
`definitions_root` in `SubAgentService` (`claw_v2/agents.py`).

```yaml
subagent_layout:
  SOUL.md:       role + provider/model in "- **Model:**" line  (required)
  HEARTBEAT.md:  per-turn contract                              (required)
  USER.md:       user-facing identity                           (required)
  skills/<name>/SKILL.md:  tool/skill definitions               (optional)
```

`_parse_provider_and_model` falls back **silently** to
`("anthropic", "claude-sonnet-4-6")` if `- **Model:**` line is missing.
Bug if typo'd. See TODO §7.

### 5.7 layer 7 — ApprovalGate

`ApprovalGate` factory (`claw_v2/approval_gate.py`):

```yaml
build_telegram_approval_gate:
  creates: pending record with HMAC token
  notifies: optional notifier(pending) → Telegram
  raises: ApprovalPending  (NOT PermissionError)
  user_command: /approve <id> <token>

build_system_auto_approve_gate:
  creates: pending record
  immediately: approve_internal (with audit trail)
  used_by: [daemon, Kairos, heartbeat]

approved_tool_invocation:
  type: one-shot context manager
  purpose: allow retry after approval without re-prompting
```

Gate selection:

```yaml
mechanism: ContextVar (_DAEMON_REASON)
setter: system_approval_mode(reason)  # context manager
default: telegram gate
inside_block: system auto-approve gate
```

### 5.8 layer 8 — Kairos (proactive)

`KairosService` (`claw_v2/kairos.py`). 30-min default tick, decides via
`router.ask(lane="judge")`, executes one of 19 action handlers per tick
(`notify_user`, `dispatch_to_agent`, `approve_pending`, `run_skill`,
`wiki_*`, `site_monitor`, `auto_publish_social`, `auto_deploy`,
`gmail_digest`, `generate_skill`, `nlm_wiki_sync`, `a2a_send`,
`publish_task`, `claim_task`, `morning_video_brief`, `daemon_health_check`).

Errors in `_decide` emit `kairos_decide_failed` with `error_kind` ∈
{`codex_timeout`, `circuit_open`, `timeout`, `general`}. Codex without
fallback is invariant (§6); KAIROS just defers to next tick.

**Limitation**: Kairos publishes tasks to the board / sends bus messages
but does NOT directly invoke `AgentLoop` or `CoordinatorService`. It is a
router-lite, not a full agent. Fixing that is Wave 2 in the plan.

**Mutating handlers** (`auto_publish_social`, `auto_deploy`) default to
draft + pending approval — they call `approvals.create(...)` and emit
`kairos_auto_*_pending` instead of mutating external state. To run them
fully autonomously the operator sets `KAIROS_AUTO_PUBLISH_SOCIAL=1` or
`KAIROS_AUTO_DEPLOY=1`; default off. See invariant §1 `kairos_external_mutation_gated`.

---

## 6. do_not (prescriptive)

Self-improvement loop must reject these even if tests pass.

```yaml
do_not:
  - change: Grant tool access to verifier, research, or judge lanes
    why: Breaks advisory-only invariant.
    enforced_by: LLMRouter._validate_lane_input + CodexAdapter advisory sandbox

  - change: Add fallback codex → anthropic
    why: Codex is ChatGPT subscription. Silent fallback hides provider switch.
    enforced_by: LLMRouter fallback config (claw_v2/llm.py)

  - change: Bypass approval_gate for tier 3 tools when autoexec_max_tier=3
    why: autoexec_max_tier is CEILING, not override.
    enforced_by: ToolRegistry.execute

  - change: Silently drop sanitized tool output instead of returning quarantine payload
    why: Agent must see filtration to avoid blocking on missing real result.
    enforced_by: sanitize_tool_output

  - change: Remove audit emit from a new dispatcher or any layer
    why: Invariant audit_trail. Blind spot in post-mortem.

  - change: Hardcode CRITICAL_TASK_KINDS additions
    why: Self-improvement should add critical kinds at runtime, not in PR.
    proposed: move to config + emit critical_task_kinds_changed event on edit.

  - change: Auto-clear manual_* freezes
    why: Manual freezes are explicit operator decisions; only circuit_breaker:*
         freezes are evidence-backed by rolling windows and safe to TTL out.
    enforced_by: ObservationWindowState._load_state stale-freeze TTL guard.

  - change: Count subscription/local provider notional costs as billable budget
    why: Subscription usage is an operational run-budget signal, not API spend;
         blocking the bot on it makes the agent unavailable while paid
         subscription lanes are still usable.
    enforced_by: AppConfig.notional_cost_providers + ObservationWindowState

  - change: Call adapter.publish, subprocess git push, or any other direct
            external-state mutation from a Kairos handler without going
            through ApprovalManager.create or an explicit env opt-in.
    why: kairos.tick() wraps every action in system_approval_mode, so a
         direct call bypasses the pending-record audit trail and any human
         gate. New mutating handlers must follow the
         _autonomous_action_authorized(env_var) pattern.
    enforced_by: invariant kairos_external_mutation_gated (§1).

  - change: Convert handle_text, _brain_text_response, or
            _prepare_visible_brain_content to `async def`, or move the LLM
            call to a thread that copies the parent context, without first
            re-deriving the meta-skip flag from a non-ContextVar source.
    why: The meta_introspection_guard wraps _brain_text_response in
         `with meta_introspection_context(...)`. ContextVar resets in
         __exit__; if the wrapped call returns a coroutine (no await
         inside the with) or hands off to a context-copying executor, the
         flag is gone before _prepare_visible_brain_content reads it and
         meta complaints become failed evidence_gate ledger rows again
         (reopens P0-1).
    enforced_by: invariant evidence_gate_meta_skip_sync_path (§1) +
                 tests/test_meta_introspection_integration.py.

  - change: Add evidence-gate logic, task_ledger writes, observe emits of
            evidence_gate_*, or reads of current_meta_introspection_kind
            inside _final_render (claw_v2/bot.py). Or apply _final_render
            to the brain path from a caller frame outside the
            `with meta_introspection_context(...)` block.
    why: _final_render is the funnel for the incremental P1-6 migration
         (render+sanitize across the 17 return points of the Telegram
         path). If gate logic creeps in, or the helper is moved outside
         the meta-context `with` for the brain path, the ContextVar
         lifetime invariant breaks and meta complaints regress to failed
         evidence_gate rows + explicit_blocker templates (reopens P0-1
         through a different door).
    enforced_by: invariant final_render_brain_path_inside_meta_context
                 (§1) + tests/test_final_render_idempotency.py.
```

---

## 7. open TODOs

```yaml
todos:
  - item: tests/arch_invariants.py
    why: Import NON_TOOL_LANES, CRITICAL_TASK_KINDS, DAEMON_AUTO_APPROVE,
         SECRET_PATH_PATTERNS, _DAEMON_REASON. Fail if any disappears
         without doc update. Closes the loop on last_verified.

  - item: AgentLoop max_cost_usd + max_wallclock_s
    why: max_iterations=3 is poor budget proxy when each iter is Opus.
    plan_wave: 2

  - item: dispatch_decision matched_pattern field
    why: Today only handler/route/reason. Need exact regex/intent label
         to enable real "see how it thinks" replays.
    plan_wave: 2

  - item: Tool pivoting in ToolRegistry code
    why: SELF_HEALING_LOOP_CONTRACT lives only in prompt. LLM-respect inconsistent.
    plan_wave: 2

  - item: Brain pushback contract + prefill stress test
    why: 8 contracts in prompt, none authorize disagreement. Anthropic 2026
         sycophancy paper methodology applicable.
    plan_wave: 2

  - item: Goal hierarchy in BoardTask (parent_task_id, project_id)
    why: GoalContract type already has parent_goal_id. Board is flat.
    plan_wave: 2

  - item: Kairos invokes AgentLoop on goals
    why: Today Kairos is router-lite. To deliver results, must drive the loop.
    plan_wave: 2

  - item: LearningLoop auto-apply (close self-improvement loop)
    why: Today suggest_soul_updates only proposes; nobody applies.
    plan_wave: 3

  - item: Trust calibration on autoexec_max_tier
    why: Static ceiling. Should adjust per-(agent, task_kind) success_rate.
    plan_wave: 3

  - item: Vector memory cold path (Letta-style hot/cold)
    why: Embeddings stored as TEXT, retrieval falls back to LIKE.
    plan_wave: 3

  - item: SKILL0-style internalization
    why: Skills are cheat-sheets (retrieve-and-paste). Paper trains the model
         and progressively withdraws context. Aplicable a Claw skill registry.
    plan_wave: 4
```

---

## 8. quick reference

When refactoring, ask in order:

1. Does the change touch any invariant in §1 or any item in §6?
   → Stop. Read those sections. If still want to proceed, escalate.
2. Does the change move a symbol mentioned in this doc?
   → Update this doc in same commit. Bump `doc_version`, set
     `last_verified`, set `describes_commit` to new HEAD.
3. Does the change add a new layer, lane, gate, or tier?
   → Add to YAML in the relevant section. Add a `do_not` if the new
     element has a non-obvious failure mode.
4. Does the change touch `bot.py:handle_text` or `agents.py`?
   → Highest churn files. Re-verify all anchors that point into them.

## 9. observability quick-paths

To "see how it thinks" without sqlite3:

```bash
python -m claw_v2.cli.think tail --limit 20                  # latest events
python -m claw_v2.cli.think tail --type dispatch_decision    # routing only
python -m claw_v2.cli.think trace <trace_id>                 # full trace
python -m claw_v2.cli.think replay <session_id>              # session reasoning
python -m claw_v2.cli.think spending                         # cost rollup today
python -m claw_v2.cli.think circuit                          # observation window state
```

DB lives at `data/claw.db` (active) + `data/observation_window.json`
(circuit state). Bot does NOT need to be running for these.
