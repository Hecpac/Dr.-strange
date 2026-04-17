# HEARTBEAT.md — Lux 🎯

# Cron already handles: content-radar (12h), orchestrator (2h), SEO (weekly), newsletter (weekly)
# Heartbeat covers: real-time awareness between automated runs

## Checks (rotate, don't do all every time)

### Priority 1 — Every heartbeat
- [ ] Any drafts pending Hector's review or approval?
- [ ] Did the last orchestrator/cron run produce errors?

### Priority 2 — Every 2-3 heartbeats
- [ ] Engagement on recently published content (unusual spikes or drops)?
- [ ] Social mentions or replies that need a response?

### Priority 3 — Once per day
- [ ] Trending topics in AI/LLM space relevant to content calendar?
- [ ] Review memory/daily file — capture marketing insights worth keeping

## Rules
- If nothing needs attention → HEARTBEAT_OK
- Don't repeat what the orchestrator already reported
- Flag opportunities, not just problems (viral moment, trending keyword, etc.)
- Keep reports scannable: bullet points, no paragraphs
