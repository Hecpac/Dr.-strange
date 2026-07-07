# ADR CB0 — Computer-vs-browser routing: does deep routing need a new lane?

- **Status:** Proposed (evidence gate — decision for Hector)
- **Date:** 2026-07-07
- **Context slice:** CB0 (autonomous, frozen spec). Docs/tests/invariants only —
  no lane built, no browser-direct atomics, no BotService strangler, no live
  browser/CDP smoke.
- **Recommendation:** **NO-GO (defer)** on building a `codex-desktop` delegated
  lane now. Ship the cheap prompt/routing honesty fix (CB1) instead, and define
  the telemetry trigger that would flip this to GO.

---

## Question

Does deep computer-vs-browser routing need a **new delegated lane**
(`codex-desktop` worker), or only **tighter delegation prompts** now that F4-B2
(automatic re-prompt) is deployed?

## Evidence

### 1. The routing graph today (code)

```
inbound message → handle_text → 15 pre-brain dispatchers
   ├─ "/computer …" / desktop request → NO dispatcher captures it
   │        → falls through to the brain (dispatch_decision: no_handlers)
   │        → brain runs computer-use INLINE via mcp__computer-use__*  (300s wall)
   │
   ├─ browser request → delegate_task → TaskHandler
   │        → _should_use_browser_executor(mode, objective) == True
   │        → ComputerHandler.run_delegated_browser_task  (in-process browser executor)
   │        → delegated/deterministic_browser_task → completed   ✅ has a home
   │
   └─ delegated "use the desktop" objective (no browser signal)
            → _should_use_browser_executor(...) == False
            → Codex coordinator (--sandbox workspace-write: no network, no GUI)
            → ✗ cannot drive the desktop
```

Anchors (verbatim, `~/srv/claw-daemon` @ `6f2210d`):

- `claw_v2/brain.py:320` **instructs** the brain: *"Running a Bash script that
  itself drives Chrome/CDP or computer-use is NOT inline work — that is
  delegation."*
- `claw_v2/bot_helpers.py:2314` `_should_use_browser_executor(mode, objective)`
  → routes to the browser executor only when the objective signals
  browser/CDP/X/social work; otherwise `None` → Codex coordinator.
- `claw_v2/bot.py:1295`
  `self._task_handler.browser_executor = self._computer_handler.run_delegated_browser_task`
  — a delegated **browser** runner exists.
- `claw_v2/computer_handler.py:255,1325` `ComputerHandler.run_delegated_browser_task`
  exists; **there is no `run_delegated_computer_task`.**

**The gap, named precisely:** the prompt tells the brain to *delegate*
computer-use, but there is **no delegation destination that can execute it**. A
delegated desktop objective lands in the Codex coordinator (sandboxed, no GUI)
or — if it happens to mention browser words — the browser executor (wrong
tool). It is BOTH a missing lane AND a prompt that routes to nowhere.

### 2. What the telemetry actually shows (redacted corpus)

`evidence-corpus.json` (window 2026-07-06 17:10 → 2026-07-07 15:46, 51k events;
structural-allowlist projection, no identifiers/user-text):

- **Computer-use has no delegation pipeline** — zero `delegated_computer_*` /
  `deterministic_computer_*` / `computer_executor_*` events exist. Only inline
  events fire: `computer_session_started`, `computer_backend_selected`
  (backend=codex), `computer_screenshot_captured` (×3), `computer_approval_*`.
- **Browser has the full pipeline**: `brain_delegation_requested` →
  `browser_executor_started` → `browser_capability_preflight_ok` →
  `delegated`/`deterministic_browser_task` → `completed`.
- **Computer-use worked inline** in the observed cases. The only computer
  failures were **not lane-related**:
  - `computer_browser_use_missing_domain_grant` — the browser_use backend needs
    an approved domain to scope to (a grant/UX issue).
  - `computer_approval_resume_blocked` (`reason: screenshot_changed`) — an
    approval-resume guard firing because the screen changed (arguably working as
    designed).
- The 4 `brain_tooluse_ledger_blocked_unverified_action` events were all from
  the `web-entrevista-diag` / `web-smoke-f4b2` diagnostic sessions — **none from
  computer-use**.
- `f4b2_auto_reprompt_*`: 0 organic events yet (deployed today 10:14).

**Caveat (in the corpus, kept honest):** LOW N — computer-use was exercised a
handful of times, mostly test-driven. This is not a statistical sample.

## Decision

**NO-GO (defer) on the `codex-desktop` lane.** Rationale:

1. **No observed failure is attributable to the missing lane.** Computer-use
   ran inline and worked; its failures were domain-grant and approval-resume —
   a new lane fixes neither.
2. **Low N.** Committing to a large architectural build (a worker that can drive
   `mcp__computer-use__*` under approval, off the brain's 300s turn) on a
   handful of test-driven invocations is premature — the spec itself flags it as
   "architectural, not a bug fix."
3. **The real defect is cheap and latent, not the lane.** The prompt/routing
   mismatch (brain told to delegate computer-use → silently lands nowhere) is a
   small honesty fix, not a lane.
4. **F4-B2 already covers narration-without-execution.** If computer-use starts
   narrating-without-acting, F4-B2 re-prompts. (Known limitation: with no
   delegation home, the re-prompt can only push toward the inline path — which
   is exactly why CB1 below matters.)

## Consequences

- **Positive:** no speculative lane; the current inline computer-use path (which
  works for short desktop tasks) is untouched; the routing asymmetry and the
  corpus redaction are now test-locked, so the premise can't drift silently.
- **Negative / accepted:** a *long* (>300s) or *delegated* desktop task still has
  no home. Today that is latent (users run `/computer` inline). We accept it
  until the trigger below fires.

## GO trigger (the evidence that flips this to build the lane)

Reconsider when telemetry shows a **measurable, non-test pattern** of
computer-use failing for **lane** reasons, e.g. any of:

- ≥5 computer-use turns in a rolling 7-day window hitting the brain's 300s wall
  inline (a `computer_*` turn that times out), OR
- ≥5 delegated desktop objectives landing in the Codex coordinator and failing
  for "no GUI/network" reasons, OR
- F4-B2 re-prompt loops observed on computer-use narration
  (`f4b2_auto_reprompt_issued` with a `computer_*` turn and no execution).

Until such a signal exists, the lane is speculative.

## Next slice

- **If NO-GO (recommended) → CB1:** fix the prompt/routing honesty. Either stop
  instructing the brain to "delegate computer-use" (there is no home) and steer
  it to run `/computer` inline for short desktop tasks + report a concrete
  blocker for long ones; or add a routing guard that detects a delegated
  *desktop* objective and returns a clear "no computer-use lane" blocker instead
  of silently landing it in the coordinator. Small docs/prompt/guard change,
  test-locked. Separately, address `missing_domain_grant` UX (the real observed
  friction).
- **If GO → CB2 (design spike, not implementation):** a design gate for the
  `codex-desktop` worker (drives `mcp__computer-use__*` under the approval gate,
  off the brain turn), preserving triple-AND + the max-1 F4-B2 re-prompt. Still
  an evidence/design gate before any lane code.

## Invariants locked by this slice

- `cb0_computer_use_has_no_delegation_home` — `tests/test_cb0_routing_matrix.py`
  (browser is delegable; computer-use is inline-only — the asymmetry the ADR
  rests on).
- `cb0_evidence_corpus_is_redacted` — `tests/test_cb0_corpus_privacy.py` (the
  ADR evidence carries only allowlisted structural routing keys; no session/turn/
  approval/task ids, hashes, or message text).
