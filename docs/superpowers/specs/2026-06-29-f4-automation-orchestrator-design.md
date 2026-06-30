# F4 Automation Orchestrator - Browser/Computer Recabling Spec

date: 2026-06-29
status: implemented-pending-live-smoke
front: F4 Browser / Computer
author: Codex
implementation:
  workspace: /Users/hector/Projects/Dr.-strange
  implemented_on: 2026-06-29
  live_restart: pending explicit approval
live-daemon-reviewed:
  checkout: /Users/hector/srv/claw-daemon
  branch: main
  commit: 5f97fb9
workspace-spec-path: docs/superpowers/specs/2026-06-29-f4-automation-orchestrator-design.md
related:
  - docs/superpowers/specs/2026-03-28-computer-use-design.md
  - docs/superpowers/specs/2026-04-02-browser-simplification-design.md
  - docs/superpowers/specs/2026-06-14-hermes-browser-tooling-adoption.md
  - docs/superpowers/specs/2026-06-25-f4b-deterministic-delegation-design.md
  - claw_v2/brain.py
  - claw_v2/browser_tools.py
  - claw_v2/computer.py
  - claw_v2/computer_gate.py
  - claw_v2/computer_handler.py
  - claw_v2/task_handler.py

## Decision

Create a single automation boundary for browser and desktop work:
`AutomationRequest -> CapabilityGrant -> AutomationRouter -> Executor -> Verifier`.

The brain still must not drive Chrome/CDP or desktop GUI inline through Bash.
That rule is correct. The failure is that delegated jobs do not carry a
portable permission grant, and the delegated browser path falls into
`browser_use` even for deterministic CDP work.

The permanent fix is not to remove policy. The fix is to make policy explicit,
structured, transferable, and boring:

- The brain creates an `AutomationRequest` with a concrete objective and
  requested capability.
- The coordinator attaches a `CapabilityGrant` that names domains, actions,
  risk tier, time budget, evidence requirements, and whether high-risk browser
  actions such as `evaluate` are allowed.
- The router chooses the smallest executor that can satisfy the request.
- The executor returns a structured result, not a fragile string.
- The verifier maps outcomes into stable user messages.

## Implementation Summary

Implemented in the workspace on 2026-06-29:

- Added subscription-first model roles in `claw_v2/model_registry.py` and moved
  the default `CODEX_MODEL` to `gpt-5.5`.
- Added `AutomationRequest`, `CapabilityGrant`, and `AutomationOutcome` in
  `claw_v2/automation_contracts.py`.
- Added startup health `model_roles` reporting without credentials.
- Routed simple delegated URL opens through deterministic Chrome CDP before
  `browser_use`.
- Honored explicit `NO browser_use` constraints with deterministic output or a
  structured blocker.
- Propagated non-sensitive browser read grants to `browser_use` as
  `allowed_domains`, `allow_high_risk_actions`, and an action allowlist limited
  to `evaluate` and `save_as_pdf`.
- Converted delegated `BrowserUsePolicyInterrupt` into a concrete approval
  message with action/domain, instead of surfacing a raw policy exception.
- Added `automation_outcome_recorded` events for browser executor results while
  preserving legacy `verification_status` compatibility.

Not executed in this workspace run:

- Production daemon restart and authenticated live Chrome/X smoke. That remains
  pending because it mutates the live daemon and requires explicit approval.

## Model Consensus

Use subscription-backed models first. Avoid model API keys for the automation
core unless the user explicitly opts into API billing for a specific fallback.

### OpenAI / Codex

Primary use: deterministic execution workers, computer-use backend, lightweight
automation glue, and code/test work.

Recommended defaults:

- `gpt-5.5`: default for `computer_use` and complex Codex execution. OpenAI's
  Codex models page describes it as the current strongest Codex model for
  complex coding, computer use, knowledge work, and research workflows.
- `gpt-5.4-mini`: default for fast, bounded subagents and low-risk helper jobs.
  OpenAI describes it as the fast, efficient mini model for responsive coding
  tasks and subagents.
- Do not use `gpt-5.3-codex` as a new default. OpenAI marks it deprecated for
  Codex when signing in with ChatGPT.
- Do not make `gpt-5.3-codex-spark` part of the runtime core. It is a Pro
  research-preview model for near-instant coding iteration and is better suited
  to manual experiments.

Local evidence:

