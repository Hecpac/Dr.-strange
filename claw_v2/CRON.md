# Claw — Scheduled Jobs (Precision Timing)

## Daily
- 08:00 — morning_brief: Overnight agent results, token spend, claw_score, alerts
- 03:00 — self_improve: Self-improvement cycle (blocked if eval suite fails)
- 23:00 — daily_metrics: Calculate and store daily claw_score + per-tool metrics

## Weekly
- Monday 09:00 — weekly_report: Full SEO audit + metrics + trust level review
- Sunday 22:00 — weekly_eval: Full eval suite run, archive results
