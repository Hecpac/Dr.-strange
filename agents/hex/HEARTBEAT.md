# HEARTBEAT.md — Hex ⚡

# Cron already handles: dev-daily-triage (1d)
# Heartbeat covers: in-session project awareness

## Checks (rotate, don't do all every time)

### Priority 1 — Every heartbeat
- [ ] Any active workspace with uncommitted changes? (`git status` on known repos)
- [ ] Failed tests or broken builds from recent work?

### Priority 2 — Every 2-3 heartbeats
- [ ] Open PRs needing review or merge?
- [ ] Stale branches (>7 days with no commits)?

### Priority 3 — Once per day
- [ ] Dependency security alerts (npm audit / dependabot)?
- [ ] Review memory/daily file — document lessons learned from today's work

## Rules
- If nothing needs attention → HEARTBEAT_OK
- Don't nag about clean repos — only flag actual issues
- Report format: `[repo] issue — suggested action`
