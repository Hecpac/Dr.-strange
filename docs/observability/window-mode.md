# Observation Window Mode

PR #1.5 adds a reinforced observation window before the next Evolution Plan phase. The intent is to reduce execution surface, expose live telemetry, and provide manual and automatic brakes without changing P0 storage formats.

## Activate

Set these environment values before starting the daemon:

```bash
export CLAW_BUDGET_CAP_DAILY=<50-percent-of-current-daily-cap>
export CLAW_TIER_AUTOEXEC_MAX=tier_2
export CLAW_AUTONOMOUS_MAINTENANCE=0
export CLAW_OBSERVABILITY_TELEGRAM_CHAT_ID=<telegram-chat-id>
```

Move high-risk outbound credentials out of the active environment during the window:

```bash
grep -E '^(RESEND_API_KEY|GITHUB_PAT)=' .env >> .env.disabled
grep -vE '^(RESEND_API_KEY|GITHUB_PAT)=' .env > .env.window
mv .env.window .env
```

Restart Claw after changing the environment.

## Deactivate

Restore the normal environment and restart:

```bash
unset CLAW_BUDGET_CAP_DAILY
unset CLAW_TIER_AUTOEXEC_MAX
unset CLAW_AUTONOMOUS_MAINTENANCE
unset CLAW_OBSERVABILITY_TELEGRAM_CHAT_ID
```

Move `RESEND_API_KEY` and `GITHUB_PAT` back from `.env.disabled` only after the observation window closes.

## Manual Kill Switches

Telegram:

```text
/freeze
/unfreeze
/budget_status
```

Dashboard:

```text
http://127.0.0.1:8765/observability
```

`/freeze` pauses autoexec by blocking tool dispatch. Chat remains available, but tool calls fail fast until `/unfreeze`.

## Automatic Circuit Breakers

The runtime freezes automatically and emits `circuit_breaker_tripped` when either threshold is exceeded:

- Rolling LLM cost per hour: `$1.50`
- Tool calls per minute: `10`

Hard-denylisted tool attempts are blocked, logged, and notified:

- `git push --force`
- `rm -rf` with dynamic arguments
- `vercel --prod`
- `gh release create`

## Live Stream

Set `CLAW_OBSERVABILITY_TELEGRAM_CHAT_ID` to send Tier-2+ tool completions and degraded LLM events to the observability chat:

```text
[HH:MM] tool=X tier=Y actor=Z cost=$N status=ok/fail
```
