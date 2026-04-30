# Claw Evolution implementation notes

Last revised: 2026-04-30

## P0 claim capture policy

The current implementation records claims from runtime-observed lifecycle facts only.
It does not parse arbitrary LLM prose and does not ask the model to self-report claims.

Automatic claim creation happens in these paths:

- `ToolRegistry.execute(...)`
  - records one verified `fact` claim after a tool handler returns successfully;
  - records one verified `fact` claim when a tool handler raises;
  - evidence ref shape: `tool_call`, `tool_registry.execute:<tool>:<status>`.
- `TaskHandler` autonomous task lifecycle
  - records verified `fact` claims for started, resumed, pending, succeeded, failed,
    cancelled, and stream-interrupted task states;
  - evidence ref shape: `tool_call`, `task_handler:<task_id>:<status>`.

What is not implemented:

- No extraction of claims from every LLM response.
- No NLP/factual-verb heuristic such as detecting "`X is Y`" statements.
- No automatic recording of non-tool conversational assertions.
- No distinction between "important" and "unimportant" LLM statements beyond the
  runtime event sources above.

The policy is intentionally conservative: only claims tied to runtime-observed tool
or task lifecycle events are recorded automatically.

## P0 storage hardening status

P0 storage layer complies with `08-storage-and-redaction.md` as of PR #1.6
(`fix/p0-storage-hardening`; final commit hash recorded in the PR description).

Implemented hardening:

- `append_jsonl` uses a POSIX `flock` sidecar `.lock` file per JSONL target.
- `append_jsonl` calls `fsync` after each write.
- `append_jsonl` rejects JSONL lines over 1 MB before modifying the target file.
- `redact_sensitive(None)` preserves `null` values instead of converting them to
  empty strings.
- Goal contracts include `goal_revision`, starting at 1 and incrementing on
  append-only updates.
- Action events include the active `goal_revision`.
- `goal_updated` writes a typed event and completed goals reject further updates.
- `action_executed` and `action_failed` can reference their `action_proposed`
  origin through `originating_event_id`.
- Startup recovery marks orphaned proposed actions as failed with
  `interrupted_by_restart` and emits an unverified `risk_signal` claim.
- Standalone Telegram bot tokens are redacted with `<REDACTED:telegram_token>`.

Reset performed for this PR:

- Legacy `~/.claw/telemetry/goals.jsonl`, `claims.jsonl`, and `events.jsonl`
  from the pre-hardening observation period were removed because they lacked
  `goal_revision` and recovery-safe event linkage.
