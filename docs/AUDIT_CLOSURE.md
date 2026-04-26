# Agent Reliability Audit Closure

Date: 2026-04-26
Workspace: `/Users/hector/Projects/Dr.-strange`
Status: **OPEN — conditional close pending three blockers (D.1, D.3, D.5)**

## Final State

The agent is operationally healthy at runtime, but iteration 3 of the audit
identified five new risks introduced by post-audit feature commits. Three are
blockers for final closure (see `Iteration 3 Findings` below).

Last local check:

```bash
bash scripts/diagnose.sh --limit 5
```

Result:

```text
Claw diagnostics: healthy
launchd_loaded: True
process_running: True
port_listening: True
database_readable: True
active_jobs: 0
active_tasks: 0
recent_error_events: 0
acknowledged_error_events: 2
```

Acknowledged events:

- `firecrawl_paused` at `2026-04-26 17:24:05`: known insufficient credits, paused until `2026-04-27 17:24 UTC`.
- `scheduled_job_error perf_optimizer` at `2026-04-25 20:32:02`: historical Codex timeout, mitigated by auto-pause.

These acknowledgements are stored in `data/diagnostics_acks.json` and expire automatically. They do not delete `observe_stream` evidence.

## Verification

Latest completed validation:

```bash
uv run pytest -q
git diff --check
bash scripts/diagnose.sh --limit 5
```

Observed result:

```text
990 passed, 6 subtests passed
git diff --check clean
diagnostics status healthy
```

## Original Failure Matrix Coverage

| Failure | Closure |
| --- | --- |
| B5 port 8765 | Web transport uses reusable server behavior and restart wrapper is launchd-aware. |
| A2 Responses 400 | OpenAI request normalization and retry handling hardened. |
| A4 session resume | Provider session resume now logs recoverable failure and falls back to fresh session. |
| A1 Codex CLI stdin | Codex adapter sends prompt via stdin, preflights CLI/auth/cwd, retries startup failure. |
| L3 Python pinning | Runtime pinned to Python `>=3.13,<3.14`; lockfile refreshed. |
| A3 Firecrawl credits | Credit/rate failures classify, pause scraping, emit actionable events. |
| L6 shutdown/thread leak | Web transport/daemon shutdown now observes lingering thread/process risks. |
| L5 history compaction | Session history compacts into rolling summary instead of unbounded context growth. |
| L1/L4 validation/retry | `LLMRequest.validate`, provider circuit breaker, OpenAI retry, and judge-lane evidence contract added. |
| NotebookLM fallback | NotebookLM failures classify and degrade to local wiki when possible. |
| Linear pipeline timeout | Pipeline polling now degrades with backoff instead of throwing cron stacktraces. |
| Telegram pool timeout | Telegram transport uses explicit pools/timeouts and tolerant stop cleanup. |
| Diagnostics gap | Local diagnostics CLI, runbook, and ack/snooze workflow added. |

## Iteration 3 Findings (2026-04-26, post-fix verification)

Iteration 3 re-audited the codebase after the closure commits and the six
feature commits that landed afterwards (`ccbca20`, `13895a2`, `7a84b70`,
`7dee2c5`, `bf590a8`, `bb625da`).

### Verified closures (10 of 13)

`A1`, `A2`, `A3`, `A4`, `B5`, `L1`, `L3`, `L4`, false-success heuristic, and
coordinator timeout are confirmed closed in the current code. See the matrix
above for the corresponding evidence.

### Partial closures (3 of 13)

| Item | Where | Gap |
| --- | --- | --- |
| L5 history compaction | `claw_v2/memory.py:497-543` | `compact_session_messages()` exists but is not auto-triggered by `store_message`; relies on a manual call. |
| L6 shutdown thread | `claw_v2/web_transport.py:124-126` | `join(timeout=5)` plus `is_alive()` warning, no escalation (no `terminate`/`kill`). |
| Coordinator retry backoff | `claw_v2/coordinator.py:228-238` | Retry loop uses `continue` without `time.sleep`; tight spin possible under fast failures. |

### New risks introduced after the closure

