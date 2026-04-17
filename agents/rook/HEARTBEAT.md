# HEARTBEAT.md — Rook 🏗️

# Cron already handles: health-audit (6h), security-scan (8 AM daily)
# Heartbeat covers: quick pulse checks between audits

## Checks (rotate, don't do all every time)

### Priority 1 — Every heartbeat
- [ ] Gateway responding? (`claw health`)
- [ ] Telegram channel status OK?
- [ ] Any cron jobs in `error` state?

### Priority 2 — Every 2-3 heartbeats
- [ ] API quota usage — any provider approaching limits? (`claw channels list`)
- [ ] Disk space on system drive above 85%?

### Priority 3 — Once per day
- [ ] Review cron job history — jobs stuck in `idle` that should have run?
- [ ] Agent count sanity — orphaned or duplicate agents in ~/.claw/agents/?
- [ ] Review memory/daily file — document any incidents or patterns

## Rules
- If everything green → HEARTBEAT_OK
- Escalate immediately if: gateway down, Telegram disconnected, security finding
- Report format: `[status] component — detail`
- Don't alarm on transient blips — confirm before escalating
- One failed health check = note it. Two consecutive = escalate.
