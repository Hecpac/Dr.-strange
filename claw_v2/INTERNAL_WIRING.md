# Claw v2 — Internal Wiring

> Architectural reference for Claw / Dr. Strange to consult during refactors,
> debugging, and self-improvement. Not part of boot context. Read on demand.

---

## meta

```yaml
describes_commit: fe99808+spec-002-self-improve-promotion-hotfix+spec-002-subprocess-bounded-pr-c+spec-002-approval-manager-pr-d+spec-002-promotion-tooling-phase-4+brain-delegation-tool+recovery-jobs-drain-c1+audit-m3-m4-offloop-emits-nonblocking-checkpoint-backup+audit-high-2026-06-11+audit-waves-2-3-2026-06-12+adapters-d1-split-2026-06-12+pasos-6-7-coordinator-resumable-2026-06-12+wal-generation-guard-2026-06-12+telegram-t1-t12-2026-06-12
doc_version: 2.21
last_verified: 2026-06-12
verification_method: manual + pytest + AST sentinel cross-check
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
  wal_generation_guard:
    rule: Every store holding a long-lived SQLite connection to the runtime DB
          (observe, memory, task_ledger, jobs, orchestration, capability_grants,
          property_graph) registers a StoreWalHealHandle; writers that exhaust
          locked retries call sqlite_runtime.heal_orphaned_wal, which — only
          when the -wal sidecar is gone from disk — closes ALL registered
          connections, clears an empty recreated -wal husk, and reopens them
          together so the process rejoins ONE WAL generation.
    why: 2026-06-12 incident — pytest run from the production repo root (by the
         runtime agent itself) unlinked data/claw.db-wal/-shm under the live
         daemon; every writer then failed "database is locked" forever and
         messages/events/task closes silently stopped persisting while the bot
         kept chatting. Two concurrent WAL generations writing the same DB risk
         corruption, so the heal is registry-wide and two-phase, never
         per-store. tests/conftest.py isolates DB_PATH so the suite can never
         touch the production DB again.
    enforced_by:
      - tests/test_sqlite_wal_heal.py

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
      - wiki_scrape -> scheduler.wiki_scrape  # PR6, uses JobService + ScheduledBackgroundJobRunner
      - perf_optimizer -> scheduler.perf_optimizer  # PR1B-b, uses JobService + ScheduledBackgroundJobRunner
      - kairos_tick -> scheduler.kairos_tick  # PR1B-c, uses JobService + ScheduledBackgroundJobRunner
      - self_improve -> scheduler.self_improve  # PR1B-c, enqueue + ScheduledBackgroundJobRunner (was inline subprocess+pytest+Codex auto_research+git)
      - pipeline_poll -> scheduler.pipeline_poll  # PR1B-c, enqueue + ScheduledBackgroundJobRunner (was raw ScheduledJob: git worktree+worker LLM+pytest+push, no skip gate)
      - pipeline_poll_merges -> scheduler.pipeline_poll_merges  # PR1B-c, enqueue + ScheduledBackgroundJobRunner
      - a2a_process_inbox -> scheduler.a2a_process_inbox  # PR1B-d, enqueue + ScheduledBackgroundJobRunner, added _maintenance_skip kill-switch (was router.ask per inbox task inline, no skip gate)
      - approval_sweep -> scheduler.approval_sweep  # PR-D1, enqueue + ScheduledBackgroundJobRunner; ApprovalManager.expire_due never runs inline in daemon.tick
      - scheduled sub-agent jobs -> scheduler.sub_agent  # PR1B-d, each job enqueues an {agent,skill,lane} payload (resume_key scheduler:sub_agent:<agent>:<skill>) to one shared off-tick runner; was run_skill->dispatch (provider) inline, default-on via _default_scheduled_sub_agents
      - auto_dream -> scheduler.auto_dream  # final leg, enqueue + ScheduledBackgroundJobRunner (was dream.run router.ask(lane=research) inline, no explicit timeout)
      - learning_consolidate -> scheduler.learning_consolidate  # final leg, enqueue + ScheduledBackgroundJobRunner, added _maintenance_skip kill-switch (was router.ask(lane=judge) inline, no skip gate)
      - learning_soul_suggestions -> scheduler.learning_soul_suggestions  # final leg, enqueue + ScheduledBackgroundJobRunner (was router.ask(lane=judge) inline)
    pending_migration: []  # CORE INVARIANT 1 CLOSED — no heavy scheduler handler runs inline in daemon.tick
    enforced_by: tests/test_architecture_invariants.py::test_no_default_on_scheduler_job_runs_heavy_work_inline_in_daemon_tick
                 (deny-by-default sweep at production default; _PENDING_INLINE_MIGRATION is now empty and may only stay empty)
    why: CronScheduler.run_due() invokes handlers synchronously. Any provider
         call, code generation, verifier, subprocess, or research workload left
         inline would freeze the daemon tick and delay heartbeat / reconciliation
         observability. Core Invariant 1 is now CLOSED: every slow/provider/
         subprocess/codegen scheduler job enqueues a durable agent_job and
         executes in a ClawDaemon background runner off-tick. The backstop fails
         if any future job re-introduces inline heavy work.

  self_improve_promotion_gate:
    rule: self-improve promotion actions must pass through BrainService
          critical-action verification and may not commit generated changes to
          the live HEAD by default. Promotion must also pass diff-scoped
          tooling checks; Ruff is required on touched Python files, Mypy is
          advisory until the baseline is green, and sensitive paths are reported
          explicitly under the same critical gate.
    chokepoints:
      - brain.RISK_FLOORS[promote] = critical
      - brain.RISK_FLOORS[self_improve] = critical
      - agents.GitWorktreeExperimentRunner -> brain.execute_critical_action(action=promote_<agent>)
      - agents.PromotionToolingGate runs uvx ruff check and uvx ruff format --check
        only on touched Python files from the promotion manifest. For existing
        files, historical baseline Ruff failures do not block; new files or new
        failures still fail the gate.
      - agents.PromotionToolingGate runs uvx mypy only as advisory and never
        blocks promotion on Mypy alone.
      - agents.PROMOTION_SENSITIVE_PATH_PATTERNS lists runtime / approval /
        scheduler / subprocess / architecture files that must be surfaced in
        the promotion report.
      - agents.GitBranchPromotionExecutor commits in an isolated detached worktree
        and attaches a claw/<agent>/<sha> branch when commit_on_promotion is enabled.
      - agents.GitBranchPromotionExecutor raises PromotionToolingError before
        applying changes if required Ruff tooling fails.
    enforced_by:
      - tests/test_brain_verify.py::PolicyFloorTests
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_worktree_runner_does_not_promote_without_critical_approval
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_git_branch_promotion_defaults_to_isolated_branch_when_commit_enabled
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_git_branch_promotion_ignores_live_head_state_flag
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_runs_only_on_touched_python_files
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_blocks_ruff_check_failure
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_blocks_ruff_format_failure
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_does_not_block_historical_baseline_ruff_failure
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_blocks_new_file_ruff_failure_even_when_baseline_is_red
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_mypy_failure_is_advisory
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_promotion_tooling_gate_reports_sensitive_paths
      - tests/test_worktree_runner.py::WorktreeRunnerTests::test_git_branch_promotion_blocks_ruff_failure_without_touching_live_head
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_self_improve_promotion_actions_have_critical_floor
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_branch_promotion_executor_does_not_accept_live_head_state_flag
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_branch_promotion_executor_runs_diff_scoped_tooling_gate
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_promotion_sensitive_path_denylist_covers_runtime_chokepoints

  computer_use_import_safe:
    rule: computer-use must be import-safe on headless hosts. Importing
          claw_v2.computer or claw_v2.main must not import pyautogui, and the
          runtime must not construct ComputerUseService when computer-use is
          disabled.
    chokepoints:
      - computer._load_pyautogui is the only pyautogui import path.
      - main._probe_pyautogui_display bounds pyautogui.size() with a sync-safe timeout.
      - main._setup_operational_services constructs ComputerUseService only when
        config.computer_use_enabled is true.
    enforced_by:
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_computer_module_does_not_import_pyautogui_at_module_scope
      - tests/test_computer_import_safety.py

  subprocess_bounded_execution:
    rule: Runtime subprocess execution must be time-bounded. New synchronous
          subprocess callers should use subprocess_runner.run_subprocess_bounded
          unless they have a local, explicit timeout and a documented reason.
          Async callers should use run_subprocess_bounded_off_loop rather than
          adding create_subprocess_exec to scheduler/runtime paths.
    chokepoints:
      - subprocess_runner.run_subprocess_bounded  # timeout + process-group terminate/kill + bounded output + event arg redaction
      - subprocess_runner.run_subprocess_bounded_off_loop  # asyncio.to_thread wrapper with cancellation signal for async callers
      - main._self_improve_handler  # pytest verification now uses bounded runner
      - agents.GitWorktreeExperimentRunner / GitBranchPromotionExecutor  # git ops now bounded
      - pipeline git branch/worktree/diff/push helpers  # git ops now bounded
      - telegram.TelegramTransport.start  # ps probe runs off-loop through bounded runner
    legacy_async_subprocess_exec_allowlist:
      - voice._transcribe_local
      - voice.extract_audio
      - voice._wav_to_ogg
      - voice._mp3_to_ogg
    enforced_by:
      - tests/test_subprocess_runner.py
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_subprocess_run_calls_in_runtime_code_have_timeouts
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_runtime_code_does_not_introduce_async_subprocess_exec
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_runtime_code_restricts_direct_subprocess_popen
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_runtime_code_does_not_use_shell_true_or_os_system
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_runtime_builder_and_git_probe_remain_sync
    why: git, pytest, gh, keychain, and ps calls can otherwise pin a worker
         thread or leave descendant processes alive after timeout. PR-C keeps
         build_runtime and _is_git_repo synchronous, avoids a create_subprocess
         migration, and bounds the real blocking callsites instead.

  approval_manager_single_source:
    rule: ApprovalManager remains the only approval source of truth. Approval
          hardening must extend the existing file-backed, HMAC-token,
          fcntl-locked records in place; no SQLite approval table, ApprovalStore
          adapter, or parallel channel may decide approval state.
    states: [pending, approved, rejected, expired, archived]
    chokepoints:
      - approval.ApprovalManager.reject  # terminal states cannot be mutated to rejected
      - approval.ApprovalManager.expire_due  # pending-only proactive expiry
      - main._setup_core_state  # startup expiry sweep
      - scheduler.approval_sweep -> ScheduledBackgroundJobRunner  # periodic off-tick sweep
      - config.AppConfig.approval_ttl_seconds  # default APPROVAL_TTL_SECONDS=900, env override APPROVAL_TTL_SECONDS
    action_hash_status: out_of_scope_until_execution_chokepoint_is_recabled
    enforced_by:
      - tests/test_approval.py::ApprovalManagerTests
      - tests/test_approval_runtime_wiring.py
      - tests/test_config.py::AppConfigDefaultsTests::test_approval_ttl_defaults_to_900_and_accepts_override
      - tests/test_config.py::AppConfigDefaultsTests::test_approval_ttl_validation_rejects_non_positive_values
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_no_default_on_scheduler_job_runs_heavy_work_inline_in_daemon_tick
    why: Expired approvals were only discovered lazily during approval, and
         reject() lacked terminal-state parity. Proactive expiry must not create
         a second approval database or run inline in daemon.tick.

  recovery_jobs_drained_off_tick:
    rule: recovery_jobs (the brain's "I promised to resume this" queue) must be
          drained by a runtime caller of resolve_recovery_job. The
          RecoveryJobDrainRunner (notify-and-close MVP) stays registered as a
          daemon background runner off-tick; losing the wiring regresses the
          queue to a cemetery + false promise of continuity (audit C1). Only
          STALE jobs are drained (>= RECOVERY_JOB_STALE_SECONDS old) so a
          freshly-queued promise is not dismissed before the user can continue.
    chokepoints:
      - daemon.RecoveryJobDrainRunner.run_once  # notify-then-resolve, never re-executes, stale-only + paced
      - main._setup_scheduler  # register_background_job_runner(name="recovery_drain"), gated on Telegram config
      - memory.MemoryStore.resolve_recovery_job  # finally has a runtime caller
    enforced_by:
      - tests/test_daemon.py::RecoveryJobDrainRunnerTests
      - tests/test_architecture_invariants.py::ArchitectureInvariantTests::test_recovery_job_drainer_stays_wired_into_runtime
    why: resolve_recovery_job had no runtime caller, so promised-but-abandoned
         requests accumulated forever. Auto-replay (re-injecting the request)
         is intentionally NOT the MVP — it stays a future opt-in to avoid
         re-running external side effects.

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
   (transport: concurrent_updates with per-chat ordering via _chat_lock;
    operator interrupt commands — /freeze, /approve, /approvals, /status,
    /action_abort... — bypass the chat lock so a long turn cannot block them)
   ↓
   Layer 1: pre-brain dispatchers (15 handlers in chain — see §5.1)
   Layer 2: CapabilityRouter (intent → chat | runtime_handoff | skill)
   Layer 3: CapabilityPreflight (binaries + sandbox policy)
   ↓
   BrainService → LLMRouter.ask(lane="brain")
   ├─ pre-hooks
   ├─ Adapter (Anthropic with session reuse for prefix cache)
   ├─ CircuitBreaker (opens per provider)
   ├─ Fallback (anthropic ↔ openai; codex no fallback — explicit;
   │   suppressed with llm_fallback_suppressed when the failed turn already
   │   executed tools — replay would duplicate side effects; brain retries
   │   honor the same tools_executed_before_failure marker and queue a
   │   recovery job instead)
   ├─ ObservationWindow gate (cost_per_hour blocks LLM calls and tier-2+
   │   tools until the rolling hour decays — auto-clears like token_window;
   │   manual freezes pause autoexec but keep LLM chat alive; subscription
   │   providers (Max/Pro) feed notional costs that are ignored;
   │   tool_calls_per_minute; token_window)
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
   ├─ entry A: brain calls mcp__claw__delegate_task (in-process SDK MCP
   │   server, attached in _build_options only when lane=brain AND a
   │   delegation_handler closure is on the LLMRequest; BotService injects
   │   the factory into BrainService at __init__; ack returned to the turn,
   │   result delivered later via autonomous_task_completed/_failed)
   ├─ entry B: pre-brain coordinated_task handler (autonomy_mode=autonomous
   │   + mode ∈ {coding, research} only)
   ├─ TaskLedger.create (SQLite ledger in data/claw.db)
   ├─ CoordinatorService — research → synthesis → impl → verify
   ├─ AgentLoop wrap (plan/exec/observe/verify/critique/replan)
   ├─ SubAgentService (assigned_agent → SOUL.md)
   ├─ ApprovalGate (tier 3)
   ├─ Verifier votes → _aggregate_verifier_votes → recommendation + risk
   │   (evidence beyond the advisory 12k-char rendering bound fails closed:
   │    evidence_pack_truncated blocker forces the human gate)
   ↓
   ObserveStream emits events at every layer (data/claw.db; turn_id
   expression index serves turn receipts; scheduler job observe_prune
   applies a 30-day retention in bounded hourly sweeps)
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

google_provider: advisory-only (D6 decision, 2026-06-12 — documented, not
  pruned). GoogleAdapter stays tool_capable=False, serves only the advisory
  lanes, and no fallback chain points to it. A Google tool loop would be a
  new project, not a flag flip.
```

