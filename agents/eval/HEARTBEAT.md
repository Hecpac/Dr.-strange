# HEARTBEAT.md — Eval Health Check

## Check Interval
- **Frequency:** On demand (triggered by `pr_ready` or `deploy_complete` bus events)
- **Fallback poll:** Every 6 hours (verify browser tools are functional)

## Health Checks

### Browser Availability
- Verify Playwright or browser_use service is reachable
- Attempt a test navigation to `about:blank`
- If browser unavailable: degrade to API-only evaluation, flag in report

### Screenshot Capability
- Capture a test screenshot and verify file is non-empty
- If screenshots fail: report as degraded, continue with text-only evidence

### Eval History
- Check last 5 eval reports for anomalies (all FAIL or all PASS suggests miscalibration)
- If anomaly detected: flag for Hector's review

## Status Codes
- `healthy` — Browser tools operational, ready to evaluate
- `degraded` — Partial capability (e.g., no screenshots), can still evaluate with limitations
- `offline` — Cannot reach any browser backend, evaluations blocked
