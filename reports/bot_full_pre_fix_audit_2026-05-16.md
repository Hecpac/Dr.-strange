# Dr. Strange / Claw — Full Pre-Fix Audit (2026-05-16)

**Auditor:** Dr. Strange (read-only)
**Branch:** `main` at `b363063`
**Working dir:** `/Users/hector/Projects/Dr.-strange`
**Scope:** end-to-end orchestration reliability audit before Wave 0 implementation. Read-only. No app code, config, secrets, or production restart touched.

---

## A. Executive summary

### What is broken today (evidence-backed)

1. **Cost-per-hour circuit breaker tripping constantly.** 450 trips in the last 30 days; latest at $20.85/h vs $10/h threshold (`brain` actor). Each trip cascades into `tool_blocked_by_freeze` events (136 in 30d) that block even Tier-1 read-only tools (Grep, Glob, WikiSearch). The bot loses tool access during freezes — this is the dominant operational pain.
2. **Owner-delegation phrases mostly do not route.** Only `"hazlo tu"` matches handler #5 (`_maybe_handle_actionable_task_request`), and only when (a) runtime is Telegram and (b) an actionable objective already exists in session state. `"córrelo tú"`, `"decide tú"`, `"te toca a ti"`, `"encárgate tú"`, all English variants — fall through to brain. (Confirmed: see §D phrase matrix.)
3. **Meta / introspection questions are reaching coordinator.** Three documented cases in `observe_stream.autonomous_task_failed` (2026-05-08 → 2026-05-14): _"Mi pregunta es porque no completas las tareas faciles…"_, _"Que queremos comunicar en el email?"_, _"Arreglalo ahora"_, plus a secret-shaped token used as an objective. The capability/classifier upstream of `start_autonomous_task` is too promiscuous.
4. **All 35 persisted sessions are `autonomy_mode=assisted`.** Coordinator handler #15 requires `autonomous` + `mode in {coding, research}`. The autonomy path in the design exists but in current session population it is effectively unreachable except when forced through some other code path.
5. **5 "lost" tasks** with reason `"runtime lost authoritative backing state"` in the last 30 days — the task reaper (`daemon.py:76 mark_stale_running_lost()`) marked them dead after 300 s without progress.
6. **Codex worker timing out at 300 s** on the coding lane (4 documented `failed` rows in 30d; `llm_circuit_open` opened on `worker/codex` 2026-05-08). The 5-minute hard cap for coordinator-style coding work is too short for the workloads being attempted.
7. **`session_state.pending_action` and `task_queue_json` are never populated in DB** (0 / 35 rows). Proceed-token state lives only in in-memory cache, so a daemon restart loses it. This conflicts with the assumption in the resolver chain at `state_handler.py:209-271`.
8. **Brain fallback (`_brain_text_response`) can mark `verification_status="passed"`** at `bot.py:899` and `bot.py:956` without artifact validation. Only the deeper `task_ledger.validate_completion` gate catches missing-evidence (it did fire once, reconciling 6 false-successes on 2026-05-02). At the response layer the gate is absent.
9. **No runtime sanitizer for manual-handoff phrases.** `claw_v2/brain.py:115-138` declares forbidden patterns ("Pega el output", "ejecuta este comando", "no puedo por sandbox") in prose only. No regex sweep is applied to outgoing messages. No test asserts the bot doesn't emit them.
10. **`INTERNAL_WIRING.md` is stale.** `last_verified: 2026-05-10`, `describes_commit: 1b6e37c+tool-policies-json` — current HEAD `b363063` is 6 days and multiple PRs (incl. #32, #33, #35 Petri wiring) past that.

### What is *not* broken (validated)

- Daemon is alive and stable: PID 61723, launchd `com.pachano.claw` state `running`, web transport listening on `127.0.0.1:8765`, 19186 heartbeats cumulative, last tick 14:57.
- All 240 targeted tests pass (`tests/test_dispatch_routing.py`, `test_bot.py`, `test_task_handler.py`, `test_task_ledger.py`, `test_capability_router.py`, `test_state_handler.py`, `test_observation_window.py`, `test_llm.py`) — 4 expected xfails, 11 subtests passed, 32 s wall.
- Provider/lane mismatch protection is **enforced** at `claw_v2/llm.py:125` and `:135` — a non-tool-capable provider on a tool lane raises `ValueError`, no silent fallback.
- Kairos external-mutation handlers (`_handle_auto_publish_social`, `_handle_auto_deploy`) are env-gated (`KAIROS_AUTO_PUBLISH_SOCIAL=0`, `KAIROS_AUTO_DEPLOY=0` defaults) and create pending approvals when flags are off.
- Defense-in-depth false-success detector works: `task_completion.validate_completion` reclassified 6 skill tasks as `missing_evidence` on 2026-05-02.
- Tier policy is well defined (`TIER_READ_ONLY=1`, `TIER_LOCAL_MUTATION=2`, `TIER_REQUIRES_APPROVAL=3`; `tier_autoexec_max=2` default) and tools at Tier 3 go through `ApprovalGate`.
- Observation window is currently **not frozen** (`data/observation_window.json` → `frozen=false`, `actor=auto_clear_stale`, last update 2026-05-16 05:26 UTC).
- `_looks_like_pending_tasks_diagnostic` — **does not exist and is not referenced**. Prior audit's missing-symbol hypothesis is **not** confirmed at HEAD `b363063`. Pending tasks are served by `_maybe_handle_pending_tasks_query` (`bot.py:3696`).

### What is unknown

- Whether brain `router.ask(lane="brain")` paths can call tools without creating an `agent_tasks` row in *all* configurations. The static trace is plausible but a live capture under load would be required to confirm "tool-use-without-ledger" frequency.
- Real per-week incidence of meta/introspection misroute beyond the three captured `autonomous_task_failed` rows — needs a broader sweep of `observe_stream.dispatch_decision` payloads, which is out of scope for this read-only pass.
- Whether the 25 cancelled tasks in the last 30 days are user-cancelled, supersession-cancelled, or auto-cancelled by reaper. (`error` column shows "superseded_by_session_cont…" in some, suggesting session-continuation supersession logic.)

### Should we implement Wave 0 immediately or audit more?

**Audit is sufficient to start Wave 0**, but the **cost-per-hour breaker (Finding #1) is operationally urgent and should be addressed in parallel with Wave 0 routing fixes**, because every breaker trip is degrading the very same autonomy that Wave 0 is meant to harden. The 5-minute Codex hard cap (Finding #6) and missing pending_action persistence (Finding #7) are also prerequisites — Wave 0 routing changes will land in an environment where their resolution targets vanish on restart.

---

## B. Current repo/runtime snapshot

| Item | State |
|---|---|
| Branch | `main` |
| HEAD | `b363063` (`docs(strategies): ai-lead-gen Sierra methodology applied — business model`) |
| Working tree | clean; only untracked: `projects/job-search-linkedin/` |
| Staged | none |
| Conflicts | none (`git diff --check` clean) |
| Stash entries | 15 (`stash@{0..14}`) — multiple `claw:autostash:tg-574707975:…` autostashes from background bot operations |
| Worktrees | 3: `Dr.-strange/` (main), `.worktrees/claw-p0` (feat/claw-p0-telemetry), `/Users/hector/.claw/agents/_worktrees/perf-optimizer/exp-2` (detached, prunable) |
| Daemon | running, PID 61723, launchd `com.pachano.claw` (LaunchAgent, runs=6, last exit "inefficient" — i.e., resource throttled, not crash) |
| Web transport | LISTEN `127.0.0.1:8765` — verified via lsof |
| Telegram transport | active (PID matches `~/.claw/telegram.pid` = `claw.pid` = 61723) |
| Started_at | `1777043209` (Unix → 2026-04-21 ~16:46 UTC; long-running daemon) |
| restart_requested.json | stale: `2026-04-24` request from user — never cleared |
| Observation window | not frozen (`actor=auto_clear_stale`, updated 2026-05-16 05:26 UTC) |
| Tests | 240 passed, 4 xfailed, 11 subtests, 32.02 s |

**Stash safety note:** none of the 15 stashes are needed for this audit and they belong to autonomous background work. Do not pop them without explicit owner instruction. They could contaminate any in-place edit if popped accidentally.

**INTERNAL_WIRING drift:** `last_verified: 2026-05-10`, `describes_commit: 1b6e37c+tool-policies-json`. HEAD has moved through PRs #32, #33, #35 since then. Wave 0 cannot rely on the document being authoritative for the current 15-handler dispatch order; it must be re-verified against `bot.py:handle_text` directly.

---

## C. Architecture map

### Inbound routes (15-handler dispatch + brain fallthrough)

```
Telegram | web/mac | daemon/scheduler
              │
              ▼
       bot.py:handle_text
              │
   ┌──────────┴──────────┐
   │ pre-flight: session  │
   │ derive, state load,  │
   │ autonomy_mode read   │
   └──────────┬──────────┘
              │
              ▼
  1. pending_computer_approval     (bot.py:1652)
  2. operational_alert
  3. boot_context_status
  4. pending_tasks_query           (bot.py:1694 → 3696)
  5. actionable_task_request       (bot.py:1711 → 3775)
       flag: CLAW_DISABLE_TELEGRAM_ACTIONABLE_TASK_ROUTER (default 0 = ON)
  6. task_intent                   (bot.py:1732)
       flag: CLAW_DISABLE_TASK_INTENT_ROUTER (default 1 = DISABLED)
       ⚠ HANDLER DEAD IN DEFAULT CONFIG
  7. operational_status
  8. change_status_question
  9. capability_route              (bot.py:1795 → capability_router.py:119-161)
 10. pending_tool_approval_grant
 11. autonomy_grant_response
 12. stateful_followup             (bot.py:1824 → state_handler.py:141-314)
 13. shortcut (URL / browse)
 14. NLM.natural_language          (bot.py:1876)
       flag: CLAW_DISABLE_NLM_NATURAL_LANGUAGE (default 0 = ON)
 15. maybe_run_coordinated_task    (bot.py:1895 → task_handler.py:141)
       gate: autonomy_mode=='autonomous' AND mode in {coding,research}
              │
              ▼
 16. _brain_text_response          (bot.py:2813) — fallback
              │
              ▼
      brain.handle_message → router.ask(lane='brain')
              │
              ▼
      tool execution (potentially without agent_tasks row)
              │
              ▼
      sanitize → response
```

### Task / job ledger paths

- **Strong path (durable):** `bot.handle_text` → handler #15 `maybe_run_coordinated_task` → `task_handler.start_autonomous_task` → `TaskLedger.create(status="queued")` → coordinator phases → `mark_terminal(verification_status="passed", artifacts={...})`. Evidence pack present.
- **Weak path A (brain shortcut marking success):** brain fallback emits a textual answer, `_brain_text_response` calls `task_ledger.mark_terminal(status="succeeded", verification_status="passed")` at `bot.py:899` and `:956` without artifact validation.
- **Weak path B (brain tool-use without ledger):** brain calls `router.ask(lane="brain")`, the adapter may execute tools via callbacks, response is returned to user, **no `agent_tasks` row created** because the brain shortcut does not own a `task_id`.
- **Kairos handlers:** `kairos.tick → router.ask(lane="judge") → dispatch to one of 19 handlers`. Mutating handlers (`_handle_auto_publish_social`, `_handle_auto_deploy`) are env-gated; default OFF → create pending approval rows, do **not** create `agent_tasks` rows.

### Verification paths

- **Authoritative gate:** `task_completion.validate_completion` (`claw_v2/task_completion.py:191-200`) blocks `mark_terminal(succeeded, …)` if the artifact pack is missing required keys. If blocked, ledger downgrades to running. Live evidence of this firing: `observe_stream.task_false_success_reconciled` 2026-05-02 23:48:11 reconciled 6 skill tasks marked `missing_evidence`.
- **Bypass:** the brain shortcut at `bot.py:899`/`:956` calls `mark_terminal` directly. Whether `validate_completion` is reached depends on whether those rows hit the COMPLETION_CANDIDATES filter in `task_completion.py`.

### Scheduler / daemon paths

- `daemon_heartbeat` every ~24 s (last 24h: 2794 events)
- `daemon_tick` every ~60 s (last 24h: 1366 events), runs job groups: `daemon_health_check_guard`, `morning_brief`, `pipeline_poll*`, `kairos_tick`, `buddy_tick`, `a2a_process_inbox`, `evening_brief`, `site_monitor_*`
- `kairos_tick` registered at `interval_seconds=600` (`main.py:1040`); 139 ticks in 24h
- `recover_stale_tasks()` at startup (`task_handler.py:993`) — rehydrates `status='running'` rows via `_resume_autonomous_record(reason="startup_recovery")`
- `mark_stale_running_lost()` after 300 s (`daemon.py:76`) — marks abandoned tasks as `lost`
- **No retry loop on `failed` tasks.** Resume requires explicit `/resume <task_id>` or startup recovery on `running`.

---

## D. Evidence tables

### D.1 Phrase simulation matrix (owner-delegation — Spanish)

| Phrase | Matched handler | Flag-disabled? | Creates agent_tasks | Falls to brain | Risk |
|---|---|---|---|---|---|
| `correlo tu mismo` | none | n/a | no | yes | typo of `córrelo`; exact-list miss |
| `córrelo tú mismo` | none | n/a | no | yes | **owner delegation no-match (primary failure mode)** |
| `puedes correrlos tu` | none | n/a | no | yes | plural variant not coded |
| `hazlo tú` | #5 actionable_task_request | no | conditional | conditional | only effective phrase; needs Telegram + objective in state |
| `decide tú` | none | n/a | no | yes | not coded |
| `te toca a ti` | none | n/a | no | yes | not coded |
| `ya no tengo que teclear nada` | none | n/a | no | yes | disengagement intent not coded |
| `no me preguntes` | NLM meta-rejection | no | no | yes | meta-discussion guard, no autonomy grant |
| `encárgate tú` / `gestiona tú` | none | n/a | no | yes | not coded |
| `perfecto` / `ok` / `dale` / `procede` | #12 stateful_followup | no | conditional | no | **implicit approval — no `is_destructive` check** |
| `lo haces tu` | none | n/a | no | yes | variant not in exact list |
| `no me devuelvas el trabajo` / `no me hagas teclear` | none | n/a | no | yes | not coded |

### D.2 Phrase simulation matrix (owner-delegation — English)

All ownership phrases (`run it yourself`, `you run it`, `do it yourself`, `do it for me`, `you decide`, `take ownership`, `handle it`, `don't ask me to do it`, `stop asking me to run commands`) → **no match**, fall to brain. **English ownership intent is entirely uncoded.**

Proceed-token English (`go ahead`, `looks good`) does match handler #12 with the same implicit-approval risk as Spanish proceed tokens.

### D.3 Meta / introspection misroute matrix

| Phrase | Predicted route | Live DB evidence |
|---|---|---|
| `¿por qué no completas tareas fáciles?` | brain (intended) | **routed to coordinator on 2026-05-12 14:35:40, failed** (`autonomous_task_failed`) |
| `¿entendiste?` | brain | none in 30d (low-volume phrase) |
| `analiza esta conversación` | brain (likely) | none |
| `revisa el chat del bot de hoy` | NLM rejection or brain | none |
| `why did you fail?` / `what went wrong?` | brain | none |
| `implementa el fix` / `parchea bot.py` / `agrega tests …` | task_intent (handler #6 — DISABLED) → brain | none in 30d at coordinator lane |
| `investiga los logs de esta tarea fallida` | brain | none |

**Plus two additional misroutes captured in DB:**
- `Que queremos comunicar en el email?` (clarification question) → coordinator → fail (2026-05-14 00:58:04)
- `8eyt8R1Hp008liTCA98a` (random/secret-shaped token) → coordinator → fail (2026-05-11 21:28:16)

### D.4 Open task / job summary (last 30d)

**agent_tasks (n=50):** cancelled 25 · succeeded 16 · lost 5 · failed 4
**verification_status:** not_applicable 25 · passed 16 · failed 8 · blocked 1
**succeeded + verification_unknown:** 0 (clean — ledger gate working)
**failed by Codex 300 s timeout:** 4
**failed by `runtime lost authoritative backing state`:** 5

**agent_jobs (n=33):** failed 14 (42%) · completed 13 · cancelled 6

**session_state (n=35):** **100% `assisted`**, 0 with `pending_action`, 0 with non-empty `task_queue_json`.

**artifacts (n=22)** vs **task_outcomes (n=2411)** — evidence-pack creation is 1.8% of outcome volume.

### D.5 Recent failure summary

| Type | Count (window) | Source |
|---|---|---|
| `circuit_breaker_tripped` | 450 (30d) | observe_stream — all `cost_per_hour`, threshold $10/h, peak $20.85/h |
| `tool_blocked_by_freeze` | 136 (30d) | observe_stream — Tier-1 read tools blocked during freezes |
| `sdk_post_tool_use_failure` | 142 (7d), 705 (24h) | observe_stream — many are `is_error: false, Exit code 1` from Bash (tool-result tracking issue, not real failures) |
| `kairos_decide_failed` | 117 (7d) | observe_stream — judge lane decision errors |
| `observation_window_freeze_set` | 7 (30d) | all `circuit_breaker:cost_per_hour`, actor brain/verifier |
| `llm_circuit_open` | 19 (30d) | brain anthropic and worker codex |
| `autonomous_task_failed` | 3 (30d) | meta/clarification misroutes (see D.3) |
| `task_false_success_reconciled` | 1 (2026-05-02) | defense-in-depth working — 6 skill tasks reclassified `missing_evidence` |

### D.6 Test results summary

```
Command:
  python -m pytest tests/test_dispatch_routing.py tests/test_bot.py \
                   tests/test_task_handler.py tests/test_task_ledger.py \
                   tests/test_capability_router.py tests/test_state_handler.py \
                   tests/test_observation_window.py tests/test_llm.py -q --tb=line
Result:
  240 passed, 4 xfailed, 11 subtests passed in 32.02 s (exit 0)
```

The 4 xfails appear in `test_dispatch_routing.py` (xxxx at positions 4-7). They are recorded as expected, consistent with INTERNAL_WIRING note that handler #5/#6 overlap is "codified as xfail strict". No regressions related to owner delegation, autonomy, ledger, or provider lanes were detected in this targeted suite.

**Tests that appear missing or thin (gap inventory):**
- No test asserts the bot **does not emit** "ejecuta este comando" / "decide tú" / "te toca a ti" in any user-facing response.
- No live test exercises handler #6 patterns (it is gated off by default; presumably xfailed strictly).
- No test that the meta-question `¿por qué no completas tareas fáciles?` routes to chat, not coordinator.
- No test for `pending_action` durability across daemon restart.
- No test for Codex 300 s timeout behaviour at coordinator boundary.

### D.7 Config / flag summary (redacted — see `tmp/audit/config_flags_redacted.json`)

26 keys in `.env`, names captured only. Routing-relevant flags:
- `CLAW_DISABLE_TASK_INTENT_ROUTER` default `1` (handler #6 dead by default).
- `CLAW_DISABLE_TELEGRAM_ACTIONABLE_TASK_ROUTER` default `0` (handler #5 on).
- `CLAW_DISABLE_NLM_NATURAL_LANGUAGE` default `0` (handler #14 on).
- `CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES` default `0` (unused in current code).
- `KAIROS_AUTO_PUBLISH_SOCIAL`, `KAIROS_AUTO_DEPLOY` default `0` (Kairos creates approvals rather than mutating).
- Tier policy: `CLAW_TIER_AUTOEXEC_MAX` set in `.env` (value redacted; default in code is `TIER_LOCAL_MUTATION=2`).
- `CLAW_BUDGET_CAP_DAILY` set in `.env`. Hourly breaker threshold is hard-coded around 10 USD in observed payloads.

---

## E. Root causes (separated)

### E.1 Routing causes

- **Owner delegation no-match.** `_looks_like_actionable_followup` is an exact-string list (e.g., `"hazlo tu"`); no accent normalisation aligned with the matcher, no fuzzy match, no English variants. Phrases like `córrelo tú`, `decide tú`, `te toca a ti` cannot be intercepted at handler #5 and inevitably hit brain.
- **Handler #6 dead.** Task-intent patterns for `implementa`, `parchea`, `agrega tests` exist in code but are disabled because `CLAW_DISABLE_TASK_INTENT_ROUTER=1` is the default. The patterns are partly subsumed by handler #5, but #5 only fires for Telegram with state-derived objective — so an explicit "patch X" instruction from the user typically goes to brain (or, with autonomy_mode=autonomous, to coordinator via #15).
- **Coordinator catches the wrong inputs.** `_maybe_handle_capability_route` and `start_autonomous_task` accept meta-phrases (`"por qué no…"`, `"que queremos comunicar…"`, raw tokens) without semantic gating on "is this an actionable objective?". The DB has three documented misroutes in 7 days.

### E.2 State causes

- **`pending_action` and `task_queue_json` never persisted to DB.** Per `state_handler.remember_assistant_turn_state`, these are written through `brain.memory.update_session_state` — but the on-disk `session_state` rows show all zero non-empty values. Either the writer uses an in-memory layer only, or the writer path is short-circuited. Result: proceed-token resolution against pending state will silently lose state on daemon restart.
- **`session_state.verification_status` always `unknown`.** The session-level column appears unused; task-level `agent_tasks.verification_status` is the authoritative signal. This is fine in current architecture but the dead column is confusing for any reader.
- **Autonomy mode never escalates.** No code path in the current dispatch sets a session's `autonomy_mode` to `autonomous` durably. Coordinator handler #15 is effectively a dead handler for users in their default sessions.

### E.3 Ledger / evidence causes

- **`mark_terminal` from brain shortcut at `bot.py:899` and `:956` sets `verification_status="passed"` without artifact validation.** Whether `validate_completion` rescues it depends on COMPLETION_CANDIDATES coverage. The May-02 reconciliation event proves the rescue can run; the absence of more reconciliation events since then could be either real improvement or that the gate misses these shortcut rows.
- **Brain tool-use bypasses `agent_tasks`.** When `router.ask(lane='brain')` triggers tool callbacks, no `task_id` exists in the brain shortcut, so no ledger row is opened. This is the single largest gap in the "every non-trivial action has a durable record" invariant.
- **Only 22 artifacts vs 2411 task_outcomes.** Evidence pack creation rate is low relative to learning-log writes; suggests artifact upload is a coordinator-only path and short success paths skip it.

### E.4 Prompt / style causes

- **`AUTONOMY_EXECUTION_CONTRACT` is prose-only.** Lines 116-138 of `brain.py` enumerate forbidden patterns ("Pega el output", "dame el token", "ejecuta este comando y luego este otro", "no puedo por sandbox") — but no runtime sanitiser scans outbound messages. Brain compliance depends entirely on model behaviour.
- **No outbound style sanitiser.** `_sanitize_chat_response` (bot_helpers.py:581-623) handles ID redaction and internal-leak suppression. It does **not** detect manual-handoff phrasing.

### E.5 Config / provider causes

- **Cost-per-hour threshold $10/h is too low** for the actual workload, or the workload is genuinely overspending. Either way, the trip rate (15/day average) is harming autonomy — every trip blocks Tier-1 tools.
- **Codex 300 s timeout** at worker lane is the hard cap on coordinator coding tasks. With brain+verifier+worker being multi-LLM, this cap is hit on legitimate work, not just runaways.
- **Provider/lane enforcement is solid** — no codex on non-tool lanes (`llm.py:125`, `:135`). No mismatch evidence in logs.

### E.6 Scheduler / idle causes

- **`recover_stale_tasks` rehydrates `running` rows but not `failed` rows.** No safe automatic retry; user must say `/resume`. Combined with "lost" reclassification at 300 s, abandoned tasks pile up as `lost` with no path back.
- **No idle executor that advances open Tier-1/Tier-2 tasks.** The daemon ticks cron jobs (heartbeat, briefs, site monitors, Kairos), not user-owned task backlog. Phrases like "ya no tengo que teclear nada" have nothing to attach to — there is no backlog executor today.
- **Kairos is a decision-and-notify loop, not a backlog drainer.** It uses judge lane to pick from 19 handlers per tick, and the mutating handlers are env-gated off.

### E.7 Test coverage causes

See §D.6 gap inventory. The most material gaps are absence of manual-handoff-language assertions, absence of meta-question-not-routed-to-coordinator assertions, and absence of cross-restart durability tests.

---

## F. Hermes-gap readiness (no implementation)

| Hermes gap | Current Dr. Strange readiness | Prerequisites | Risks if added now | Recommended timing |
|---|---|---|---|---|
| Autonomous skill creation | **Not ready.** Only 22 artifacts vs 2411 outcomes; success-without-evidence path exists in brain shortcut; trajectories can fail silently. | (1) Durable ledger for every tool-using turn. (2) Evidence pack reliably populated. (3) Successful-vs-failed trajectory segregation. (4) Skill storage path + loader + version + tier gating. None of these exist as a stable foundation. | Skill synthesis would learn from contaminated trajectories (success-without-evidence rows, meta-misroute rows). Could codify wrong behaviour. | Defer until Wave 0 + Wave 1 (ledger + evidence-pack) are merged and stable for ≥7 days. |
| Model router | **Partially ready.** `provider_for_lane` enforces tool-capability per lane (`llm.py:125`). | Need a routing layer ABOVE `provider_for_lane` that selects model per lane × cost-budget × latency-budget × confidence-target. Not present. | Adding model router on top of the broken cost breaker would oscillate. Fix breaker thresholds first. | Safe to prototype in shadow mode (read-only, log decisions) after Wave 0. |
| Multi-channel | **Partially ready.** Telegram + web/mac + daemon all enter `bot.handle_text`. Channels share session state. | Need first-class channel identity in session_state + per-channel sanitiser policy + idempotency across channels. Idempotency_keys table is empty (0 rows). | Adding channels without idempotency will fan out duplicate work. | Defer until idempotency_keys is exercised and persistence test exists. |
| Multi-backend / Modal | **Not ready.** All workers run locally; Codex CLI 300 s timeouts already breaking. | Need worker abstraction with remote execution, retry/checkpoint semantics, secret distribution policy. None exist. | Adding Modal would mask local timeouts with remote ones unless retry/checkpoint is solved. | Defer until coordinator can complete its own work locally without 300 s aborts. |
| agentskills.io | **Not ready.** Depends on autonomous skill creation. | Same as row 1. | Same as row 1. | Same as row 1. |

**Net:** none of the Hermes gaps are safe to *implement* now. Two (model router, multi-channel) are safe to *shadow-prototype* once Wave 0 routing reliability is in place.

---

## G. Fix sequencing recommendation (no implementation)

### What to fix first (P0 — Wave 0 scope)

1. **Add owner-delegation phrase set as first-class router intent** (handler before #15, before brain fallthrough). Cover both languages, accent-normalised matching, fuzzy boundary handling. Emit autonomy grant + try to attach to a derivable objective; if none, *summarise current backlog* rather than running a generic command.
2. **Gate coordinator on "is this an actionable objective?"** Reject short meta-phrases, clarification questions, raw tokens, and questions starting with "por qué / why / what". Route those to brain with a meta-aware system prompt.
3. **Persist `pending_action` and `task_queue_json` to DB on every assistant turn.** The in-memory-only behaviour means a restart loses the resolver state. Acceptance test: after `launchctl kickstart -k gui/501/com.pachano.claw`, the next user `"ok"` still resolves correctly.
4. **Enforce ledger row creation when brain tool-use occurs.** Either (a) brain shortcut opens a synthetic agent_task before invoking tools, or (b) brain shortcut is forbidden from calling tools and must hand off to coordinator. Acceptance: every `sdk_post_tool_use` event in `observe_stream` must have a `task_id` field populated.
5. **Add outbound-message sanitiser for forbidden manual-handoff phrases** ("Pega el output", "ejecuta este comando", "decide tú", "te toca a ti", "lo haces tu", "do it yourself", "you decide"). Rewrite or reject, and emit an `internal_message_suppressed_from_chat`-style event.

### What to fix next (P1 — Wave 0 / Wave 1 hinge)

6. Tune `cost_per_hour` breaker threshold (or split per-lane budgets) — current $10/h with $20+/h observed peaks means breaker trips during normal coordinated work.
7. Raise or remove the Codex 300 s worker timeout for coordinator-bound work, or implement work-splitting + checkpoint so 300 s is not the hard cap.
8. Resolve "lost" task class: either auto-resume on next idle tick or surface them to the user with a one-tap resume button.
9. Add `is_destructive` classifier on `pending_action` strings before stateful_followup acts on them — current resolver runs whatever the prior assistant turn proposed.

### What to defer (P2)

- Hermes gaps (skill creation, model router, multi-backend, multi-channel, agentskills.io) — per §F.
- session-level `verification_status` cleanup (cosmetic).
- Reducing `sdk_post_tool_use_failure` noise (most are Bash exit-code-1 events that are not really failures).

### What to avoid touching while PR/conflicts/stashes exist

- The 15 stashes (`stash@{0..14}`) — many are `claw:autostash:tg-574707975:…` from background work. Do not pop during refactor; reconcile owners first.
- The `.worktrees/claw-p0` worktree on branch `feat/claw-p0-telemetry` — separate branch with its own pending work. Wave 0 refactor on `main` should not assume p0 is merged.
- `restart_requested.json` from 2026-04-24 — stale signal; only Hector should clear or fire it.

### What tests must exist before any patch

- `tests/test_owner_delegation_routing.py`: each phrase in §D.1/§D.2 routes deterministically; success path creates `agent_tasks` row; failure path falls to brain with an explicit `dispatch_decision` event.
- `tests/test_meta_introspection_misroute.py`: meta phrases never reach coordinator regardless of autonomy_mode.
- `tests/test_pending_action_persistence.py`: `pending_action` and `task_queue_json` survive simulated restart (close + reopen DB).
- `tests/test_brain_tooluse_ledger.py`: every brain tool-use opens a ledger row.
- `tests/test_manual_handoff_sanitizer.py`: every forbidden phrase is caught.

---

## H. No-change confirmation

- **No application code changed.** Only files created under `reports/`, `tmp/audit/`.
- **No configuration changed.** `.env`, `data/observation_window.json`, launchd plist untouched.
- **No secrets exposed.** Values in `.env` redacted to names only; OAuth/API tokens never read.
- **No production restart performed.** Daemon PID 61723 still running, uninterrupted; `restart_requested.json` not modified.
- **Only report and evidence files written:**
  - `reports/bot_full_pre_fix_audit_2026-05-16.md`
  - `tmp/audit/router_trace_matrix.json`
  - `tmp/audit/phrase_simulation_results.json`
  - `tmp/audit/task_ledger_snapshot_redacted.json`
  - `tmp/audit/config_flags_redacted.json`
  - `tmp/audit/test_results_pre_fix.txt`
  - `tmp/audit/worktree_status_pre_fix.txt`

---

## I. Appendix

### I.1 Commands run (representative)

```
git status --short; git branch --show-current; git log --oneline -15
git diff --stat; git diff --cached --stat; git diff --check
git stash list; git worktree list
rg -n '<<<<<<<|^=======$|>>>>>>>' .
ps aux | rg 'claw|com\.pachano'
launchctl print gui/501/com.pachano.claw
lsof -nP -iTCP -sTCP:LISTEN | rg 8765
ls -la ./claw.db ./data/claw.db ./data/buddy.db
sqlite3 ./data/claw.db ".schema agent_tasks"
sqlite3 ./data/claw.db "SELECT status, COUNT(*) FROM agent_tasks WHERE …"
sqlite3 ./data/claw.db "SELECT autonomy_mode, COUNT(*) FROM session_state GROUP BY 1"
sqlite3 ./data/claw.db "SELECT event_type, COUNT(*) FROM observe_stream WHERE …"
sqlite3 ./data/claw.db "SELECT … FROM observe_stream WHERE event_type='task_false_success_reconciled' …"
python -m pytest tests/test_dispatch_routing.py … -q
rg -n '_looks_like_pending_tasks_diagnostic' claw_v2/ tests/
rg -n 'mark_terminal' claw_v2/bot.py
rg -n 'os\.getenv\("(CLAW|KAIROS|TASK|DAEMON|OBSERVATION)_' claw_v2/
sed -n '115,140p' claw_v2/brain.py
```

### I.2 Files inspected (representative)

- `claw_v2/INTERNAL_WIRING.md` (header + TODOs §7)
- `claw_v2/bot.py` (lines around 1644-3818 dispatch chain, 2813 brain shortcut, 899/956 mark_terminal)
- `claw_v2/bot_helpers.py` (lines around 283-313 PROCEED_TOKENS, 488 proceed_request, 581-623 sanitize, 626-644 autonomy_grant, 3882-3900 actionable_followup)
- `claw_v2/state_handler.py` (lines around 141-314 stateful_followup resolver)
- `claw_v2/capability_router.py` (lines 39-41 CRITICAL_TASK_KINDS, 119-161 classify_autonomy_intent)
- `claw_v2/task_handler.py` (lines 141-308 coordinator/start, 993-1096 recover/resume)
- `claw_v2/task_ledger.py` (lines 93-218 create/mark_terminal + validate_completion bridge)
- `claw_v2/task_completion.py` (lines 89-202 validate_completion)
- `claw_v2/brain.py` (lines 115-138 AUTONOMY_EXECUTION_CONTRACT, 219-255 handle_message)
- `claw_v2/llm.py` (lines 22 NON_TOOL_LANES, 125-135 lane/tool-capable enforcement)
- `claw_v2/kairos.py` (lines 71-182 KairosService.tick/dispatch, 710-792 mutating handlers)
- `claw_v2/daemon.py` (line 76 mark_stale_running_lost)
- `claw_v2/main.py` (line 1040 scheduler register, 418 autoexec_max_tier)
- `claw_v2/config.py` (lines 426 DB_PATH default, 475 observability config, 589-612 provider_for_lane)
- `claw_v2/tools.py` (lines 153-155 tier constants, 444 enforcement)
- `claw_v2/observation_window.py`, `data/observation_window.json` (current state)
- `claw_v2/nlm_handler.py` (line 53 flag check)
- `claw_v2/agents.py` (line 1181 STRICT_SOUL_MODEL)

### I.3 Redacted SQL queries used (representative)

```sql
SELECT status, COUNT(*) FROM agent_tasks
 WHERE datetime(created_at,'unixepoch') >= datetime('now','-30 days')
 GROUP BY status ORDER BY 2 DESC;

SELECT verification_status, COUNT(*) FROM agent_tasks
 WHERE datetime(created_at,'unixepoch') >= datetime('now','-30 days')
 GROUP BY verification_status ORDER BY 2 DESC;

SELECT autonomy_mode, COUNT(*) FROM session_state GROUP BY 1;
SELECT mode, COUNT(*) FROM session_state GROUP BY 1;

SELECT event_type, COUNT(*) FROM observe_stream
 WHERE timestamp >= datetime('now','-30 days')
   AND (event_type LIKE '%circuit%' OR event_type LIKE '%cost%'
        OR event_type LIKE '%freeze%' OR event_type LIKE '%false_success%')
 GROUP BY event_type;

SELECT timestamp, lane, substr(payload,1,200) FROM observe_stream
 WHERE event_type='autonomous_task_failed' ORDER BY id DESC LIMIT 5;
```

(All `substr(payload,…)` queries cap at ≤300 characters; secret-shaped tokens redacted before emission to evidence files.)

### I.4 Limitations

- Static routing analysis only. No live in-process simulation of phrases; matchers were inspected and traced manually. A future audit pass could fire each phrase through the actual bot with `CLAW_DRY_RUN`-style instrumentation (does not exist; would need to be built).
- `observe_stream.dispatch_decision` payloads were not bulk-decoded for per-route counts (would require iterating 693 events in 24h × 30d ≈ 20K events). The misroute evidence here is from `autonomous_task_failed` only — a stronger characterisation would parse `dispatch_decision`.
- `brain.memory` internal storage path was not opened. Whether `pending_action` / `task_queue_json` writes are intentionally in-memory-only or are a bug requires reading `memory.py` deeper than this pass.
- `task_completion.py` COMPLETION_CANDIDATES filter was not enumerated; whether brain-shortcut `mark_terminal` paths land in the gate is unknown without that.
- "Tool-use-without-ledger" was reasoned from code structure, not from a live capture correlating `sdk_post_tool_use` events to `agent_tasks.task_id`. Cross-correlation would be the strongest evidence.

---

**End of audit. Read-only deliverables complete. Do not implement fixes until Hector confirms sequencing.**
