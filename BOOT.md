# BOOT.md

Startup checklist for runtime restarts.

- Load `BOOT_PROTOCOL.md` first; if it cannot be loaded, emit a clear startup context warning.
- Load identity, user profile, persistent memory, dated working notes, session state, lessons, and task_ledger before answering.
- Verify operational configuration from local config/env-derived `AppConfig`; do not assume API vs Pro, model, channel, paths, or permissions.
- Confirm workspace files are present.
- Confirm task/session state is durable before starting new work.
- Do not send outbound messages unless there is an actionable alert.
