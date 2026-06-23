# Claw Operations Runbook

This runbook covers the local production contract for Claw on Hector's Mac.

## Runtime Contract

- Launchd label: `com.pachano.claw`
- Watchdog label: `com.pachano.claw-watchdog`
- Chrome CDP label: `com.claw.chrome-cdp`
- Entrypoint: `ops/claw-launcher.sh`
- Watchdog: `ops/claw-watchdog.sh`
- Chrome CDP supervisor: `ops/chrome-cdp-launcher.sh`
- Python process: `.venv/bin/python -m claw_v2.main`
- Web UI: `http://127.0.0.1:8765/`
- Chat API: `POST /api/chat`
- Database: `data/claw.db` unless `DB_PATH` is set in `~/.claw/env`
- Logs: `logs/claw.log`, `~/.claw/claw.stdout.log`, `~/.claw/claw.stderr.log`

Treat Telegram, web chat, cron, and CLI as channels. Task state and session state live in SQLite.

## First Response

Run:

```bash
bash scripts/diagnose.sh
```

For machine-readable output:

```bash
bash scripts/diagnose.sh --json
```

To acknowledge a known degraded condition without deleting audit evidence:

```bash
bash scripts/diagnose.sh --ack-current --ack-hours 24 --ack-reason "Known external outage or already fixed"
```

This writes `data/diagnostics_acks.json`; future matching event IDs are still shown as acknowledged, but they no longer keep status in `attention`. New events still surface normally.

The diagnostic checks:

- launchd service state
- local Python process
- port `8765` listener
- recent observe events
- active generic jobs
- active autonomous task records
- cron run state

The launchd watchdog runs `ops/claw-watchdog.sh` on its interval. It does not
restart Claw for ordinary `attention` states; it restarts only when diagnostics
reports a `critical` condition tied to process, port, heartbeat, or web transport
liveness.

The restart *action* is debounced in the testable `claw_v2.watchdog` module so a
transient `critical` reading during the daemon's own bootstrap never triggers a
restart (which historically self-perpetuated into a restart loop). The diagnostics
`critical` condition itself is unchanged; only the watchdog's reaction is gated:

- **Bootstrap grace** — `CLAW_WATCHDOG_BOOTSTRAP_GRACE_S` (default `120`): the
  watchdog holds while the daemon process has been up for fewer than this many
  seconds, giving a slow bootstrap time to finish coming up.
- **N-strikes** — `CLAW_WATCHDOG_STRIKES` (default `2`): the watchdog requires
  this many consecutive `critical` + restartable readings before it reaches the
  restart threshold; the counter persists in `~/.claw/watchdog_state.json`.
- **Port wait** — `CLAW_RESTART_PORT_WAIT_S` (default `10`): seconds
  `scripts/restart.sh` waits for the web port to listen after a restart; raise it
  when a slow bootstrap (e.g. DB contention) makes the port come up late.

Chrome CDP is managed by `ops/chrome-cdp-launcher.sh` when installed via
`ops/com.claw.chrome-cdp.plist`. The launcher reuses a healthy CDP process and
refuses to remove `SingletonLock` while the configured profile is active.

## Browser Atomic Tools Current Status

As of the 2026-06-23 operator smoke, browser atomic tools from #112 are merged,
deployed, and live at code_version `e4a3ee2`
(`e4a3ee2fd9399b8ff7633cde5be4aafe6ccfd2ca`). The live daemon evidence was
`agent_startup_context` event `270260`, pid `33828`.

The read-only smoke used the runtime tool path:
`ToolRegistry.default(...).execute(...)`. It did not use an ad-hoc Playwright
script.

Smoke coverage:

- `BrowserNavigate` to `https://example.com` passed.
- `BrowserSnapshot` on the same session passed.
- `observe_stream` included `browser_tool_action_started` and
  `browser_tool_action_completed` for navigate/snapshot.
- The snapshot contained `Example Domain` and stayed bounded.
- Sensitive payload hits were `0`, and no URL userinfo/query/fragment was
  persisted.
- RuntimeDb/WAL/SQLite/database-lock/browser_tools/tool-policy errors were `0`.
- The post-smoke watchdog stale-filter smoke was `PASS` with
  `database_open_mode=read_only`.

Safety boundaries:

- `BrowserClick`, `BrowserType`, submit, screenshot, private/authenticated
  sites, and mutating browser actions were not executed in the smoke.