| ID | Component | Lines | Severity | Blocker? |
| --- | --- | --- | --- | --- |
| **D.1** | `claw_v2/diagnostics.py` | 277-350 | Critical | **Yes** |
| **D.3** | `claw_v2/bot.py` (notebook routing, commit `7dee2c5`) | 440-450 | Critical | **Yes** |
| **D.5** | `claw_v2/web_transport.py` | 114 (`serve_forever`) | High | **Yes** |
| D.2 | `claw_v2/operational_alerts.py` | 50, 69-75 | High | No |
| D.4 | `claw_v2/main.py` + `claw_v2/cron.py` | 770-880, 55-78 | Medium | No |

Details:

- **D.1 — Acks TOCTOU.** `_load_acknowledgements` and `acknowledge_events` in
  `claw_v2/diagnostics.py` read and rewrite `data/diagnostics_acks.json`
  without `fcntl.flock` or atomic replace. Two parallel
  `bash scripts/diagnose.sh --ack-current` invocations can drop acks. The same
  module also opens the SQLite DB read-only without `PRAGMA busy_timeout`, so a
  daemon `BEGIN IMMEDIATE` in `claw_v2/jobs.py` can stall `diagnose.sh`.
- **D.3 — Coordinator bypass on notebook tasks.** Commit `7dee2c5` routes
  notebook completion questions directly to `NlmHandler` before the coordinator
  is invoked. The path skips judge lane, evidence pack, verification, and never
  records into the task ledger. Audit trail is broken for that surface.
- **D.5 — Daemon liveness blind spot.** `web_transport.serve_forever()` runs in
  a daemon thread with no `/health` endpoint or heartbeat log. If the WSGI
  thread dies on an unhandled exception, launchd does not detect it.
- **D.2 — Alerts dedupe race.** `_last_sent` dict in
  `claw_v2/operational_alerts.py` is read/written without a lock; concurrent
  events can each pass the cooldown check.
- **D.4 — Brief cron overlap.** Morning brief and evening brief are both
  scheduled at 86400s intervals with no de-dupe across the schedule, so a slow
  morning run can collide with the evening run.

### Other observations

- `bot.log` showing the historical port-8765 crash is from 2026-04-14, before
  `c21be4f` landed `_ReusableWSGIServer` and the bind retry loop. The current
  process (PID 14122) holds 127.0.0.1:8765 cleanly.
- `data/diagnostics_acks.json` currently has four active entries; expired acks
  are filtered on load but never purged on write, so the file grows linearly
  with unique alerts.
- Commit `bb625da` ("conversational replies") relaxes brain tone instructions.
  The Telegram-terse preference recorded in user memory should be re-validated
  in practice; flag any drift.

## Operational Commands

Fast health:

```bash
bash scripts/diagnose.sh
bash scripts/diagnose.sh --json
```

Acknowledge known external degradation without deleting audit evidence:

```bash
bash scripts/diagnose.sh --ack-current --ack-hours 24 --ack-reason "Known external outage or already fixed"
```

Restart:

```bash
bash scripts/restart.sh
```

Relevant bot commands:

```text
/status
/jobs
/job_status <task_id>
/job_trace <task_id> [limit]
/tasks
/task_status
/agent_status perf-optimizer
/agent_resume perf-optimizer
/pipeline_status
/traces [limit]
/trace <trace_id> [limit]
```

## Risks Deferred

- Firecrawl credits remain an external operational dependency. The current pause is acknowledged, not solved commercially.
- `perf_optimizer` needs a 12-24h soak or a manual `/agent_run perf-optimizer 1` to verify the real-world auto-pause path after the Codex timeout mitigation.
- `observe_stream` has no retention/archival policy yet.
- `data/diagnostics_acks.json` is local runtime state and should not be treated as source code.
- The tree contains unrelated `prototypes/programmatic-seo` and `public/` changes that must not be mixed into this audit commit.
- D.2 (alerts dedupe race) and D.4 (brief cron overlap) are deferred for the
  90-day window. They are not blockers for closure but should be tracked.
- Partial closures (L5 auto-trigger, L6 escalation, coordinator retry backoff)
  are deferred unless they reappear in production logs.

## Commit Scope

Recommended commit split:

### Commit 1: provider and runtime reliability