### provider roles + timeouts

`lane` remains the capability/routing surface. `role` is the safety policy
surface for specific call-sites. PR2 adds `ProviderRole` and role policy
helpers in `AppConfig`:

```yaml
control_path_roles:
  control_judge:
    provider_default: brain_provider
    timeout_seconds: 30
    codex_allowed: false
  control_verifier:
    provider_default: brain_provider
    timeout_seconds: 30
    codex_allowed: false
  critical_verifier:
    provider_default: brain_provider
    timeout_seconds: 30
    codex_allowed: false

async_roles:
  heavy_coding:              { provider_default: worker_heavy_provider, timeout_seconds: 180 }
  research_synthesis:        { provider_default: research_provider, timeout_seconds: 90 }
  coordinator_worker:        { provider_default: worker_provider, timeout_seconds: 120 }
  coordinator_research:      { provider_default: research_provider, timeout_seconds: 90 }
  coordinator_verification:  { provider_default: verifier_provider_or_brain, timeout_seconds: 60 }
```

`LLMRouter.ask(..., role=..., timeout=...)` validates role/provider policy
before adapter execution. Control roles fail fast if configured for Codex or
if timeout exceeds 30s. Adapter timeout failures emit `llm_timeout` with
`role`, `timeout_seconds`, `provider`, `error_type`, and a redacted preview.
`request.timeout` is enforced at runtime by all three tool-capable adapters:
Codex (subprocess timeout), Anthropic (`asyncio.wait_for` around the SDK
turn, raising AdapterError reason=timeout), and OpenAI (per-HTTP-call
`client.with_options(timeout=...)`).