- The installed Codex CLI is authenticated with `auth_mode=chatgpt`.
- The local Codex config currently defaults to `gpt-5.3-codex`, but the daemon
  env overrides computer use to `CODEX_MODEL=gpt-5.5`.
- The visible Codex model cache includes `gpt-5.5`, `gpt-5.4-mini`, and older
  Codex models.

Source evidence:

- https://developers.openai.com/codex/models
- https://developers.openai.com/codex/cli

### Anthropic / Claude Code

Primary use: brain, high-context reasoning, and the browser-agent fallback when
deterministic browser tools cannot solve the task.

Recommended defaults:

- Keep brain/worker lanes on the existing Anthropic subscription path.
- Keep `browser_use` on the existing Claude OAuth path, but only as the
  exploratory browser-agent fallback.
- Use `claude-sonnet-4-6` for browser-agent fallback by default because it is
  already wired and used by the current runtime.
- Use higher-capability Opus models only for planning/brain lanes, not for
  every browser action loop.
- Prevent accidental API billing: Claude support documents that
  `ANTHROPIC_API_KEY` makes Claude Code use API authentication instead of the
  Claude subscription. Runtime code that intends subscription usage must avoid
  inheriting that env var where possible.

Local evidence:

- Live observe_stream events show Anthropic lanes using `claude-opus-4-8`,
  `claude-opus-4-7`, and `claude-sonnet-4-6`.
- `browser_use` already builds Claude models through OAuth and the Max/Pro
  subscription path in `claw_v2/computer.py`.

Source evidence:

- https://support.claude.com/en/articles/11145838-use-claude-code-with-your-pro-or-max-plan

### Google / Gemini

Primary use: optional experiment only.

Recommendation:

- Do not put Gemini CLI or Gemini Code Assist in the required automation path.
- Local machine has Gemini OAuth and `gemini` installed, but Google documents
  Gemini Code Assist deprecation for consumer accounts as of June 18, 2026.
- Treat Gemini as an optional future provider behind the same model matrix, not
  as a dependency for this PR.

Source evidence:

- https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals

## Current Runtime Findings

Verified against the live daemon database and code, read-only:

- The daemon is running from `/Users/hector/srv/claw-daemon`, not from the
  current workspace checkout.
- Startup health checks report `chrome_cdp`, `browser_use`, `computer_use`, and
  `codex_cli` as available.
- Inline brain attempts to drive browser/CDP/computer-use are blocked by the
  pre-tool-use hook with reason `inline browser/CDP/computer-use drive in a chat
  turn`.
- Delegation works. `mcp__claw__delegate_task` is declared for brain context and
  recent live turns used it to enqueue jobs.
- Delegated browser tasks can reach Chrome CDP. One X task reached
  `https://x.com/home`, title `Inicio / X`, with visible authenticated feed text.
- Delegated browser tasks then often fail because the path falls into
  `browser_use` and returns `(no result)`.
- Historical delegated `browser_use` jobs also fail when the browser agent
  proposes high-risk actions such as `evaluate` or `save_as_pdf` without a
  grant that permits them.

## Problem

The browser/computer system has the right parts, but they do not compose into a
single, predictable control plane.

Observed failure modes:

- The brain tells the truth that inline browser/computer-use is blocked, but the
  user sees it as "policy" instead of a clear delegation contract.
- Delegated browser jobs do not carry action/domain permissions, so high-risk
  browser-use actions interrupt or fail.
- Simple CDP requests can fall into the autonomous browser-use agent even when
  the objective explicitly says not to use it.
- Some failures are classified by string markers such as `(no result)`.
- Auto-approval does not consistently translate into `allow_high_risk_actions`
  and `approved_domains` for browser-use.
- Model defaults are scattered across env vars, local Codex config, runtime
  config, and hardcoded defaults.

## Target Architecture

### 1. AutomationRequest

Create a small typed request object for all delegated browser/computer work.

Fields:

- `request_id`
- `session_id`
- `task_id`
- `objective`
- `mode`
- `surface`: `browser` | `desktop`
- `intent`: `open_url` | `snapshot` | `extract` | `click` | `form_fill` |
  `auth_check` | `explore` | `computer_app`
- `target_url`
- `target_domains`
- `requested_actions`
- `evidence_required`
- `time_budget_seconds`
- `model_policy`

### 2. CapabilityGrant

