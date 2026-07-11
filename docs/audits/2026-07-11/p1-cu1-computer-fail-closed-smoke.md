# P1-CU1 live smoke — explicit `/computer` fail-closed routing

Date: 2026-07-11 (America/Chicago)

## Deployed incarnation

- Commit: `bc7290414fabe7e85d52ea381b5341b156fb405c`
- Production PID before restart: `59034`
- Production PID after `scripts/restart.sh`: `61446`
- `agent_startup_context` event: `850125`
- Observed `code_version`: `bc72904`
- Web health: `status=ok`, port `8765`
- Boot stderr delta: one expected `Claw boot complete` warning; no traceback.
- Stderr lines before the smoke: `321299`
- Stderr lines after the smoke: `321299`

## Exact regression input

```text
/computer Abre Calculator, calcula 17 por 23, deja el resultado visible y toma una captura como evidencia. No cierres otras apps, no guardes archivos, no cambies settings y no hagas ninguna otra acción.
```

Final controlled attempt:

- Turn: `441c8caf26dcf9b0`
- Round-trip inside BotService: `695 ms`
- Telegram total latency: `1584.3 ms`
- `computer_session_started`: event `850844`
- Selected backend: `codex`, event `850845`
- Approval screenshot: event `850846`, `612920` bytes
- Approval created: `25ca8b7661ebbe76`, event `850847`
- Approval outcome: `pending_approval`, `reason_code=approval_required`
- Tools used according to `turn_receipt`: none
- Brain/tool events for the turn: no `brain_turn_start`, no Bash, no Write

Approval response:

- Turn: `a9baa8d09ec54e77`
- Round-trip: `779 ms`
- `approval_approved`: event `850857`
- Resume stopped by the pre-existing screenshot-integrity guard:
  `computer_approval_resume_blocked`, event `850863`,
  `reason=screenshot_changed`
- Telegram delivered the blocker response: event `850875`

## Verdict

**PASS — primary P1-CU1 invariant.** The exact instruction that previously
entered screenshot → Brain → ToolSearch/Bash/Write now enters the dedicated,
capability-gated computer session, selects the `codex` computer backend, and
requests approval without invoking Brain or mutable SDK tools. The original
side-effect bypass and 300-second Brain timeout did not recur.

The Calculator operation itself did **not** complete and no result/screenshot is
claimed. Approval revalidation stopped safely on `screenshot_changed` in three
attempts, including one attempt where no operator command ran between the
request and approval. This is a separate approval-screenshot stability issue,
not evidence that P1-CU1 routing regressed.

## Carriles no probados

- End-to-end Calculator keystrokes after approval: blocked by
  `screenshot_changed`; close with a separate read-only recon of screenshot
  identity/tolerance before changing the approval guard.
- Live screenshot-only `/computer` analysis: not exercised because the active
  Telegram screen contained private sidebar previews; the read-only tool
  allowlist is test-locked locally.
- OpenAI computer backend: production selected `codex`.
- Worker-lane AppleScript behavior: covered by focused tests, not a live worker
  task in this smoke.

No B4.6 work was opened.