PR2 explicitly covers Kairos decision/notification checks, PlanGate
verification, critical action verifier votes, and Coordinator worker,
synthesis, and distillation calls. Other historical provider call-sites remain
lane-governed unless they declare a role in a later PR.

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

Brain-lane SDK tool names (preset tools and in-process MCP tools alike) are
enforced against these policies fail-closed in BOTH the PreToolUse hook and
`can_use_tool` (`runtime_policy.enforce`; unknown name → RuntimePolicyViolation).
`mcp__claw__delegate_task` (medium, not read_only, contexts `[brain]`) is the
brain's delegation tool: `_context_candidates` maps only the brain lane onto
the `brain` context, so coordinator workers cannot re-delegate recursively.

**Inline browser-drive backstop** (`_inline_browser_drive_reason`,
`claw_v2/adapters/anthropic_hooks.py`; re-exported by
`claw_v2/adapters/anthropic.py`): the PreToolUse hook denies — `brain` lane
only — any Bash call that would drive Chrome/CDP, a browser, or desktop
computer-use (high-confidence markers: peekaboo, playwright/selenium, Chrome
debug ports `:9250/:9222`, `webSocketDebuggerUrl`, `/json/list`; it also reads
a referenced local `.py` script's contents so `python3 _ig_publish.py` is
caught). The deny nudges the model to `delegate_task` instead. This is the
structural backstop to the prompt-level DELEGATION_CONTRACT: such work does not
fit the brain turn's 300s wall. Worker/`worker_heavy` lanes are NOT gated —
delegated coordinator work legitimately drives CDP.