Create a portable permission object attached before execution.

Fields:

- `grant_id`
- `scope_domains`
- `allow_read`
- `allow_navigation`
- `allow_click`
- `allow_type`
- `allow_download`
- `allow_upload`
- `allow_evaluate`
- `allow_desktop_mouse`
- `allow_desktop_keyboard`
- `allow_sensitive_domains`
- `expires_at`
- `risk_tier`
- `approved_by`: `system_auto` | `user` | `none`
- `approval_id`
- `reason`

Default policy:

- Read/navigation/screenshot on non-sensitive domains can be auto-granted.
- Click/type on non-sensitive browser pages can be medium risk but bounded by
  grant and evidence requirements.
- Sensitive domains, payments, trading, credentials, posting, destructive
  actions, upload, download, and desktop keyboard on unknown state require user
  approval.
- `evaluate` is allowed for non-sensitive deterministic diagnostics only when
  the grant names the domain and the request is read-only. It remains blocked
  on sensitive domains unless explicitly approved.

### 3. AutomationRouter

Route to the smallest executor:

- `deterministic_browser`: CDP/Playwright-backed, no LLM action loop.
- `browser_agent`: `browser_use`, only for exploratory multi-step web tasks.
- `computer_use`: Codex-backed desktop control for native apps and OS UI.

Routing rules:

- Open URL, snapshot, screenshot, console read, DOM text extraction, auth check,
  and page title verification always use `deterministic_browser`.
- X/Instagram/etc. "open and confirm" tasks use deterministic browser.
- "Review feed" may start deterministic, then use browser agent only if the
  deterministic extractor cannot gather enough content.
- Requests that explicitly say "do not use browser_use" must never route to
  `browser_agent`; if deterministic cannot satisfy them, return a structured
  blocker.
- Desktop app tasks route to `computer_use`, not browser agent.

### 4. Structured Outcomes

Replace policy-looking strings with structured outcomes:

- `passed`
- `needs_approval`
- `denied`
- `blocked_by_login`
- `blocked_by_challenge`
- `runtime_failed`
- `no_result`
- `timed_out`

Each outcome must include:

- `executor`
- `status`
- `reason_code`
- `human_summary`
- `evidence`
- `next_action`
- `approval_request`, when needed

User-facing language must say what happened operationally:

- Good: "CDP opened X and verified login, but JS evaluation on x.com needs an
  explicit grant."
- Bad: "Policy/law does not allow this."

## Checkpoints

### Checkpoint 1 - Model Matrix

Goal:

- Centralize subscription-first model defaults and prevent accidental deprecated
  or API-billed defaults.

Scope:

- Add a model matrix or extend `model_registry.py`/config with named roles:
  `computer_use_primary`, `computer_use_fast`, `brain_primary`,
  `browser_agent_primary`, `browser_agent_fallback`.

Expected defaults:

- `computer_use_primary`: `gpt-5.5`
- `computer_use_fast`: `gpt-5.4-mini`
- `browser_agent_primary`: `claude-sonnet-4-6`
- `brain_primary`: existing Anthropic Opus lane default
- `gemini_optional`: disabled / experimental

Acceptance criteria:

- Config no longer silently prefers deprecated `gpt-5.3-codex` for daemon
  automation.
- Tests prove `CODEX_MODEL` override still wins when explicitly set.
- Startup health emits model role summary without secrets.

### Checkpoint 2 - Request and Grant Types

Goal:

- Introduce typed `AutomationRequest`, `CapabilityGrant`, and
  `AutomationOutcome`.

Scope:

- New module under `claw_v2/automation.py` or `claw_v2/automation_contracts.py`.
- Unit tests only. No executor behavior change yet.

Acceptance criteria:

- Grants encode domain/action/time/evidence permissions.
- Sensitive domains require explicit approval for write/high-risk actions.
- Non-sensitive read/navigation can be system-auto-granted.
- Serialization is stable enough for task ledger artifacts.

### Checkpoint 3 - Deterministic Browser First

Goal:

- Make simple CDP/browser work use atomic browser tools before `browser_use`.

Scope:

- Extend `ComputerHandler.run_delegated_browser_task` routing.
- Reuse `BrowserToolService`/`DevBrowserService` where possible.
- Honor "do not use browser_use" as a hard executor constraint.

Acceptance criteria:

