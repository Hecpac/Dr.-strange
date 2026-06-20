# F1.2/F1.3 RuntimeDb Gate

## H24 Gate

F1.2/F1.3 became eligible after the valid H24 observation window passed:

- Baseline: 2026-06-19 15:50:09 UTC.
- Verified: 2026-06-20 17:26:55 UTC.
- Production daemon stayed on pid 15601, boot_id 3961df3b04e58224, code_version c232c07.
- launchd runs stayed 56.
- Genuine post-baseline database-is-locked events: 0.
- RuntimeDb/WAL/SQLite failure count: 0.
- Checkout/context/hook drift: 0.
- Full suite did not run against live production.
- Watchdog remained unloaded.
- WAL-heal remained present during H24.
- SQLite synchronous behavior was untouched during H24.

The gate validates F1.1a/F1.1b stability for at least 24 clean hours. It does
not prove a permanent cure and does not authorize deploy, daemon restart,
watchdog re-enable, or any F1.4/F2/F3/F4/F5/F6 work.

## F1.2/F1.3 Scope

This PR retires active WAL-heal from the production RuntimeDb path only:

- RuntimeDb remains the sole production owner of the `claw.db` connection.
- RuntimeDb-backed stores do not register per-store WAL-heal handles.
- RuntimeDb itself does not register a WAL-heal handle.
- Legacy `runtime_db=None` construction keeps WAL-heal behavior for tests and
  back-compat seams.
- `observe.maintenance_vacuum()` keeps its dedicated short-lived connection.
- SQLite `PRAGMA synchronous=FULL` remains unchanged.
- `property_graph` remains dormant in production.
- `buddy.db` remains out of scope.

## Audit Table

| Component | Opens SQLite connection | Registers WAL-heal | Uses RuntimeDb in production | Reconnect/close semantics | synchronous PRAGMA | DB scope | Production path | Intentional exception |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| RuntimeDb | Yes, one `claw.db` connection via `connect_runtime_sqlite` | No | Yes | Owns direct close/reconnect; stores use dynamic handles | FULL via `connect_runtime_sqlite` | runtime DB | Yes | None |
| memory | Legacy only when `runtime_db=None` | Legacy only when `runtime_db=None` | Yes | Delegates through RuntimeDb handle | Does not set directly | runtime DB | Yes | Pending restore check before open |
| observe | Legacy only when `runtime_db=None` | Legacy only when `runtime_db=None` | Yes | Delegates through RuntimeDb handle; spills on shared write error | Does not set directly | runtime DB | Yes | `maintenance_vacuum()` dedicated short-lived connection |
| jobs / job_service | Legacy only when `runtime_db=None` | Legacy only when `runtime_db=None` | Yes | Delegates through RuntimeDb handle | Does not set directly | runtime DB | Yes | None |
| orchestration | Legacy only when `runtime_db=None` | Legacy only when `runtime_db=None` | Yes | Delegates through RuntimeDb handle | Does not set directly | runtime DB | Yes | None |
| task_ledger | Legacy only when `runtime_db=None` | Legacy only when `runtime_db=None` | Yes | Delegates through RuntimeDb handle | Does not set directly | runtime DB | Yes | None |
| capability_grants | Legacy/lazy only when `runtime_db=None`; lazy HeyGen path uses RuntimeDb | Legacy only when `runtime_db=None` | Yes, via tool registry/HeyGen read-only path | Delegates through RuntimeDb handle | Does not set directly | runtime DB | Yes, lazy | None |
| property_graph | Optional legacy/manual construction | Legacy only when `runtime_db=None` | No | RuntimeDb-capable but not production-constructed | Does not set directly | runtime DB when used | No | Dormant guard must fail if production construction appears |
| build_runtime | Opens one RuntimeDb | No | Yes | Injects RuntimeDb into core stores and tool registry | Inherited from RuntimeDb | runtime DB | Yes | None |
| sqlite_runtime / WAL helpers | `connect_runtime_sqlite` opens configured connections | Helpers remain for legacy handles | RuntimeDb path does not register | Legacy heal registry remains for `runtime_db=None` seams | FULL remains explicit | runtime DB helpers | Yes for connect/health; heal inactive in RuntimeDb path | Legacy tests/back-compat only |
