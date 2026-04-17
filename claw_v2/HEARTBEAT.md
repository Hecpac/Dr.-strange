# Claw — Heartbeat Checklist (Awareness Only)

## Always Run (every heartbeat)
- [ ] System health: disk > 85% alert, RAM > 90% alert, Claude CLI responds
- [ ] Agent watchdog: if any agent running > 2x expected duration, kill and alert
- [ ] Budget watchdog: alert if any agent >80% daily budget

## Business Hours (9am-10pm)
- [ ] Check GSC for pachanodesign.com — alert if impressions drop >10% vs 7-day avg
- [ ] Check GSC for tcinsurancetx.com — alert if any page deindexed
- [ ] Check if any scheduled cron job was missed
