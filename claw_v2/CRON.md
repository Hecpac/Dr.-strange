# Claw — Scheduled Jobs (Precision Timing)

## Daily
- 08:00 — morning_brief: Telegram briefing with day/date, weather, agenda/email connector summaries, pending tasks/jobs, paused agents, and system alerts
- 03:00 — self_improve: Self-improvement cycle (blocked if eval suite fails)
- 23:00 — daily_metrics: Calculate and store daily claw_score + per-tool metrics

## Morning Brief Configuration
- `MORNING_BRIEF_ENABLED=true|false`
- `MORNING_BRIEF_HOUR=8`
- `MORNING_BRIEF_TIMEZONE=America/Chicago`
- `MORNING_BRIEF_LOCATION="City, ST"` (optional; wttr.in auto-detects by IP when empty)
- `MORNING_BRIEF_EMAIL_COMMAND="..."` (optional local command returning a concise inbox summary)
- `MORNING_BRIEF_CALENDAR_COMMAND="..."` (optional local command returning a concise agenda summary)

## Weekly
- Monday 09:00 — weekly_report: Full SEO audit + metrics + trust level review
- Sunday 22:00 — weekly_eval: Full eval suite run, archive results
