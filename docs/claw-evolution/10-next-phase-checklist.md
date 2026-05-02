# Next phase checklist

Target resume date: Monday 2026-05-04

## Current state

- P0 telemetry is wired in observability-only mode.
- `GoalContract`, `EvidenceLedger`, and `TypedActionEvent` JSONL writers are available.
- `TaskHandler` and `ToolRegistry` emit P0 records to `config.telemetry_root`.
- Evidence Ledger claims are mirrored into `claim_recorded` action events with
  `claims[]` and `evidence_refs[]` populated.
- Critic enforcement is intentionally not connected.

## Required checks before Step 4

- Locate the active runtime `config.telemetry_root`.
- Confirm these files are present and growing under real traffic:
  - `goals.jsonl`
  - `claims.jsonl`
  - `events.jsonl`
- Validate every sampled line parses as JSON.
- Confirm event records include stable IDs:
  - `goal_id`
  - `event_id`
  - `originating_event_id` for `action_executed` / `action_failed`
  - `session_id` when available
- Confirm `claim_recorded` events include:
  - the recorded `claim_id` in `claims[]`
  - evidence references when evidence exists on the claim
- Confirm autonomous task records cover started, resumed, pending, completed, failed, cancelled, and interrupted states when those states occur.
- Confirm tool records cover proposed, executed, and failed actions.
- Confirm redaction removes field-name fragments such as `token`, `secret`, `api_key`, `password`, and `credential`.
- Confirm the telemetry layer does not break Telegram sends, approval policy, task resume, or tool execution.

## Shadow-mode Critic entry criteria

- At least 30-50 real tool/task actions are represented in telemetry, or two days of normal daily use have elapsed.
- No malformed JSONL lines are found in sampled telemetry.
- No secrets appear in sampled telemetry.
- P0 event shape is sufficient for the Critic to evaluate actions without inventing missing context.
- Runtime behavior remains stable with P0 enabled.

## Next implementation phase

1. Wire Critic as shadow mode only.
2. Write Critic observations to telemetry without blocking actions.
3. Compare Critic output against real action outcomes.
4. Add tests proving Critic shadow mode cannot block or mutate runtime decisions.
5. Promote to operational gating only after shadow-mode review is stable.