**Detached-process backstop** (`_detached_process_reason`, same module and
re-export; T12, 2026-06-12): the PreToolUse hook also denies — `brain` lane
only — Bash that launches detached/long-lived background processes
(`nohup`/`setsid`/`disown`, or a trailing `&` combined with long-life markers:
sleep N / install / download / curl / wget). Motive: during the T10 lock storm
the brain improvised ghost background processes with no ledger/monitor/
notification and the work died silently. The deny nudges to `delegate_task`;
a trivial short `cmd &` is allowed and worker lanes are not gated.

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

### 5.1 layer 1 — pre-brain dispatchers

`BotService.handle_text` (`claw_v2/bot.py`) tries the handlers in order.
Each emits `dispatch_decision`. Order matters; no test enforces it. The
real call sites in `_handle_text_body` (verified 2026-06-10):

| # | Handler symbol | Trigger / contract |
|---|---|---|
| 0 | `_maybe_handle_brain_first_new_task` | semantic new_task + clear_goal → brain route |
| 1 | `_handle_pending_computer_approval_response` | response to pending computer-use approval (exact/word-boundary grant matcher) |
| 2 | `_maybe_handle_operational_alert` | "alertas operacionales" + parse |
| 3 | `_maybe_handle_boot_context_status` | boot context queries |
| 4 | `_maybe_handle_pending_tasks_query` | "tareas pendientes" / "pendientes" |
| 5 | `_maybe_handle_operational_failure_summary` | failure summary queries |
| 6 | `_maybe_handle_operational_status` | operational status questions |
| 7 | cleanup status / owner delegation / `_maybe_handle_telegram_imperative_request` | explicit operator imperatives; unresolved context → fallthrough_to_brain (never clarifies) |
| 8 | `_maybe_handle_actionable_task_request` | runtime=Telegram + state-derived objective; unresolved follow-up → fallthrough |
| 9 | `_maybe_handle_task_intent` | **gated OFF** by `CLAW_DISABLE_TASK_INTENT_ROUTER=1` (default) |
| 10 | `_maybe_handle_change_status_question` | change-status questions |
| 11 | meta introspection guard + `_maybe_handle_capability_route` | classify_autonomy_intent → CRITICAL_TASK_KINDS gate |
| 12 | `_handle_pending_tool_approval_grant_response` | response to pending tool approval |
| 13 | `_handle_autonomy_grant_response` | "tienes autonomía", "full autonomy" |
| 14 | `_maybe_resolve_stateful_followup` | proceed-class continuation (state_handler); stale options / no pending context → fallthrough |
| 15 | `_maybe_handle_shortcut` | URL extraction, chrome browse, link review |
| 16 | `_nlm_handler.natural_language_response` | NotebookLM intent classifier |
| 17 | `_task_handler.maybe_run_coordinated_task` | coordinated autonomous task |