- `BrowserClick` and `BrowserType` remain Tier 3 approval-gated.
- No private or authenticated browser state was inspected.
- F2 remains design-only and is not implemented.

## Watchdog Stale-Filter Smoke (Dry Run)

Before reactivating or reloading the watchdog after F1.4 stale-event filtering,
run the read-only smoke script against the target runtime DB:

```bash
.venv/bin/python scripts/audit/watchdog_stale_filter_smoke.py \
  --db data/claw.db \
  --expected-code-version c42ae47
```

The script opens SQLite with `mode=ro`, reads `observe_stream`, and reports:

- The latest `agent_startup_context` payload and whether its `code_version`
  matches `c42ae47`.
- Recent watchdog-relevant observe errors classified as actionable, stale
  historical, or unknown relevance using the same current-daemon-window rules as
  diagnostics.
- Whether stale historical events are non-actionable under the F1.4 filter.
- Whether the result is a reload-safe candidate for an operator to review.
- A `PASS` / `REVIEW` / `FAIL` recommendation and `next_manual_step`.

This smoke does not reload launchd, does not restart Claw, does not run
`ops/claw-watchdog.sh`, and does not write diagnostics acknowledgements. Its
`not_executed_commands` output lists the launchd commands an operator may run or
roll back manually after reviewing the report.

## Watchdog Current Status

As of the 2026-06-23 operator checks, the watchdog is re-enabled safely and the
latest read-only stale-filter smoke after the browser atomic tools smoke passed
against live code_version `e4a3ee2`.

- Live daemon: startup event `270260`, pid `33828`, code_version `e4a3ee2`
  (`e4a3ee2fd9399b8ff7633cde5be4aafe6ccfd2ca`).
- C4, F0.2d, and #112 browser atomic read-only tools are live in that daemon
  version.
- Portable enable command form:

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.pachano.claw-watchdog.plist"
```

- Portable rollback command form:

```bash
launchctl bootout "gui/$(id -u)/com.pachano.claw-watchdog"
```

- Portable status command form:

```bash
launchctl print "gui/$(id -u)/com.pachano.claw-watchdog"
```

- Post-enable status: loaded LaunchAgent, interval `300s`, last exit code `0`,
  idle between interval runs.
- Latest post-browser-smoke watchdog smoke: `safe_candidate` / `PASS`,
  `database_open_mode=read_only`, `expected_code_version=e4a3ee2`,
  `latest_startup_code_version=e4a3ee2`.
- Post-browser-smoke observe scan checked events after `270334`; RuntimeDb/WAL/
  SQLite/database-lock errors `0`; browser_tools/tool-policy errors `0`;
  stale-event action attempts `0`.

Next recommended check: run a 1h and 24h read-only observe soak. Re-run the
watchdog smoke with `--expected-code-version e4a3ee2`; do not treat this docs
update as a new deploy.

## Restart

Prefer the repo restart wrapper:

```bash
bash scripts/restart.sh
```

For launchd-managed restarts:

```bash
uid="$(id -u)"
launchctl kickstart -k "gui/$uid/com.pachano.claw"
```

Verify after restart:

```bash
bash scripts/diagnose.sh
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

## Local Status Checks

Process:

```bash
pgrep -fl "claw_v2.main"
```

Launchd:

```bash
launchctl list com.pachano.claw
launchctl print "gui/$(id -u)/com.pachano.claw"
launchctl print "gui/$(id -u)/com.claw.chrome-cdp"
```

Port:

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

Recent logs:

```bash
tail -n 200 logs/claw.log
tail -n 200 "$HOME/.claw/claw.stderr.log"
```

## Observe Queries

Recent events:

```bash
sqlite3 data/claw.db \
  "select id,timestamp,event_type,lane,provider,json_extract(payload,'$.error') from observe_stream order by id desc limit 20;"
```

Actionable error events:

```bash
sqlite3 data/claw.db \
  "select timestamp,event_type,payload from observe_stream where event_type in ('scheduled_job_error','daemon_tick_error','llm_circuit_open','nlm_research_failed','nlm_research_degraded','firecrawl_paused') order by id desc limit 20;"
```

Trace replay:

```bash
sqlite3 data/claw.db \
  "select timestamp,event_type,lane,provider,model,payload from observe_stream where trace_id = '<trace_id>' order by id asc;"
```

Job replay:

```bash
sqlite3 data/claw.db \
  "select timestamp,event_type,lane,payload from observe_stream where job_id = '<job_id>' order by id asc;"
```

