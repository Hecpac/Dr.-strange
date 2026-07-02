# Claw v2 — Internal Wiring

> Architectural reference for Claw / Dr. Strange to consult during refactors,
> debugging, and self-improvement. Not part of boot context. Read on demand.

---

## meta

```yaml
describes_commit: "S-α autonomy-block slice 1: waiting_for_user_input failure notifications announce the pre-existing rescue path (reply-in-chat re-drive ~24h + /task_pending) via _WAITING_USER_INPUT_RECOVERY_HINT in task_handler._failure_response_text"
doc_version: 2.44
last_verified: 2026-07-01
verification_method: "code cross-read of _failure_response_text + _blocked_user_input_reason (task_handler.py) and the rescue chain (_recent_waiting_for_user_task / _telegram_continuation_shortcut, bot.py) against this doc + WaitingUserInputRecoveryHintTests (2, green inside 54-test task_handler file) + live deploy 965871a: clean restart (pid 68921, zero stderr delta), composer exercised on the daemon checkout with the production-verbatim KeepAlive error shape. Predecessor P0-2 branch-integrity (doc_version 2.43) remains in main"
anchor_strategy: symbol_only  # path:symbol, no line numbers
audience: claw_v2  # consumed by the agent itself
```

If `git rev-parse HEAD` diverges substantially from `describes_commit`,
assume parts of this doc may be stale. The invariants below are the most
stable section; the layer detail decays fastest.

F2 production state (2026-06-24): F2.0/F2.1 are merged; the four F2 tables
(`phase_checkpoints`, `phase_checkpoint_writes`, `external_effect_records`,
`phase_recovery_cursors`) physically exist in production `claw.db` but are empty
after purging a Stage 2C1 synthetic-record seed. `CLAW_F2_DURABILITY_ENABLED` is
unset, so the live daemon constructs no `F2DurabilityStore` and performs no F2
reads/writes. Older commit-keyed `operational_status` blocks below that read
"F2: design-only" are point-in-time snapshots, not current state.

## e4a3ee2 browser atomic tools live smoke status

```yaml
main_head: e4a3ee2
main_commit: e4a3ee2fd9399b8ff7633cde5be4aafe6ccfd2ca
live_daemon_field_verification:
  source: observe_stream agent_startup_context payload.code_version
  event_id: 270260
  code_version: e4a3ee2
  pid: 33828
browser_atomic_tools:
  source_pr: "#112"
  merged_to_main: true
  deployed_live: true
  live_code_version: e4a3ee2
  smoke_status: pass
  smoke_path: ToolRegistry.default(...).execute(...)
  smoke_session_id: smoke-browser-readonly
  smoke_scope:
    - BrowserNavigate to https://example.com
    - BrowserSnapshot on the same session
  smoke_not_executed:
    - BrowserClick
    - BrowserType
    - submit
    - BrowserScreenshot
    - private_or_authenticated_site
    - mutating_browser_action
  smoke_evidence:
    navigate_ok: true
    navigate_final_url: https://example.com/
    navigate_title: Example Domain
    snapshot_ok: true
    snapshot_contains: Example Domain
    snapshot_bounded: true
    observe_events:
      - browser_tool_action_started
      - browser_tool_action_completed
    sensitive_payload_hits: 0
    persisted_url_userinfo_query_fragment_hits: 0
    RuntimeDb_WAL_SQLite_database_locked_errors: 0
    browser_tools_errors: 0
    tool_policy_errors: 0
    watchdog_smoke_after_browser_smoke: PASS/read_only
  approval_model:
    read_only_tools: BrowserNavigate and BrowserSnapshot are Tier 1
    mutating_tools: BrowserClick and BrowserType remain Tier 3 approval-gated
    approval_bypass_observed: false
operational_status:
  browser_atomic_read_only_tools_live: true
  browser_atomic_read_only_smoke_passed: true
  private_authenticated_browser_state_inspected: false
  F2: design-only; not implemented
```

## 901fd72 audit status

```yaml
main_head: 901fd72
main_commit: 901fd72146fbf48590bc36513ae25c87b5c2606b
live_daemon_field_verification:
  source: operator-reported observe_stream agent_startup_context payload.code_version
  event_id: 266236
  code_version: 901fd72
  pid: 55176
  boot_time_utc: "2026-06-23 16:55:10"
  scope: code_version/boot evidence only; does not verify every production state surface
  post_132_live_verification: performed; F0.2d is live at 901fd72
merged_lanes:
  - "#125 / F1.4 watchdog stale-event filter"
  - "#126 autonomy recovery wave A"
  - "#127 O3 verification reconciliation lane"
  - "#128 C4 promote-gate artifact lift"
  - "#130 internal wiring docs"
  - "#131 read-only watchdog stale-filter smoke/runbook"
  - "#132 F0.2d llm_decision snapshot minimization"
f1_source_status:
  F1.1: complete; production runtime uses one RuntimeDb owner/lock for core stores
  F1.2_F1.3: complete; production RuntimeDb path no longer registers WAL-heal handles
  F1.4: complete/deployed through c42ae47; diagnostics classifies historical/stale observe errors as non-actionable
  C4: complete/deployed through #128; field-verified live at 901fd72
  F0_2d: fixed by #132; field-verified live at 901fd72
f1_live_status:
  RuntimeDb_single_writer: field-verified live at c42ae47
  watchdog_stale_event_filter: field-verified live at c42ae47
  included_live_lanes: ["#126 autonomy recovery wave A", "#127 O3 verification reconciliation lane", "#128 C4 promote-gate artifact lift", "#132 F0.2d llm_decision snapshot minimization"]
  watchdog_reload_reenable: complete; watchdog re-enabled safely after PASS smoke
  watchdog_reenable_evidence:
    command: launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.pachano.claw-watchdog.plist"
    rollback: launchctl bootout "gui/$(id -u)/com.pachano.claw-watchdog"
    status_command: launchctl print "gui/$(id -u)/com.pachano.claw-watchdog"
    status: loaded LaunchAgent; interval 300s; last exit code 0; idle between runs
    preflight_smoke: safe_candidate/PASS at expected_code_version 901fd72
    post_enable_smoke: safe_candidate/PASS at expected_code_version 901fd72
    observe_window_checked: events 266365-266434
    RuntimeDb_WAL_SQLite_database_locked_errors: 0
    stale_event_action_attempts: 0
    unexpected_historical_stale_resume_enqueue: 0
    rollback_needed: false
  next_recommended_check: 1h and 24h read-only observe soak; rerun watchdog smoke with expected_code_version 901fd72
operational_status:
  source_integrated_on_main: true
  live_daemon_code_version_field_verified: true
  watchdog_reenabled_field_verified: true
  watchdog_gate: complete; continue read-only 1h/24h soak monitoring
pending_remediation_notes:
  C4_promote_gate_bypass: fixed in main by #128 and field-verified live via agent_startup_context event 266236
  browser_tools_PR_112: merged, deployed, and read-only smoke-passed live at e4a3ee2
  PR_92: stale/draft/conflicting/obsolete; superseded by focused #128 C4 fix
  F0_2d: fixed in main by #132 and field-verified live at 901fd72
  F2: design exists in draft #133; design-only; not implemented
draft_prs:
  "#129": browser tools security patch against PR #112 branch; superseded by merged #112 stack
  "#133": F2 design; draft, design-only, not implemented
```