```bash
git add \
  pyproject.toml \
  uv.lock \
  claw_v2.egg-info/PKG-INFO \
  claw_v2.egg-info/requires.txt \
  claw_v2/adapters/base.py \
  claw_v2/adapters/codex.py \
  claw_v2/adapters/openai.py \
  claw_v2/llm.py \
  claw_v2/retry_policy.py \
  claw_v2/memory.py \
  claw_v2/brain.py \
  claw_v2/bot.py \
  claw_v2/cron.py \
  claw_v2/daemon.py \
  claw_v2/learning.py \
  claw_v2/observe.py \
  claw_v2/tools.py \
  claw_v2/web_transport.py \
  tests/test_codex_adapter.py \
  tests/test_daemon.py \
  tests/test_llm.py \
  tests/test_memory_core.py \
  tests/test_observe_subscribe.py \
  tests/test_retry_policy.py \
  tests/test_secondary_providers.py \
  tests/test_tools.py \
  tests/test_web_transport.py
```

Suggested message:

```text
fix: harden provider runtime reliability
```

### Commit 2: autonomous jobs and external integrations

```bash
git add \
  claw_v2/agent_handler.py \
  claw_v2/agents.py \
  claw_v2/bot_helpers.py \
  claw_v2/lifecycle.py \
  claw_v2/main.py \
  claw_v2/notebooklm.py \
  claw_v2/pipeline.py \
  claw_v2/skills.py \
  claw_v2/telegram.py \
  claw_v2/wiki.py \
  claw_v2/operational_alerts.py \
  tests/test_agents.py \
  tests/test_notebooklm.py \
  tests/test_pipeline.py \
  tests/test_skills.py \
  tests/test_telegram.py \
  tests/test_wiki.py \
  tests/test_judge_lane_contract.py \
  tests/test_operational_alerts.py
```

Suggested message:

```text
fix: degrade autonomous integrations gracefully
```

### Commit 3: diagnostics and operations

```bash
git add \
  claw_v2.egg-info/SOURCES.txt \
  claw_v2/diagnostics.py \
  scripts/diagnose.sh \
  scripts/restart.sh \
  docs/OPERATIONS_RUNBOOK.md \
  docs/AUDIT_CLOSURE.md \
  tests/test_diagnostics.py
```

Suggested message:

```text
chore: add operational diagnostics and audit closure
```

## Do Not Include In Audit Commits

These are visible in the working tree but unrelated to the reliability audit:

```text
prototypes/programmatic-seo/app/sector/[slug]/page.tsx
prototypes/programmatic-seo/out/
prototypes/programmatic-seo/public/
public/
```

Local runtime state should also stay out of source commits unless intentionally promoted:

```text
data/diagnostics_acks.json
data/claw.db
logs/
```

## Blocker Remediation Plan (iteration 3)

These three items must land before final closure. Suggested sequence and
estimated effort:

1. **D.3 — Restore coordinator/judge for notebook tasks** (`claw_v2/bot.py`
   ~440-450 + `claw_v2/nlm_handler.py`). Route through `coordinator.ask()`
   even for notebook completion questions, or wrap the direct path so the task
   ledger and verification are recorded. Add `test_notebook_task_coordinator_contract`.
   Effort: 2-3 days.
2. **D.1 — Lock the acks file** (`claw_v2/diagnostics.py:277-350`). Use
   `fcntl.flock` on the JSON file or write via `os.replace` after a temp
   write. Purge expired entries on every write. Add a parallel-write test.
   Add `PRAGMA busy_timeout=5000` to the read-only DB connection.
   Effort: 0.5-1 day.
3. **D.5 — Heartbeat/health for the daemon** (`claw_v2/web_transport.py:114`
   plus a periodic emitter in `claw_v2/lifecycle.py`). Expose `/health` from
   the WSGI app and log a heartbeat line every 60s. Wire smoke check into
   `scripts/diagnose.sh`. Effort: 1-2 days.

Total: roughly one work week.

## Close Criteria

The audit can be closed when:

- `bash scripts/diagnose.sh` reports `healthy`.
- `uv run pytest -q` passes.
- `git diff --check` is clean.
- Firecrawl credit pause is acknowledged or resolved.
- `perf_optimizer` no longer creates `scheduled_job_error` on the next cron run, or pauses itself through `auto_research_adapter_error`.
- The audit changes are committed without unrelated prototype/public output.
- **D.1 acks file lock landed and covered by a parallel-write test.**
- **D.3 notebook routing reroutes through the coordinator (or records the task
  ledger entry and verification status) and is covered by
  `test_notebook_task_coordinator_contract`.**
- **D.5 daemon exposes `/health`, emits a periodic heartbeat, and
  `scripts/diagnose.sh` consumes the heartbeat as part of the healthy check.**
