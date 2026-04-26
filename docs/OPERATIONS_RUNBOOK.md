# Claw Operations Runbook

This runbook covers the local production contract for Claw on Hector's Mac.

## Runtime Contract

- Launchd label: `com.pachano.claw`
- Entrypoint: `ops/claw-launcher.sh`
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

Claw sends one proactive Telegram briefing each morning during the configured hour.

Default:

```bash
MORNING_BRIEF_ENABLED=true
MORNING_BRIEF_HOUR=8
MORNING_BRIEF_TIMEZONE=America/Chicago
```

Optional enrichments:

```bash
MORNING_BRIEF_LOCATION="City, ST"
MORNING_BRIEF_EMAIL_COMMAND="/path/to/email-digest"
MORNING_BRIEF_CALENDAR_COMMAND="/path/to/calendar-digest"
```

The email/calendar commands must print a short summary to stdout and finish quickly. When unset, the briefing explicitly reports those connectors as unconfigured instead of pretending it checked them.

Events:

- `morning_brief_sent`
- `morning_brief_failed`

Duplicate protection lives in `~/.claw/morning_brief_last_sent.txt`.

## Escalation

Escalate only after local diagnostics confirm the blocker:

- Missing credential or expired external login.
- Launchd cannot start the service.
- SQLite database cannot be opened read-only.
- Port `8765` is held by a non-Claw process.
- Repeated `llm_circuit_open` across all configured providers.