---

## 1. invariants

Non-negotiable. Any refactor that breaks one breaks operability even if
tests pass. Defend them.

```yaml
invariants:
  wal_generation_guard:
    rule: The production runtime DB path does not use the legacy WAL-heal
          registry. RuntimeDb is the sole long-lived production owner of the
          `claw.db` connection, and RuntimeDb-backed stores (observe, memory,
          task_ledger, jobs, orchestration, capability_grants) do not register
          StoreWalHealHandle callbacks or call the conservative heal helpers.
          The legacy `runtime_db=None` back-compat/test seams still register
          StoreWalHealHandle and retain the WAL generation guard behavior.
    why: 2026-06-12 incident — pytest run from the production repo root (by the
         runtime agent itself) unlinked data/claw.db-wal/-shm under the live
         daemon; every writer then failed "database is locked" forever and
         messages/events/task closes silently stopped persisting while the bot
         kept chatting. Two concurrent WAL generations writing the same DB risk
         corruption. F1.1 collapsed production to one RuntimeDb connection and
         F1.1b passed H24 cleanly, so F1.3 retires active production WAL-heal
         instead of preserving a registry-wide close/reopen cascade. Legacy
         tests keep the old guard available for non-production seams.
    enforced_by:
      - tests/test_sqlite_wal_heal.py
      - tests/test_runtimedb_wiring.py::BuildRuntimeIdentityTests::test_build_runtime_registers_no_wal_heal_handles_for_runtime_db_path
      - tests/test_runtimedb_wiring.py::RuntimeDbBackedStoresNoWalHealTests

  runtime_db_read_lock_discipline:
    rule: The five core stores wired in build_runtime (memory, observe, jobs,
          orchestration, task_ledger; capability_grants joins via the tool path)
          share ONE RuntimeDb — a single sqlite3 connection plus a single
          re-entrant lock (RuntimeDb.lock). Every SQL call on a store's shared
          connection (self._conn, the RuntimeDb connection handle) runs while
          that lock is held: lexically inside `with self._lock:` or a
          self._db.cursor()/transaction()/try_cursor()/try_acquire() block, or
          in an @_synchronized method. observe.emit keeps its non-blocking
          try_acquire fast-drop so a busy store never blocks the event loop;
          observe.maintenance_vacuum runs on a dedicated short-lived connection
          (the only sanctioned non-self._conn SQL). Schema/migration and
          under-caller-lock helpers are allowlisted in the tripwire by name.
    why: RAÍZ #1 — 7 long-lived connections + 7 locks against one claw.db
         produced a "database is locked" storm and a WAL-heal cascade that left
         the DB write-dead. Collapsing to one serialized connection (F1.1a)
         means SQLite never sees concurrent access; a bare self._conn SQL call
         outside the lock re-opens that race. Single-conn+lock is intra-process
         only — the watchdog stale-event filter (F1.4) covers multi-daemon
         overlap; the WAL-heal cascade is retired from the production RuntimeDb
         path in F1.3.
    enforced_by:
      - tests/test_architecture_invariants.py::RuntimeDbReadLockDisciplineTests::test_no_bare_conn_execute_outside_runtimedb_cursor
      - tests/test_architecture_invariants.py::RuntimeDbReadLockDisciplineTests::test_bare_conn_detector_has_teeth
      - tests/test_sqlite_runtime.py::RuntimeDbConcurrencyTests::test_20_threads_across_stores_zero_locked_errors_zero_heals

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
    inline_bounded_local_maintenance:
      - durable_retention_prune -> two bounded local SQLite DELETE paths
        (`JobService.prune_terminal`, `TaskLedger.prune_terminal`) plus env
        parsing only; no provider, subprocess, LLM, VACUUM, or unbounded scan.
        This is the same allowed class as observe_prune, not a slow autonomous
        scheduler job.
    enforced_by: tests/test_architecture_invariants.py::test_no_default_on_scheduler_job_runs_heavy_work_inline_in_daemon_tick
                 (deny-by-default sweep at production default; _PENDING_INLINE_MIGRATION is now empty and may only stay empty)
    why: CronScheduler.run_due() invokes handlers synchronously. Any provider
         call, code generation, verifier, subprocess, or research workload left
         inline would freeze the daemon tick and delay heartbeat / reconciliation
         observability. Core Invariant 1 is now CLOSED: every slow/provider/
         subprocess/codegen scheduler job enqueues a durable agent_job and
         executes in a ClawDaemon background runner off-tick. The backstop fails
         if any future job re-introduces inline heavy work.

  startup_recovery_is_seeded_from_running_agent_tasks_not_phase_checkpoints:
    rule: Startup recovery roots come from `agent_tasks` records that are
          running/resumable. Startup recovery must not globally enumerate
          `phase_checkpoints`.
    checkpoint_only_orphans: Checkpoint-only orphan rows, including old
          synthetic `stage2c1-*` rows with no `agent_tasks` record, are not
          recovery roots at startup.
    effective_startup_state: These rows are not classified at startup as
          `complete`, `retryable`, `manual_review_required`, or
          `verified_absent`; their effective startup state is
          `not_classified_not_reached`.
    f2_boundary: This is independent of `CLAW_F2_DURABILITY_ENABLED`; F2 ON
          only affects per-resumed-task planning after an `agent_tasks` record
          has seeded resume.
    no_side_effects: No replay or coordinator rerun is allowed solely because an
          orphan F2 checkpoint exists.
    enforced_by:
      - tests/test_task_handler.py::ResumeWiringTests::test_startup_recovery_is_seeded_from_running_agent_tasks_not_phase_checkpoints

  maintenance_mode_blocks_claims_scheduler_work_and_drain_applies:
    rule: With `CLAW_MAINTENANCE_MODE` truthy, the daemon may stay up but must
          not pick up work through the A2 chokepoints: JobService claims are
          blocked, scheduler work enqueue is blocked for `approval_sweep` and
          `pipeline_poll_merges`, and pending-verification drain apply is
          blocked.
    flags:
      CLAW_MAINTENANCE_MODE: Truthy values (`1`, `true`, `yes`, `on`) block
          JobService.claim(), JobService.claim_next(), scheduler enqueue work
          for `approval_sweep` / `pipeline_poll_merges`, and the mutating
          pending-verification drain apply path. Absence/default preserves
          current production behavior.
      CLAW_NO_JOB_CLAIM: Truthy values block only JobService.claim() and
          JobService.claim_next(). Absence/default preserves current production
          behavior.
    existing_maintenance_relationship: `CLAW_AUTONOMOUS_MAINTENANCE` /
          `CLAW_AUTONOMOUS_MAINTENANCE_ENABLED` still control autonomous
          maintenance jobs and keep their existing skip reason
          `autonomous_maintenance_disabled`. `CLAW_MAINTENANCE_MODE` is a
          broader no-work gate and is checked before the autonomous-maintenance
          and capability skip reasons on jobs that use the combined skip
          helper.
    drain_relationship: `CLAW_PENDING_VERIFICATION_DRAIN_APPLY` still defaults
          off and is still required before any drain apply is requested. When
          `CLAW_MAINTENANCE_MODE` is truthy, the runner reports
          `maintenance_mode_active` and does not call
          drain_reconcilable_unverified(apply=True) or
          reconcile_failed_unverified(apply=True), even if a queued payload asks
          for `drain_apply=true`.
    f2_boundary: This invariant is independent of F2 durability flags.
          `daemon up + maintenance ON + F2 OFF` is a valid positive control and
          emits `maintenance_mode_gate_assertion` with `claim=off`,
          `scheduler=off`, and `drain=off`.
    scheduler_chokepoint: Scheduler work must be gated before
          enqueue_scheduled_background_job(), not only by blocking JobService
          claims. Claim-only blocking would still allow scheduler ticks to
          create queued work and emit enqueue side effects.
    drain_chokepoint: Drain apply has its own gate even when claims are
          blocked because `_execute()` is the mutating boundary and can be
          called directly in tests or by future runner paths.
    enforced_by:
      - tests/test_jobs.py::JobServiceTests::test_claims_allowed_when_maintenance_flags_absent
      - tests/test_jobs.py::JobServiceTests::test_claims_blocked_by_maintenance_mode_before_running_transition
      - tests/test_jobs.py::JobServiceTests::test_claims_blocked_by_no_job_claim_before_running_transition
      - tests/test_approval_runtime_wiring.py::ApprovalRuntimeWiringTests::test_maintenance_mode_blocks_approval_and_pipeline_merge_enqueues_with_f2_off
      - tests/test_approval_runtime_wiring.py::ApprovalRuntimeWiringTests::test_pipeline_poll_merges_preserves_autonomous_maintenance_skip
      - tests/test_daemon.py::DaemonTickTests::test_maintenance_mode_blocks_drain_apply_even_when_payload_requests_apply

  maintenance_preflight_proves_no_work_pickup_before_canary:
    rule: Before Fase B / Stage 2C2 canary, operators must run the
          maintenance preflight in the intended runtime posture. The preflight
          reports explicit PASS/FAIL for claim, scheduler, and drain paths and
          fails closed when `CLAW_MAINTENANCE_MODE` is absent or a path cannot
          be verified.
    entrypoint: `python -m claw_v2.maintenance_preflight`
    proves:
      claim_path: With the supplied flags, JobService.claim() and
          JobService.claim_next() do not transition queued/retrying jobs to
          `running`. The proof uses isolated temp job state and the real claim
          methods.
      scheduler_path: With the supplied flags, `approval_sweep` and
          `pipeline_poll_merges` are blocked before
          enqueue_scheduled_background_job(). The proof uses isolated temp job
          state and the registered scheduler job kinds/resume keys.
      drain_path: With the supplied flags, observe/report-only reconciliation
          may run, but the mutating calls
          drain_reconcilable_unverified(apply=True) and
          reconcile_failed_unverified(apply=True) are blocked even when a
          payload asks for `drain_apply=true`.
    does_not_prove: The preflight does not start/restart the daemon, run a live
          scheduler loop, claim live jobs, apply live drains, or prove a
          launched process is using a specific environment. Live daemon
          confirmation remains a separate smoke after operator authorization.
    flags:
      CLAW_MAINTENANCE_MODE: Must be truthy for PASS. This is the required
          canary no-work posture.
      CLAW_NO_JOB_CLAIM: Reported separately. It can block claim path only, but
          cannot make scheduler or drain paths PASS without
          `CLAW_MAINTENANCE_MODE`.
      CLAW_F2_DURABILITY_ENABLED: Reported as `f2_enabled`; PASS/FAIL for the
          no-work paths is independent of F2 ON/OFF.
    read_only_safety: Tests and local smoke use temp DBs/fakes only. If a live
          DB path is supplied, the preflight opens it read-only/immutable for a
          liveness check and still proves work paths with temp/fake state.
          Operator procedure still requires the approved backup +
          `integrity_check` pattern before primary DB inspection.
    output_contract: Structured output includes `overall_status`, `claim_path`,
          `scheduler_path`, `drain_path`, `maintenance_mode_active`,
          `no_job_claim_active`, `f2_enabled`, `db_path_checked`, and
          path-level reasons/details. Any path FAIL makes
          `overall_status=FAIL`.
    enforced_by:
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_preflight_passes_with_maintenance_on_and_f2_off
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_preflight_passes_with_maintenance_on_and_f2_on
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_preflight_fails_when_maintenance_is_off
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_claim_path_fails_if_runtime_claim_gates_are_inactive
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_scheduler_path_fails_if_scheduled_work_would_enqueue
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_drain_path_fails_if_apply_would_run
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_output_is_structured_with_path_level_reasons
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_cli_smoke_outputs_json_pass_with_temp_state
      - tests/test_maintenance_preflight.py::MaintenancePreflightTests::test_supplied_db_path_is_opened_read_only_immutable

  stage2c2_synthetic_canary_uses_isolated_f2_state_only:
    rule: The Stage 2C2 synthetic F2 canary runs only against an isolated temp
          DB it creates, using synthetic `stage2c2-*` IDs. It must never be
          invented against the primary live `data/claw.db`: the live daemon is
          the single RuntimeDb writer, so a second writer would violate the
          single-writer invariant (WAL-corruption risk), and ad-hoc primary
          synthetic seeds are exactly the Stage 2C1 mistake that had to be
          purged (2026-06-24).
    entrypoint: `python -m claw_v2.stage2c2_synthetic_canary --temp-db --json`
    temp_db_default: A supplied `--db-path` is refused before any DB is opened
          (`primary_db_touched=false`); the harness writes only to its own temp
          DB. `--temp-db` and `--db-path` are mutually exclusive.
    synthetic_prefix: All seeded task/run/effect IDs use the `stage2c2-` prefix.
          The harness scans the four F2 tables and fails if any row lacks it
          (`non_synthetic_records_created`).
    proves: F2 store + recovery-planner LOGIC on isolated synthetic state —
          phase checkpoints (started→succeeded), contiguously ordered + linked
          checkpoint writes with payload hashes, external-effect idempotency
          (same idempotency_key returns the existing first row), and recovery
          classifications COMPLETE / RETRYABLE / BLOCKED /
          MANUAL_REVIEW_REQUIRED, plus verified_applied (no replay) and
          verified_absent (future execution required, no replay).
          `will_replay_external_effects` is always False.
    does_not_prove: It does NOT exercise the live daemon's F2 path against the
          primary DB. That remains UNBUILT and still requires injection through
          the daemon single-writer path or a quiesced daemon. A PASS here is
          not a signal that enabling F2 live is safe.
    relationship_to_gate_b: The Gate B live idle canary (maintenance ON + F2
          ON, Posturas 1/2, 2026-06-25) proved F2 ON is inert/idle-safe on the
          live daemon; this harness proves the F2 logic on synthetic state.
          Neither proves live F2 with real work. Stage 3 remains a separate
          gate.
    output_contract: Structured `--json` includes `overall_status`,
          `db_path_checked`, `temp_db_only`, `primary_db_touched`,
          `synthetic_prefix`, `phase_checkpoint_path`, `recovery_planner_path`,
          `external_effect_path`, `counts_before`, `counts_after`,
          `synthetic_ids`, `reasons`, and `does_not_prove`. Fails closed: any
          path FAIL, any non-synthetic write, a supplied non-temp DB path, or
          any exception makes `overall_status=FAIL`.
    enforced_by:
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_harness_passes_on_temp_db
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_refuses_supplied_db_path_and_leaves_it_untouched
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_only_stage2c2_ids_used
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_recovery_classifications
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_verified_absent_requires_future_execution_and_no_replay
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_verified_applied_does_not_replay
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_duplicate_idempotency_returns_existing_row
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_json_output_contains_required_fields
      - tests/test_stage2c2_synthetic_canary.py::Stage2C2SyntheticCanaryTests::test_no_real_work_paths_invoked

  primary_f2_compatibility_preflight_is_read_only:
    rule: The F2 primary compatibility preflight only ever READS a supplied DB.
          A supplied `--db-path` is opened `mode=ro` (URI `?mode=ro`) plus
          `PRAGMA query_only=ON`; it MUST NOT be opened `immutable=1` (the live
          daemon is the single RuntimeDb WAL writer, and `immutable` ignores the
          `-wal`, yielding a stale snapshot). It never constructs a writing
          `RuntimeDb`/`F2DurabilityStore` against the supplied path — those are
          built only on its own temp DBs (for the expected-schema derivation and
          the `--temp-db` smoke). `primary_db_touched` is always false.
    entrypoint: `python -m claw_v2.f2_primary_compat_preflight --db-path data/claw.db --json`
    replaces: The proposed primary seed/verify/purge synthetic canary
          (`primary_f2_write_path_incompatibility_canary`), rejected by the
          operator 2026-06-25 (mutating the primary buys little vs its cost).
    retires_failure_mode: `primary_f2_write_path_incompatibility` — the first
          real F2 write to the primary failing/corrupting/behaving differently
          due to schema drift, missing real indexes/constraints, or physical
          state. Answered read-only: do the F2 tables/columns/unique-indexes the
          code expects exist (subset semantics) and does `quick_check` pass?
    does_not_prove: NOT the live F2 write path, crash recovery, WAL concurrency,
          a real executor, the durable NotebookLM lane, external-effect dedup,
          or Stage 3. A `PRIMARY_COMPAT_PREFLIGHT_READY` result means only that
          the primary schema is compatible — it is NOT a signal that enabling F2
          live (Gate B / Stage 2C2) is safe. Each gate stays separate.
    output_contract: Structured `--json` includes `overall_status`,
          `recommendation` (PRIMARY_COMPAT_PREFLIGHT_READY / NEEDS_REPAIR /
          BLOCKED), `db_path_checked`, `opened_read_only`, `immutable_mode_used`
          (false), `primary_db_touched` (false), `schema_version_expected`,
          `schema_version_found`, `schema_path`, `index_path`, `counts_path`,
          `integrity_path`, `integrity_required` (true), `f2_table_counts`,
          `non_empty_f2_tables`, `reasons`, `checks`, and `does_not_prove`. Fails
          closed (`BLOCKED`) on read-only open failure or any exception.
    enforced_by:
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_read_only_enforcement_write_raises
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_open_failure_is_blocked
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_matching_primary_passes
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_missing_table_needs_repair
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_missing_unique_index_needs_repair
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_subset_extra_objects_still_passes
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_json_output_contains_required_fields
      - tests/test_f2_primary_compat_preflight.py::CliTests::test_cli_db_path_is_read_only

  external_effect_recovery_is_idempotent_and_never_auto_replays:
    rule: F2 recovery classifies external-effect evidence only; it never
          executes external effects directly and never sets
          `will_replay_external_effects=true`.
    idempotency_key: `external_effect_records.idempotency_key` is unique.
          A duplicate idempotency key must reuse the existing
          `external_effect_records` row; executor behavior must treat that as a
          no-op and must not call the external provider again.
    executor_ordering: Executors for real external effects must write durable
          intent (`intent_recorded` plus a linked checkpoint write) before any
          real-world effect is attempted. Ledger dedup only protects effects
          after that durable intent exists.
    verified_applied: `verified_applied` means the effect is already applied.
          Recovery may classify the phase as complete or retryable depending on
          checkpoint state, but the effect itself must be reused/no-op by
          idempotency key and never replayed.
    verified_absent: `verified_absent` means the effect was checked and is
          absent. Recovery records the effect as requiring future execution,
          keeps `will_replay_external_effects=false`, and TaskHandler blocks
          coordinator auto-rerun with
          `f2_recovery_retry_requires_future_external_effect`.
    manual_review: Unsafe statuses (`intent_recorded`, `apply_in_progress`,
          `applied`, `failed`, `verification_required`,
          `blocked_manual_review`) and orphan/unlinked external-effect rows
          require manual review. They may not be auto-replayed by recovery.
    crash_before_ledger: If a crash occurs after a real-world effect starts but
          before `external_effect_records` is written, F2 has no durable row to
          dedup or classify. Current recovery treats the phase from checkpoint
          evidence alone, usually as retryable when the latest checkpoint is
          `started`; this remains outside ledger dedup and must be controlled
          by executor ordering and the future Stage 3 design.
    enforced_by:
      - tests/test_f2_external_effect_synthetics.py::F2ExternalEffectSyntheticTests::test_same_idempotency_key_executes_fake_effect_once_and_reuses_record
      - tests/test_f2_external_effect_synthetics.py::F2ExternalEffectSyntheticTests::test_crash_before_ledger_write_is_undetectable_retryable_risk
      - tests/test_f2_external_effect_synthetics.py::F2ExternalEffectSyntheticTests::test_effect_then_crash_before_checkpoint_does_not_reexecute_verified_applied
      - tests/test_f2_external_effect_synthetics.py::F2ExternalEffectSyntheticTests::test_orphaned_verified_applied_effect_requires_manual_review
      - tests/test_f2_external_effect_synthetics.py::F2ExternalEffectSyntheticTests::test_verified_absent_future_effect_blocks_taskhandler_auto_rerun

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

  brain_tooluse_verify_timeout_is_real_or_explicitly_unsupported:
    rule: `BRAIN_TOOLUSE_VERIFY` (the active inline verifier) has CODE default
          OFF (`config.py` `_env_bool(..., False)`) but may be RUNTIME ON via
          `~/.claw/env` — do NOT read "code default OFF" as "off in prod".
          `BRAIN_TOOLUSE_VERIFY_TIMEOUT_SECONDS` is a REAL, leak-free bound (not
          a wall-clock around a leaked thread): parsed into
          `AppConfig.brain_tooluse_verify_timeout_seconds`
          (`_brain_tooluse_verify_timeout_from_env`) and threaded by
          `verify_brain_tooluse` as the verifier `WorkerTask.timeout_seconds`,
          which `_execute_worker` passes as the per-dispatch provider timeout
          (`router.ask(timeout=...)`). The bound is the provider call itself, so
          a timeout raises inside the worker (no runaway) → `WorkerResult` with
          an error and no content → `pending` → the mutation-aware blocker. A
          timeout NEVER yields `passed`/`succeeded`.
    semantics: Absent → `None`: the verifier lane keeps its role-default
          timeout (`coordinator_verification` ≈ 60s) — this env OVERRIDES the
          existing ~60s bound, it does not add a bound where none existed.
          Positive number → that value (e.g. 30s tightens 60→30 and may
          marginally raise pending/blocked — fail-closed-safe). Invalid /
          non-positive → `None` + a startup warning (the operator keeps the
          bounded role default instead of being silently unbounded).
    no_clobber: `verify_brain_tooluse` runs with
          `lane_overrides=_lane_model_overrides(session_id)`, and
          `_execute_worker` lets an override `timeout` key win over
          `WorkerTask.timeout_seconds`. `ModelOverride.to_dict()` (the override
          source) emits only provider/model/billing/effort/source/key — NO
          `timeout` key — so the verifier task timeout is never clobbered; a
          regression test fails if a `timeout` field is ever added to
          `ModelOverride`.
    record: A timeout (or any dispatch error with no verdict) is logged with a
          GENERIC marker only — the raw `WorkerResult.error` is never echoed
          (it may carry secrets).
    not_f4_forced_action: Verifier ON is the honest-COMPLETION gate (verify/
          block a turn that already ran), NOT F4 forced-action. Forced action
          (synchronous post-model gate + re-prompt when the brain promises
          without acting + deterministic-router reactivation) is UNBUILT and is
          a SEPARATE track (F4-B). This invariant is timeout/config hygiene only.
    enforced_by:
      - tests/test_config.py::AppConfigDefaultsTests::test_brain_tooluse_verify_timeout_parsing
      - tests/test_brain_tooluse_verify.py::test_verify_task_carries_timeout_when_set
      - tests/test_brain_tooluse_verify.py::test_verify_task_timeout_defaults_none_keeps_role_default
      - tests/test_brain_tooluse_verify.py::test_verify_timeout_error_returns_pending_without_echoing_raw_error
      - tests/test_brain_tooluse_verify.py::test_model_override_to_dict_has_no_timeout_key_so_verifier_timeout_not_clobbered
    why: The operator set a 30s verify timeout that no code consumed (a no-op).
         F4-A makes it real (provider-call bound, fail-closed); silently
         ignoring it misled the operator about how long the inline verifier can
         block a brain turn.

  high_confidence_delegation_intents_do_not_depend_on_model_tool_choice:
    rule: A narrow, unambiguous "review my authenticated X / Twitter feed" intent
          is routed deterministically to a durable, crash-recoverable delivery
          state machine seeded in `_maybe_handle_f4_deterministic_delegation`
          (`bot.py`) — it does NOT depend on the brain choosing to call
          `mcp__claw__delegate_task`. Fixes
          the 2026-06-25 failure where the brain emitted zero tool calls,
          enqueued nothing, and confabulated a `ToolSearch`/tool_policy rejection
          that never happened (`ToolSearch` does not exist in claw_v2). F4-B1
          only; broader forced-action + post-model anti-confabulation = F4-B2.
    flag: `CLAW_F4_DETERMINISTIC_DELEGATION` (config `f4_deterministic_delegation`),
          default OFF. OFF = exact prior behavior (gate returns None first; the
          off-tick runner + stale-recovery allowlist run but no-op with no
          `f4b.delegation` jobs). Does NOT touch `CLAW_DISABLE_TASK_INTENT_ROUTER`.
          ONE deliberate, flag-INDEPENDENT carve-out: the Telegram transport
          always attaches `context_metadata["inbound"]` (message_id/update_id —
          Telegram's own ids, not secrets), which is persisted into
          `session_state.last_channel_route`/`task_ledger.route`/observe payloads
          even when OFF. No consumer branches on it while OFF (functionally
          inert); it is the gate's delivery identity when ON. Gate the attach on
          the flag if strict storage parity is required.
    placement: Runs in `_handle_text_body` BEFORE `_maybe_handle_task_intent` /
          `_maybe_handle_capability_route` and captures on match, so if the broad
          task-intent router is ever re-enabled the request is still handled
          exactly once (no double routing/enqueue).
    classifier: `classify_authenticated_browse_intent` (`delegation_intents.py`)
          is a conservative pure function (review-verb AND explicit X/feed target,
          minus authoring/definitional/opinion/placeholder markers). Prefers
          false negatives; matches "Haz un repaso por X"; rejects "¿Qué es X?" /
          "Escribe un post para X" / "Qué opinas de Twitter" / "Resume este
          texto…" and X-as-placeholder ("punto X", "por X razón", or X behind an
          object noun — "código/repo/PR de X" — since `_X_PLATFORM` only counts X
          when bound to a review verb/noun or a feed word, not an arbitrary noun).
    architecture: A two-stage durable pipeline. (1) The GATE only enqueues a
          durable `f4b.delegation` delivery job — it does NOT call
          `start_autonomous_task`, start a thread, run the coordinator, or delete.
          (2) `F4DelegationJobRunner` (`f4_delegation.py`), registered off-tick in
          the daemon (`_run_f4_delegation_runner_loop`, `daemon.py`;
          `daemon.task_handler` wired in `main.py`), claims that job and runs the
          idempotent bootstrap. Execution is then ledger-driven, not job-claimed
          (see start_latency). This supersedes the earlier inert
          `f4b.delegation_reservation` + `JobService.delete` design: the delivery
          job IS the recoverable state machine.
    delivery_identity: `delivery_key = f"f4b-delegation:{session_id}:{message_id}"`
          and a deterministic `task_id = f4b_delivery_task_id(delivery_key)`
          (`f4_delegation.py` → `f4bdeliv:{sha1(delivery_key)[:16]}`, stable
          forever, so a redelivery / reclaim converges on ONE logical task).
          Delivery id plumbed via `context_metadata["inbound"]`; the prod chain is
          `TelegramTransport → AgentRuntime.handle_text →
          BotService.handle_text(context_metadata) → gate` (AgentRuntime forwards
          inbound; stripping it was the P1 regression). No delivery id → fall
          through (skipped_no_delivery_id).
    gate_dedup: Two-window, existence-keyed, BEFORE any second side effect.
          WINDOW 1 — check the `task_id`'s `agent_tasks` ledger row FIRST
          (`task_ledger.get(task_id)`): if it EXISTS the bootstrap already
          materialised this delivery → status-aware dedup ack, never a second job.
          This survives the ACTIVE-ONLY `idx_agent_jobs_active_resume_key` index
          AFTER the delivery job terminalizes, and is keyed on row EXISTENCE — so
          coordinator_unavailable / failed bootstraps (which write NO ledger row)
          correctly fall through and re-attempt. WINDOW 2 — else
          `job_service.reserve(resume_key=delivery_key, kind="f4b.delegation",
          payload={task_id, session_id, message_id, objective, mode, task_kind,
          source_text, delegation_metadata})` returns `(record, created)`: the DB
          unique index elects exactly one creator under concurrent duplicate
          delivery (cross-process). `created=True` → truthful accepted/queued ack;
          `created=False` (duplicate while the job is still active, no ledger row
          yet) → status-aware dedup ack. The gate NEVER calls
          `start_autonomous_task` and NEVER deletes.
    runner: `F4DelegationJobRunner` claims `kind="f4b.delegation"` ONLY
          (`JobService.claim_next(kinds=("f4b.delegation",))`); no generic /
          unfiltered consumer claims it (AST-proven — see enforced_by). It is
          maintenance-aware (claim_next returns None while `job_claim_block_reason`
          is set → the job stays queued; P0-2 adds a SIBLING in-process latch —
          `JobService.set_safe_mode_reason(...)`, set by the daemon's
          branch-integrity check when the live checkout is stranded on a wrong
          branch — that blocks every claim path the same way; branch-integrity
          safe mode gates job claiming, but cron/`scheduler.run_due` is not
          branch-gated) and `should_stop`-wired
          (`shutdown.is_set`) for graceful shutdown. Per claimed job it calls
          `TaskHandler.ensure_autonomous_task_enqueued(...)`, checkpoints
          `{task_id, coordinator_job_id}`, then completes the delivery job.
    bootstrap: `ensure_autonomous_task_enqueued` (`task_handler.py`, ADDITIVE —
          `start_autonomous_task` is unchanged) is idempotent on the deterministic
          `task_id`: ONE `agent_tasks` row via `_record_ledger_task_started` →
          `TaskLedger.create` (`ON CONFLICT(task_id) DO UPDATE`) guarded by
          `if not existed_task` (a retry never clobbers coordinator progress or
          resurrects a terminal task), and ONE `coordinator.autonomous_task` job
          via `reserve(resume_key="coordinator:{task_id}")` with a TERMINAL-TASK
          guard (skip the reserve when the existing task is already terminal → no
          spurious coordinator job). Returns a structured
          `AutonomousTaskBootstrapResult`.
    start_latency: Execution is LEDGER-DRIVEN, not job-claimed. Nothing
          claim-executes the `coordinator.autonomous_task` job — it is a
          tracking / lease handle; `_reconcile_orphaned_jobs` cancels it for a
          terminal task. The orphan-job scan is rate-limited by
          `ClawDaemon.orphan_job_reconciliation_interval` (default 300s), so
          `daemon.tick()` does not repeat the N+1 lookup path every control-loop
          iteration. `resume_interrupted_autonomous_tasks` (startup + the 300s
          `task_lifecycle_watchdog`) resumes the `running` ledger row, so the
          start latency is ≤300s by design.
    crash_recovery: Crash-recoverable at every transition (verified by a 25×
          looped crash matrix). The JobService claim lease + `recover_stale_running`
          (`f4b.delegation` ∈ `AUTONOMY_STALE_RUNNING_JOB_KINDS`, `main.py`) + the
          runner's own `reclaim_stale_running` re-queue a job whose worker
          disappeared; the idempotent bootstrap guarantees no second task/job on
          retry. Each window converges to ONE delivery job, ONE `agent_tasks` row,
          ONE coordinator job, terminal delegation:
            - crash BEFORE delivery-job commit          → redelivery enqueues one job
            - crash AFTER commit, BEFORE claim          → runner bootstraps once
            - crash AFTER claim, BEFORE bootstrap       → reclaim → bootstraps once
            - crash AFTER bootstrap, BEFORE checkpoint  → idempotent retry, no dup
            - crash AFTER completion                    → terminal task, no new work
    no_delete: Failures TERMINALIZE, never delete (the audit row is preserved). A
          raised error from the bootstrap OR the checkpoint/complete linkage →
          `fail(retry=True)` (→ retrying, then `failed` after max_attempts); a
          structured `coordinator_unavailable` / `failed` result → `fail(reason)`.
          No delivery-path code deletes the durable job (quarantine / terminalize
          only) — the row always survives for the audit trail.
    exactly_once: ONE logical task (one `agent_tasks` row + one
          `coordinator.autonomous_task` job) per delivery identity, crash-
          recoverable. This is NOT exactly-once browser / external-effect
          execution — that lives in the F5 / execution track.
    truthful: Acks are status-aware and never fabricated. A fresh creator gets
          accepted/queued; a duplicate reflects the REAL linked-task state (queued
          when no linked task yet, running when it is running, processed when it is
          terminal). A reserve failure emits `f4_deterministic_delegation_failed`
          (reason code only, never raw error/secrets) and a concise truthful
          failure message — no fabricated tool/policy/loader detail, no
          retry/future-execution promise, no "send the same command again".
    observe: f4_deterministic_delegation_matched (deduped) / _enqueued / _failed /
          _skipped_no_delivery_id, plus runner events f4_delegation_runner_started
          / _completed / _failed / f4_delegation_stale_running_recovered —
          best-effort, safe ids/reason codes only.
    why_not_reprompt: A re-prompt re-enters the same model that just confabulated
          and can be talked around; deterministic routing removes the enqueue
          from model discretion for this narrow case. Broader forced-action /
          post-model anti-confabulation stays F4-B2.
    enforced_by:
      # gate: classifier, reserve dedup token, ledger-first + reserve windows, acks
      - tests/test_f4b_deterministic_delegation.py::ClassifierTests
      - tests/test_f4b_deterministic_delegation.py::JobServiceReserveTests
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_match_enqueues_one_delivery_job_with_accepted_ack
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_flag_off_falls_through
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_gate_independent_of_broad_router_flag
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_duplicate_delivery_one_job_queued_dedup_ack
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_legitimate_repeat_new_job
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_dedup_ack_running_when_linked_task_running
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_dedup_ack_processed_when_linked_task_terminal
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_dedup_ack_queued_when_no_linked_task
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_redelivery_after_terminalized_delivery_job_dedups_no_new_job
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_redelivery_without_ledger_row_falls_through_to_accepted
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_no_delivery_id_falls_through
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_reserve_failure_returns_truthful_message
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_concurrent_duplicate_elects_one_creator
      - tests/test_f4b_deterministic_delegation.py::GateTests::test_observe_none_does_not_crash
      - tests/test_f4b_deterministic_delegation.py::RealChainIntegrationTests::test_real_bot_handle_text_enqueues_durable_delivery_job
      - tests/test_f4b_deterministic_delegation.py::RealChainIntegrationTests::test_agent_runtime_path_forwards_inbound_id_to_gate
      # deterministic task_id + idempotent bootstrap + terminal-task guard
      - tests/test_f4_delegation.py::DeliveryTaskIdTests::test_deterministic_and_stable
      - tests/test_f4_delegation.py::BootstrapIdempotencyTests::test_bootstrap_is_idempotent_on_deterministic_task_id
      - tests/test_f4_delegation.py::BootstrapIdempotencyTests::test_bootstrap_on_terminal_task_mints_no_new_coordinator_job
      - tests/test_f4_delegation.py::BootstrapIdempotencyTests::test_terminal_task_not_resumed_no_reexecution
      # runner: bootstrap+complete, terminalize-not-delete, maintenance, should_stop
      - tests/test_f4_delegation.py::F4DelegationRunnerTests::test_runner_bootstraps_one_task_and_completes_delivery_job
      - tests/test_f4_delegation.py::F4DelegationRunnerTests::test_runner_bootstrap_failure_terminalizes_not_deletes
      - tests/test_f4_delegation.py::F4DelegationRunnerTests::test_runner_maintenance_leaves_job_queued
      - tests/test_f4_delegation.py::F4DelegationRunnerTests::test_runner_honors_should_stop
      # crash matrix (each window → one task, one coordinator job, terminal delegation)
      - tests/test_f4_delegation.py::F4DelegationCrashBoundaryTests::test_window1_crash_before_delivery_commit_redelivery_enqueues_one
      - tests/test_f4_delegation.py::F4DelegationCrashBoundaryTests::test_window2_crash_after_commit_before_claim_runner_bootstraps_once
      - tests/test_f4_delegation.py::F4DelegationCrashBoundaryTests::test_window3_crash_after_claim_before_bootstrap_reclaim_bootstraps_once
      - tests/test_f4_delegation.py::F4DelegationCrashBoundaryTests::test_window4_crash_after_bootstrap_before_complete_idempotent
      - tests/test_f4_delegation.py::F4DelegationCrashBoundaryTests::test_window5_crash_after_delivery_completion_terminal_task_no_new_work
      # daemon registration + stale-recovery allowlist + runner kind exclusivity (AST)
      - tests/test_daemon.py::DaemonF4DelegationRunnerWiringTests::test_run_loop_constructs_single_f4_runner_with_should_stop
      - tests/test_daemon.py::AutonomyStaleRunningAllowlistTests::test_f4b_delegation_in_stale_running_allowlist
      - tests/test_daemon.py::F4DelegationClaimExclusivityTests::test_claim_next_calls_are_filtered_and_f4b_kind_is_exclusive
      - tests/test_daemon.py::F4DelegationClaimExclusivityTests::test_main_does_not_wire_a_generic_consumer_for_f4b_kind

  waiting_user_input_failure_announces_recovery:
    rule: A terminal task-failure notification whose error carries the
          waiting_for_user_input class MUST append
          _WAITING_USER_INPUT_RECOVERY_HINT (claw_v2/task_handler.py,
          _failure_response_text), announcing the pre-existing rescue path —
          reply-in-chat re-drives the task (continuation shortcut,
          _recent_waiting_for_user_task, ~24h window) and /task_pending shows
          the blocker detail. The hint fires ONLY for that error class; every
          other failure text stays hint-free.
    enforced_by:
      - tests/test_task_handler.py::WaitingUserInputRecoveryHintTests::test_waiting_user_input_failure_announces_recovery_path
      - tests/test_task_handler.py::WaitingUserInputRecoveryHintTests::test_failure_text_without_user_input_block_has_no_recovery_hint
    why: The rescue mechanism predates the hint but was never announced, so the
         notification was a dead end — the user received worker-internal
         blockers with no visible way to respond (recon jul-2026, caso
         KeepAlive tg-574707975). Slice S-α of the autonomy remediation block
         (α announce / β bounded re-drive / γ evidence phase / δ structured
         verdict); regressing it reopens the dead end silently.
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
   │   + mode ∈ {coding, research, browse, ops}; browse/ops admitted 2026-06-14
   │   so the deterministic visible-Chrome flow runs pre-brain. Guarded by the
   │   matcher, not the gate — see §5.4. publish never admitted.)
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
re-export; T12, 2026-06-12, hardened in review #100): the PreToolUse hook also
denies — `brain` lane only — Bash that launches detached or backgrounded
processes. It is **background-based, not marker-based**: `nohup`/`setsid`/
`disown`, OR any real `&` backgrounding (`_BACKGROUND_TAIL_RE`), so even a bare
`python long_job.py &` is denied. The regex excludes the logical-AND `&&`, the
`&>`/`2>&1` redirections and a `&` glued inside a URL query string
(`?a=1&b=2`); a `&` inside a quoted string with spaces is a tolerated rare
false positive. Motive: during the T10 lock storm the brain improvised ghost
background processes with no ledger/monitor/notification and the work died
silently. The deny nudges to `delegate_task`; worker lanes are not gated (the
coordinator runs long processes under its own monitoring).

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
Each *records* its decision into a turn-scoped accumulator
(`dispatch_decision_accumulator`, `claw_v2/turn_context.py`);
`_handle_text_body` then emits a SINGLE consolidated `dispatch_decision`
event per turn (F0.3c — `_flush_dispatch_decision`, idempotent), instead
of ~15 rows/turn. Order matters; no test enforces it. The real call sites
in `_handle_text_body` (verified 2026-06-10):

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
| 8b | `_maybe_handle_f4_deterministic_delegation` | **F4-B1**, gated OFF by `CLAW_F4_DETERMINISTIC_DELEGATION` (default); narrow authenticated-X-feed-review intent → enqueues ONE durable `f4b.delegation` delivery job (ledger-row-first dedup on the deterministic `task_id`, else `JobService.reserve(resume_key=delivery_key)`); does NOT call `start_autonomous_task`/start a thread/delete — `F4DelegationJobRunner` claims the job off-tick and runs the idempotent bootstrap. Captures BEFORE the broad router (exactly-once on telegram message_id). See invariant `high_confidence_delegation_intents_do_not_depend_on_model_tool_choice` |
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

**dispatch_decision payload** (F0.3c consolidated, one event/turn):
`tried_handlers[]` (every handler considered — each entry: `handler`,
`route`, `reason`, `captured`, `matched_pattern`; bounded, no
prompt/system/evidence blobs), `selected_handler`/`selected_route` (the
winner, else None/`fall_through`), plus back-compat TOP-LEVEL fields so
existing parsers keep working: `handler`/`route` (mirror selected),
`reason` (winner's or `fall_through_all_<n>`), `captured` (any captured),
`matched_pattern`, `text_preview[:80]`, `text_len`, `text_length`,
`session_id`. `route` values: intercepted | fall_through | brain_shortcut
| explicit_command. `brain_shortcut` means the dispatcher only enriched
the prompt and the brain handled the turn (`captured=false`). Entry points
without a turn accumulator (`handle_multimodal`) still emit the legacy
single-handler shape via `_emit_single_dispatch_decision`.

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
    # cwd=workspace_root. ops/publish/browse added 2026-06-10. UPDATE 2026-06-14:
    # the pre-brain coordinator gate (maybe_run_coordinated_task / autonomy matrix
    # automatic_coordinator_modes) now ALSO admits {browse, ops} so the
    # deterministic visible-Chrome / Instagram flow runs pre-brain without a brain
    # round-trip. Safety rests on the matcher, not the gate: _looks_like_social_browser_request
    # requires an explicit navigation VERB + platform (bare nouns feed/timeline/
    # perfil/profile removed), so ambiguous/conversational turns still fall through
    # to the brain per the Routing Contract. publish stays blocked in every
    # autonomy mode. The executor-only contract holds: browser/CDP runs through the
    # in-process executor (a PreToolUse backstop still denies brain-lane Bash that
    # drives Chrome/CDP), never a brain-lane shell.
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