## Jobs And Task Recovery

Active generic jobs:

```bash
sqlite3 data/claw.db \
  "select job_id,kind,status,attempts,max_attempts,worker_id,datetime(updated_at,'unixepoch'),error from agent_jobs where status in ('queued','running','waiting_approval','retrying') order by updated_at desc;"
```

Active autonomous tasks:

```bash
sqlite3 data/claw.db \
  "select task_id,session_id,runtime,provider,model,status,verification_status,datetime(updated_at,'unixepoch'),error from agent_tasks where status in ('queued','running') order by updated_at desc;"
```

The daemon reconciles stale running tasks into `lost`. To force a recovery path, restart Claw and then inspect:

```bash
bash scripts/restart.sh
bash scripts/diagnose.sh --json
```

From Telegram or web chat, use:

- `/status`
- `/spending`
- `/jobs`
- `/job_status <task_id>`
- `/job_trace <job_id>`
- `/tasks`
- `/task_status`
- `/task_resume <task_id>`
- `/task_cancel <task_id>`
- `/traces [limit]`
- `/trace <trace_id> [limit]`

## Provider Failures

OpenAI/Anthropic provider circuit:

- Event: `llm_circuit_open`
- Follow-up: inspect `payload.reason`, then retry after cooldown or switch lane model via `/model set`.

Codex CLI:

```bash
codex --version
codex login status
```

If auth is broken:

```bash
codex login
```

Auto-research / `perf_optimizer`:

- Event: `auto_research_adapter_error`
- Event: `perf_optimizer_paused`
- `codex_timeout` pauses the auto-research agent instead of repeatedly failing cron.
- Inspect with `/agent_status perf-optimizer`.
- Resume after fixing Codex with `/agent_resume perf-optimizer`.

Firecrawl:

- Event: `firecrawl_paused`
- `insufficient_credits` pauses scraping for 24h.
- `rate_limited` pauses scraping for 1h.

NotebookLM:

- Event: `nlm_research_degraded`
- `fallback_used=true` means local Wiki covered the request.
- `fallback_used=false` means no local source could cover the research.

Linear pipeline polling:

- Event: `pipeline_poll_degraded`
- `timeout` / `rate_limited` / `auth` failures back off polling instead of throwing cron stacktraces.
- Event: `pipeline_poll_skipped` means polling is still inside backoff.
- Inspect active pipeline runs with `/pipeline_status`.

Telegram transport:

- Event: `telegram_transport_stop_error`
- Startup uses explicit HTTP pool and timeout settings:
  `TELEGRAM_CONNECTION_POOL_SIZE`, `TELEGRAM_GET_UPDATES_POOL_SIZE`,
  `TELEGRAM_POOL_TIMEOUT`, `TELEGRAM_REQUEST_TIMEOUT`, `TELEGRAM_MEDIA_WRITE_TIMEOUT`.
- Restart cleanup errors are logged and observed, but shutdown continues through app stop and shutdown.

## Morning Brief

Claw sends one proactive Telegram briefing at the start of the day and one final report at night.

Default:

```bash
MORNING_BRIEF_ENABLED=true
MORNING_BRIEF_HOUR=5
EVENING_BRIEF_ENABLED=true
EVENING_BRIEF_HOUR=21
MORNING_BRIEF_TIMEZONE=America/Chicago
```

Optional enrichments:

```bash
MORNING_BRIEF_LOCATION="City, ST"
MORNING_BRIEF_EMAIL_COMMAND="/path/to/email-digest"
MORNING_BRIEF_CALENDAR_COMMAND="/path/to/calendar-digest"
```

When command overrides are unset, Claw automatically tries macOS Mail and Calendar through `osascript`. If macOS privacy permissions or account setup block access, the brief reports that source as unavailable. Override commands must print a short summary to stdout and finish quickly.

Events:

- `morning_brief_sent`
- `morning_brief_failed`
- `evening_brief_sent`
- `evening_brief_failed`

Duplicate protection lives in `~/.claw/morning_brief_last_sent.txt` and `~/.claw/evening_brief_last_sent.txt`.

## Escalation

Escalate only after local diagnostics confirm the blocker:

- Missing credential or expired external login.
- Launchd cannot start the service.
- SQLite database cannot be opened read-only.
- Port `8765` is held by a non-Claw process.
- Repeated `llm_circuit_open` across all configured providers.
