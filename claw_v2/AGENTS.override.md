# claw_v2/AGENTS.override.md - Browser / Computer Runtime Contract

This override applies to Codex work under `claw_v2/`.

It specializes the repository-level `AGENTS.md` for Browser Use, Computer Use,
browser tooling, delegated browser execution, and related runtime orchestration.

Do not treat this file as production agent persona. Codex is the engineering
agent working on the codebase.

## Local Scope

This directory contains the production runtime surface for browser and computer
automation.

Primary implementation files include:

- `computer.py`
- `computer_handler.py`
- `browser.py`
- `browser_tools.py`
- `browser_capability.py`
- `computer_gate.py`
- `browser_cli.py`
- `tools.py`
- `task_handler.py`
- `bot.py`
- `main.py`
- `lifecycle.py`
- `adapters/anthropic_hooks.py`

`claw_v2/AGENTS.md` may exist as an auto-updated registry. Do not edit it unless
Hector explicitly asks. This override is the local operating contract.

## Core Rule

A prior browser/computer failure is evidence, not a terminal instruction.

Do not encode failure as agent identity, durable self-belief, or a reason to
avoid the tool forever. Classify the failure, change strategy, preserve evidence,
and stop only on a defined retry budget, safety gate, missing permission,
external blocker, or explicit user stop condition.

## Required Diagnostic Procedure

For any Browser Use / Computer Use bug, inspect before patching:

1. Entry point:
   - runtime construction;
   - lifecycle/CDP wiring;
   - bot orchestration;
   - task delegation;
   - tool registry;
   - CLI wrapper.

2. State read before action selection:
   - `ComputerSession`;
   - `ComputerHandler._sessions`;
   - `session.pending_action`;
   - `session_state`;
   - task ledger;
   - tool result history;
   - previous browser/computer observation.

3. State written after failure:
   - session checkpoint;
   - pending action;
   - previous response id;
   - screenshot or observation hashes;
   - task ledger blocker;
   - emitted diagnostic event;
   - memory/facts/lessons, if any.

4. Retry scope:
   - tool-call local;
   - browser session;
   - computer session;
   - task id;
   - Telegram/user session;
   - durable/global memory.

5. Recovery behavior:
   - the next attempt must change at least one variable:
     tool, selector, URL, wait condition, timeout, observation, prompt,
     approval path, adapter, or recovery branch.

Do not patch by intuition. First identify the call path and state boundary.

## Known High-Risk Areas

Inspect these patterns carefully:

- Browser/computer loops that repeat the same action.
- Retry logic that does not change strategy.
- Pending actions that leak across sessions or tasks.
- `session_state` contamination from stale failures.
- Task ledger blockers that are read as permanent tool incapability.
- Delegated browser tasks that write incomplete checkpoints.
- CDP/browser refs that become stale without recovery.
- Computer-use screenshot loops with no progress detection.
- Brain-lane Bash attempts to bypass browser/CDP/computer-use safety hooks.
- Production daemon behavior diverging from local tests.

## Failure Classification

Every browser/computer failure should be classified as one of:

- transient browser/computer issue;
- selector/DOM issue;
- timeout/wait issue;
- navigation issue;
- auth/permission issue;
- approval/safety gate;
- task ambiguity;
- external service issue;
- stale session/reference issue;
- code bug.

If classification is uncertain, preserve the raw observation and state the
uncertainty. Do not collapse unknown failures into "tool cannot do this."

## Retry Contract

A retry must have:

- max attempt budget;
- structured failure reason;
- changed tactic;
- observable result;
- terminal blocker if exhausted.

Do not:

- increase retry count without changing strategy;
- retry the same selector/action blindly;
- hide failed attempts behind silent fallback;
- convert one failed attempt into durable memory;
- write defeatist conclusions into facts, lessons, prompts, or session state.

Good retry examples:

- change selector strategy;
- reload or reattach browser session;
- wait for a more specific condition;
- navigate through a different URL;
- switch from atomic browser tool to delegated browser execution, or the reverse,
  when appropriate;
- request approval only when a real safety boundary requires it;
- stop with a concrete blocker and evidence.

## Observability Requirements

Preserve or add structured observability when changing behavior.

A browser/computer failure event should include, when available:

- task id;
- session id;
- user/chat id if already available and safe;
- attempt number;
- action attempted;
- tool or adapter used;
- observed error;
- failure class;
- recovery decision;
- next action;
- terminal blocker if any.

Do not log secrets, tokens, cookies, passwords, raw credentials, or full private
page content.

Preserve existing events such as browser tool failures, delegated browser
outcome events, computer/session timeout diagnostics, task ledger writes, and SDK
post-tool-use failure diagnostics.

## Change Rules

When modifying Browser Use / Computer Use behavior:

- Prefer state-machine fixes over prompt-only changes.
- Do not solve timeout, selector, routing, retry, persistence, or stale-state
  bugs with motivational prompt text.
- Keep changes small and isolated to F4 Browser unless Hector explicitly
  approves broader scope.
- If touching `bot.py` or `task_handler.py`, verify routing and task ledger side
  effects.
- If touching `lifecycle.py` or `main.py`, verify production/local daemon
  wiring assumptions.
- If touching `adapters/anthropic_hooks.py`, preserve browser/CDP/computer-use
  safety backstops.
- If touching `browser_tools.py`, preserve stale-reference detection and action
  failure events.
- If touching `computer.py`, preserve `ComputerSession` invariants.
- If touching `computer_handler.py`, preserve session scoping and approval
  boundaries.

## Testing Guidance

Prefer focused tests over broad suites.

Relevant focused tests include:

- `tests/test_browser_tools.py`
- `tests/test_browser.py`
- `tests/test_browser_capability.py`
- `tests/test_tools.py`
- `tests/test_computer.py`
- `tests/test_computer_gate.py`
- `tests/test_computer_handler_scope_leak.py`
- `tests/test_computer_diagnostics.py`
- `tests/test_telegram_imperative_router.py`
- `tests/test_brain_tooluse_ledger.py`
- `tests/test_brain_tooluse_verify.py`
- `tests/test_anthropic_hooks.py`

Suggested focused commands:

```bash
.venv/bin/python -m pytest tests/test_browser_tools.py tests/test_browser.py tests/test_browser_capability.py -q
```
