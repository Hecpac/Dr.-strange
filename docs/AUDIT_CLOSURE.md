# Agent Reliability Audit Closure

Date: 2026-04-26
Workspace: `/Users/hector/Projects/Dr.-strange`

## Final State

The agent is operationally healthy.

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

## Close Criteria

The audit can be closed when:

- `bash scripts/diagnose.sh` reports `healthy`.
- `uv run pytest -q` passes.
- `git diff --check` is clean.
- Firecrawl credit pause is acknowledged or resolved.
- `perf_optimizer` no longer creates `scheduled_job_error` on the next cron run, or pauses itself through `auto_research_adapter_error`.
- The audit changes are committed without unrelated prototype/public output.