- Open URL + title + URL + screenshot returns without calling browser-use.
- X open/auth-check returns deterministic evidence without browser-use.
- A `NO browser_use` objective returns a deterministic result or structured
  blocker, never a browser-use `(no result)`.

### Checkpoint 4 - Browser Agent Grant Propagation

Goal:

- When browser-use is necessary, pass the grant into
  `allow_high_risk_actions`, `allowed_domains`, and approval handling.

Scope:

- `ComputerHandler._run_browser_use_task`
- `ComputerHandler._run_browser_use_session`
- `ComputerHandler.run_delegated_browser_task`
- `BrowserUsePolicyInterrupt` conversion to `needs_approval`

Acceptance criteria:

- `evaluate` on an explicitly granted non-sensitive domain can run.
- `evaluate` without a matching grant returns `needs_approval`, not failed.
- Sensitive domains still block until user approval.
- Auto-approve sets grant permissions only when the task is non-sensitive.

### Checkpoint 5 - Outcome and User Message Cleanup

Goal:

- Remove vague policy/law language and stop relying on string failure markers as
  the primary result contract.

Scope:

- Browser executor result handling in `task_handler.py`.
- User-facing response mapping.
- Observe events for `automation_outcome_recorded`.

Acceptance criteria:

- `(no result)` is mapped to `no_result` with executor and evidence context.
- Policy/approval blockers map to `needs_approval` with exact action/domain.
- Runtime errors map to `runtime_failed`.
- User sees concrete operational language.

### Checkpoint 6 - Smoke and Production Readiness

Goal:

- Prove the recabling works end-to-end before enabling by default.

Focused gates:

- `.venv/bin/python -m pytest tests/test_computer.py tests/test_computer_gate.py tests/test_browser_tools.py tests/test_task_handler.py tests/test_anthropic_hooks.py -q`
- `.venv/bin/python -m pytest tests/test_workspace.py tests/test_lifecycle.py -q`

Manual/live smoke after approval:

- Restart daemon with `./scripts/restart.sh`.
- Verify `agent_startup_context` in production `data/claw.db`.
- Run deterministic browser smoke: open a non-sensitive URL via CDP and verify
  URL/title/screenshot.
- Run X auth-check smoke: open `https://x.com/home`, verify title/login state,
  no feed extraction unless explicitly requested.
- Run browser-agent approval smoke: force a read-only `evaluate` on a
  non-sensitive test domain with a matching grant.

## Expected Files For PR

Likely production files:

- `claw_v2/model_registry.py`
- `claw_v2/config.py`
- `claw_v2/automation_contracts.py` or `claw_v2/automation.py`
- `claw_v2/computer_gate.py`
- `claw_v2/computer.py`
- `claw_v2/computer_handler.py`
- `claw_v2/task_handler.py`
- `claw_v2/browser_tools.py`
- `claw_v2/brain.py`, only if prompt wording must reflect the new contract
- `claw_v2/INTERNAL_WIRING.md`

Likely tests:

- `tests/test_model_registry.py`
- `tests/test_automation_contracts.py`
- `tests/test_computer.py`
- `tests/test_computer_gate.py`
- `tests/test_browser_tools.py`
- `tests/test_task_handler.py`
- `tests/test_anthropic_hooks.py`

## Out Of Scope

- RuntimeDb schema redesign.
- Formal lease changes.
- Telegram routing rewrites unrelated to browser/computer work.
- Replacing Claude brain lanes.
- Adding Gemini as a required provider.
- Removing approval gates.
- Any production restart before tests and explicit user approval.

## Stop Conditions

- A required fix crosses into F2 RuntimeDb or F3 leases.
- Deterministic browser tools cannot reach Chrome CDP in focused smoke.
- Model docs or local entitlement checks contradict the selected defaults.
- A change would require storing or printing credentials.
- A test requires real authenticated browser state where a fake/unit contract
  can cover the behavior.

## PR Acceptance Criteria

- Simple browser tasks no longer use `browser_use` by default.
- Delegated browser tasks carry an explicit grant.
- High-risk browser-use actions are either allowed by grant or produce
  `needs_approval`.
- The daemon can explain blockers without "law/policy" language unless the
  actual runtime policy is the blocker.
- Model defaults are subscription-first and centrally documented.
- Focused tests pass.
- Post-restart observability proves the live daemon loaded the new wiring.