Then fallthrough to brain.

**Routing-policy conformance (2026-06-10 audit, group 4)**: pre-brain
handlers never ask for clarification. When target/artifact/mission cannot
be resolved from the literal text + session_state, they emit a
fallthrough event and return None so the brain handles the turn. The
`task.continue_active_mission` patterns are anchored to whole-message
continuations ("Continúa", "procede por favor") — embedded verbs ("el
deploy sigue fallando") never enter the imperative router.

**Known fragility**: handler #8 vs #9 overlap; #9 is gated OFF for that
reason (`tests/test_dispatch_routing.py:121` codifies the over-capture as
xfail strict). The CRITICAL_TASK_KINDS list in #11 is hardcoded
(`{social_publish, pipeline_merge, deploy}`) — see TODO §7.

**dispatch_decision payload**: `handler`, `route` (intercepted |
fall_through | brain_shortcut | explicit_command), `reason`, `captured`
(bool), `text_preview[:80]`, `text_len`, `session_id`. `brain_shortcut`
means the dispatcher only enriched the prompt and the brain handled the
turn (`captured=false`).

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

mode_phases:  # planned_phases_for_mode (artifacts.py) + _build_coordinator_tasks (bot_helpers.py)
  coding|ops|publish|browse: [research, synthesis, implementation, verification]
    # implementation worker: lane=worker (tool-capable claude_code preset),
    # cwd=workspace_root; ops/publish/browse added 2026-06-10 — reachable
    # ONLY via brain delegation (entry A in §2), the pre-brain gate stays
    # {coding, research}.
  research: [research, synthesis, verification]
  other: [research, synthesis, verification]  # text-only fallback

scratch_dir: ~/.claw/scratch/<task_id>/
  persists: research/*.json, synthesis.md, implementation/*.json, verification/*.json
  resume: TaskLedger.list(statuses=("running",)) → _resume_autonomous_record
  retention: CoordinatorService._prune_stale_scratch_dirs (default 14d, bounded,
    best-effort at run() start; current task always kept)

resumability:  # F3.1 + AM-CANCEL (2026-06-12)
  run(start_phase=...): phases before start_phase load artifacts from scratch
    instead of re-executing; detect_resume_phase(task_id) finds the first
    incomplete phase; TaskHandler._run_coordinated_task(resumed=True) wires it.
  implementation_gate: a resumed run that finds implementation.started without
    persisted results fails closed (implementation_rerun_blocked) — re-running
    the side-effect phase requires allow_implementation_rerun=True explicitly.
  should_abort: checked at every phase boundary (TaskHandler passes
    _is_cancelled); cancelled runs emit coordinator_cancelled and return
    error=cancelled_at_phase_boundary:<next_phase>.
  empty_synthesis: visible degradation (audit.synthesis_empty +
    coordinator_synthesis_empty event + Advertencia de Contexto downstream).
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
`router.ask(lane="judge", role="control_judge", timeout<=30)`, executes one of 19 action handlers per tick
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

### 5.9 CodeSkill governance

`SkillRegistry` is the enforcement point for generated executable skills.
Tool tier policy still applies at `SkillExecute`, but CodeSkills can also be
created by Kairos and the scheduled `skill_expand` runner, so governance is
centralized in `claw_v2/skills.py`.

Contract:

```yaml
generated_skill_status: pending_review
execute_allowed_status: active
sensitive_generation_targets: denied_before_router_call
invalid_skill_names: denied_before_file_write
events:
  allow: codeskill_governance_allowed
  deny: codeskill_governance_denied
```

Generated skills may be written and tested, but they are not executable until
explicitly activated outside the generation path. Denials fail closed and emit
audit events without persisting raw prompts, generated code, or secret-like
payloads.

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

  - change: Route control_judge/control_verifier/critical_verifier through Codex or timeout >30s
    why: Control-path provider calls must be bounded and must not block behind a heavy coding runtime.
    enforced_by: AppConfig.validate_provider_role_policy + explicit LLMRouter role call-sites

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
