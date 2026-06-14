# Hermes Browser Tooling Adoption - Design Spec

date: 2026-06-14
status: draft
author: Codex
related:
  - docs/superpowers/specs/2026-04-02-browser-simplification-design.md
  - claw_v2/browser.py
  - claw_v2/browser_capability.py
  - claw_v2/browser_profiles.py
  - claw_v2/computer.py
  - claw_v2/computer_handler.py
  - claw_v2/tools.py
hermes_reference:
  repo: https://github.com/NousResearch/hermes-agent
  commit: 4b5ba112adbfdfe588b015b288bc91f873ca602b
  primary_files:
    - tools/browser_tool.py
    - tools/browser_cdp_tool.py
    - tools/browser_dialog_tool.py
    - agent/browser_provider.py
    - agent/browser_registry.py
    - plugins/browser/browser_use/provider.py
    - toolsets.py

## Decision

Adopt Hermes' browser-tooling practice, not Hermes' whole runtime.

Claw should keep its strongest browser asset: local visible Chrome CDP with a
persistent authenticated profile, profile health gates, Telegram/daemon
observability, and approval controls. The change is to put a Hermes-like
tool surface in front of that infrastructure:

- browser actions are first-class semantic tools, not only slash commands,
  shell instructions, or an autonomous `browser_use` task.
- simple browser work is deterministic and stepwise before it is delegated to
  an autonomous agent.
- backend/provider choice is hidden behind a small interface.
- CDP escape hatches are gated by runtime capability, not prompt claims.
- browser output is structured, sanitized, bounded, and auditable.

`browser_use.Agent` remains useful, but it should be a fallback for broad web
objectives, not the default path for basic navigation, clicking, extraction, or
authenticated profile checks.

## Amendment 2026-06-14 — evidence-based design constraints

Added after a live incident review (Telegram capture of `flowing.to` failed on
both inline and delegated paths) plus log forensics on `~/.claw/claw.stderr.log`
and an isolated `browser_use.Agent` test. These are hard constraints that
**elevate atomic-tools-first from a preference to a requirement**, and correct
two misdiagnoses.

### C1. The autonomous loop is rate-limited at the door on Max (primary blocker)

`browser_use.Agent` rides the Anthropic Max subscription (OAuth `claude-sonnet-4-6`,
fallback `claude-haiku-4-5`) — verified in `claw_v2/computer.py:_build_browser_llm`.
It does **not** use a metered key. But the autonomous loop's call pattern (one LLM
call per step, each carrying a screenshot) trips Max rate limits constantly:

- prod log: `ModelRateLimitError (status=429)` ×546, each "switching to fallback LLM".
- isolated test (single tab, trivial `example.com` task, `use_vision=False`,
  `fallback_llm=None`): still hit `ModelRateLimitError` ×2 per run and stalled to a
  120s timeout. Rate-limiting blocks even the trivial case.

This creates a trap: without a fallback the 429 stalls the run; with the haiku
fallback the run hits C2.

### C2. Claude structured output fails browser_use's `AgentOutput` schema

When the loop does run, browser_use 0.11.13's `AgentOutput` pydantic model rejects
Claude's output:

- prod log: `1 validation error for AgentOutput / action Field required` ×114,
  failing **both** sonnet and haiku, retried **6/6 times** → `(no result)`.
- Claude returns `{thinking: ..., <action nested wrong>}`; the schema requires a
  top-level `action`. Upgrading to browser_use 0.13.1 did not fix it (rolled back,
  see `project_x_sweep_openai_quota` memory). `include_tool_call_examples=True` and
  `flash_mode=True` are untested levers, but per C1 they cannot rescue the loop
  while rate-limiting blocks it first. Format tuning is not a priority.

**Conclusion (C1 + C2):** on Max, the autonomous `browser_use.Agent` loop is
inherently unreliable. Atomic deterministic tools (no LLM in the action loop)
bypass both failure modes entirely. `browser_use.Agent` is a **last resort**, only
for genuinely open-ended multi-step web objectives, with throttle/backoff, and
never for anything that has a deterministic equivalent.

### C3. Atomic READ tools must run INLINE, not through delegation

The `flowing.to` capture died because the only two execution paths both failed:
inline browser drive is denied by the brain's 300s-wall rule (`claw_v2/brain.py:248`),
and delegation could not dispatch (`JobService.enqueue` to `agent_jobs` was failing
on a degraded WAL DB — same root as the daemon restart loop). The two safety
mechanisms compose into a total browser outage.

Requirement: fast atomic **read** tools — `BrowserNavigate`, `BrowserSnapshot`,
`BrowserScreenshot`, `BrowserConsoleRead`, `BrowserGetImages` — must be callable
**inline in the brain turn** (deterministic, bounded ~30–60s, well under the 300s
wall), as a carve-out to the "all browser work is delegation" rule. Only mutation,
autonomous, or genuinely multi-step work delegates. The delegation pipeline is a
single point of failure (jobs DB + worker slots + coordinator); atomic reads must
not depend on it.

### C4. Atomic tools must use tested wait strategies

The deterministic capture script hung on `page.goto(..., wait_until="networkidle")`
because live sites keep analytics/websocket connections open so `networkidle` never
fires. Atomic-tool navigation MUST use `domcontentloaded` + best-effort `load`/
`networkidle` with bounded fallbacks — never bare `networkidle`. Encode this in the
backend, not in each caller's ad-hoc script.

### C5. Quota is not a browser/web constraint — do not self-limit

Browser, web search, research, and computer use already ride subscriptions
(Anthropic Max OAuth / ChatGPT Codex). The `insufficient_quota` storm in the log
(×1090) comes solely from `claw_v2/voice.py:transcribe` (Whisper STT, which has a
local fallback) — unrelated to browser/web. The agent must not decline browser work
citing "OpenAI quota"; that belief is stale and now corrected in memory.

## Problem

Claw currently has the right browser primitives but the agent-facing boundary is
fragmented.

- `DevBrowserService` can browse, interact, use Chrome CDP, take screenshots,
  wait for downloads, and use Browserbase.
- `BrowserCapability` and `ManagedChrome` can self-heal local visible Chrome
  on a dedicated port and profile.
- `BrowseHandler` selects public/authenticated browse strategies.
- `BrowserUseService` wraps `browser_use.Agent` for autonomous browser tasks.
- `browser_profiles.py` provides X-first login/challenge checks.
- Browser instructions also live in `SOUL.md` and playbooks.

The result is operationally powerful but not as clean as Hermes:

- The brain does not have a compact, stable set of atomic browser tools similar
  to `browser_navigate`, `browser_snapshot`, `browser_click`, and
  `browser_type`.
- Some browser work is forced through natural language into `browser_use.Agent`,
  which is harder to audit step-by-step.
- CDP, browser_use, `/browse`, `/chrome_*`, `browser_cli`, and profile gates
  are spread across handlers instead of one browser tool contract.
- Prompt docs describe browser behavior that should be enforced by tool schemas,
  capability checks, and code.

## Hermes Practices To Adopt

Verified against Hermes official repo at commit
`4b5ba112adbfdfe588b015b288bc91f873ca602b`.

1. Atomic browser toolset.
   Hermes exposes a browser toolset with navigation, snapshot, click, type,
   scroll, back, press, images, vision, console, CDP, and dialog tools through
   `toolsets.py`.

2. Navigate returns an actionable snapshot.
   `browser_navigate` initializes the session, loads the page, and tries to
   return a compact accessibility snapshot with element refs so the model can
   immediately continue with click/type actions.

3. Ref-based interaction.
   The model clicks/types against refs like `@e5`, not brittle raw selectors.
   The backend owns resolving refs to DOM targets.

4. Provider registry.
   Hermes separates browser tool behavior from backend selection. The registry
   can route cloud mode through Browser Use, Browserbase, or explicit providers,
   while local mode remains available.

5. Capability-gated escape hatches.
   CDP and dialog tools are only available when a CDP endpoint/supervisor is
   available. The agent is not asked to infer that from prose.

6. Browser use is not the only browser layer.
   Hermes' Browser Use provider creates a cloud browser session for the same
   atomic browser tools. It is not equivalent to Claw's current autonomous
   `browser_use.Agent` wrapper.

7. Output budgets and safety.
   Snapshots are capped/summarized, external content is treated as untrusted,
   secrets in URLs are blocked, private/cloud-metadata URLs are guarded in cloud
   contexts, and browser sessions are cleaned up.

## Claw Principles To Preserve

1. Local authenticated Chrome is first-class.
   X, Instagram, ChatGPT, NotebookLM, and other user-authenticated workflows
   should prefer `~/.claw/chrome-profile` via CDP when allowed.

2. No anti-bot evasion.
   Existing profile health gates are correct: if a site shows login or
   verification, report that human state and stop.

3. Approval gates stay stricter than Hermes.
   Browser clicks, typing, dialog responses, and CDP commands can mutate remote
   state. They must go through Claw's risk model, not only tool descriptions.

4. Telegram and daemon observability are part of the product.
   Every browser action should emit concise events with session id, action,
   URL origin, backend, duration, and sanitized outcome.

5. Browser docs belong in playbooks and schemas.
   `SOUL.md` should not be the canonical implementation manual for browser
   behavior.

## Non-goals

- Do not rewrite Claw as Hermes.
- Do not remove `ManagedChrome`, `BrowserCapability`, or profile health gates.
- Do not auto-login, solve CAPTCHAs, or bypass anti-bot challenges.
- Do not migrate to Browser Use cloud sessions in the first PR.
- Do not add a second approval system.
- Do not add browser secrets, cookies, or tokens to memory files.
- Do not make `browser_use.Agent` the primary path for deterministic actions.

## Target Architecture

### New module: `claw_v2/browser_tools.py`

Owns the semantic browser-tool contract.

Core objects:

```python
@dataclass(slots=True)
class BrowserToolResult:
    success: bool
    url: str | None = None
    title: str | None = None
    snapshot: str | None = None
    element_count: int = 0
    screenshot_path: str | None = None
    error: str | None = None
    backend: str = "chrome_cdp"
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class BrowserElementRef:
    ref: str
    label: str
    role: str | None
    selector: str | None
    text: str | None
    href: str | None
    input_type: str | None


class BrowserToolBackend(Protocol):
    def navigate(self, session_id: str, url: str) -> BrowserToolResult: ...
    def snapshot(self, session_id: str, full: bool = False) -> BrowserToolResult: ...
    def click(self, session_id: str, ref: str) -> BrowserToolResult: ...
    def type(self, session_id: str, ref: str, text: str, clear: bool = True) -> BrowserToolResult: ...
    def press(self, session_id: str, key: str, ref: str | None = None) -> BrowserToolResult: ...
    def scroll(self, session_id: str, direction: str, amount: int = 500) -> BrowserToolResult: ...
    def back(self, session_id: str) -> BrowserToolResult: ...
    def console(self, session_id: str, clear: bool = False) -> BrowserToolResult: ...
```

Initial backend:

- `ChromeCdpBrowserBackend`
  - uses `BrowserCapability.ensure_ready()`.
  - uses existing `ManagedChrome` profile.
  - uses Playwright CDP over `DevBrowserService` helpers where practical.
  - produces snapshots and ref maps.

Later backends:

- `BrowserbaseCdpBackend`.
- `AgentBrowserBackend` if we decide to introduce `agent-browser`.
- `BrowserUseCloudProvider` only if cloud browser sessions are needed.
- `BrowserUseAutonomousExecutor` remains separate and is not an atomic backend.

### Session State

Add a small in-memory session store:

```python
@dataclass(slots=True)
class BrowserToolSession:
    session_id: str
    cdp_endpoint: str
    backend: str
    current_url: str | None
    refs: dict[str, BrowserElementRef]
    ref_version: int
    last_used_at: float
```

Rules:

- key by Claw session id or explicit browser session id.
- refresh refs on every `navigate`, `snapshot`, and post-action snapshot.
- refs expire when a new snapshot is captured.
- return `stale_ref` if a caller uses an old ref after ref_version changed.
- do not persist refs to SQLite; they are live DOM handles, not memory.

### Snapshot Contract

The snapshot must include:

- current URL and title.
- compact visible page text.
- interactive element refs, one per line, for example:
  - `@e1 button "Post"`
  - `@e2 textbox "Search"`
  - `@e3 link "Settings" href="/settings"`
- `element_count`.
- optional `truncated` marker.
- optional `pending_dialogs` once dialog support exists.

Implementation can start with DOM enumeration:

- `a[href]`
- `button`
- `input`
- `textarea`
- `select`
- `[role=button]`
- `[role=link]`
- `[contenteditable=true]`
- `[tabindex]:not([tabindex="-1"])`

For each element, store the best resolver available:

- stable CSS selector if possible.
- role/name pair if available.
- text fallback.
- bounding box as last resort.

This does not need to be perfect in PR 1. It must be deterministic, bounded,
and testable.

## ToolRegistry Additions

Register Claw-style tool names, mapped to OpenAI-compatible schemas by the
existing `ToolRegistry`.

### Read/inspect tools

- `BrowserNavigate`
- `BrowserSnapshot`
- `BrowserConsoleRead`
- `BrowserGetImages`
- `BrowserScreenshot`

Tier:

- default `TIER_READ_ONLY`.
- `requires_network=True`.
- `ingests_external_content=True`.
- `sanitize_fields=("snapshot", "content", "console_messages", "js_errors")`.

Note: navigation can trigger server-side state on hostile sites, but this is
the same practical risk class as `WebFetch` for initial adoption. Mutating
browser actions remain higher tier.

### Interaction tools

- `BrowserClick`
- `BrowserType`
- `BrowserPress`
- `BrowserScroll`
- `BrowserBack`

Tier:

- default `TIER_LOCAL_MUTATION`.
- pass through `ActionGate.risk_browser_use_action` or a new
  `risk_browser_action` helper.
- high-risk actions require approval based on current URL origin, action, and
  target ref label.

### Escape hatches

- `BrowserEval`
- `BrowserCdp`
- `BrowserDialog`

Tier:

- default `TIER_REQUIRES_APPROVAL`.
- `BrowserEval` may later allow read-only expressions through a strict
  allowlist, but not in the first pass.
- `BrowserCdp` should be unavailable unless CDP preflight passes.
- `BrowserDialog` should require approval when accepting a prompt/confirm on a
  sensitive domain.

### Agent class visibility

Initial allowed classes:

- researchers: `BrowserNavigate`, `BrowserSnapshot`, `BrowserConsoleRead`,
  `BrowserGetImages`, `BrowserScreenshot`.
- operators/deployers: all browser tools, subject to tier gates.

Do not expose raw `BrowserCdp` to researcher agents.

## Browser Use Repositioning

Current `BrowserUseService` should remain, but its role changes:

1. Deterministic path first:
   - navigate
   - snapshot
   - click/type/press
   - extract visible state
   - profile health gate

2. Autonomous fallback only when:
   - the objective requires multiple unknown web steps, and
   - deterministic tools cannot cheaply decide the next action, and
   - the approval/risk policy allows autonomous work for that domain.

3. Keep current safety improvements:
   - Claude OAuth path for subscription use.
   - fallback LLM.
   - `max_actions_per_step=1`.
   - post-run screenshot artifact.
   - `BrowserUsePolicyInterrupt`.
   - `allowed_domains` and `prohibited_domains`.

4. Add a policy requirement:
   - `browser_use.Agent` must not start for a single explicit URL read, one
     click, one form field, or a screenshot. Those belong to atomic tools.

## Provider Registry, Later

Do not copy Hermes' provider registry in PR 1. Add it only after the local
tool surface exists.

When added, the registry should be small:

```python
class BrowserProvider(Protocol):
    name: str
    display_name: str
    def is_available(self) -> bool: ...
    def create_backend(self, session_id: str) -> BrowserToolBackend: ...
```

Resolution order:

1. explicit config `CLAW_BROWSER_PROVIDER`.
2. local Chrome CDP if available.
3. Browserbase only when explicitly configured.
4. Browser Use cloud only when explicitly configured.
5. no silent paid provider fallback.

This differs from Hermes intentionally. Claw should not accidentally route a
personal authenticated workflow to a remote paid browser.

## Prompt And Docs Hygiene

Move browser implementation details out of persona documents over time.

Keep in `SOUL.md`:

- one capability sentence.
- no step-by-step browser implementation rules.

Move to:

- `claw_v2/playbooks/browser_cdp.md`
- tool schemas in `claw_v2/tools.py`
- `docs/OPERATIONS_RUNBOOK.md`
- this spec and follow-up implementation plans.

The rule is: if the model must know exact parameters, refs, or gates, encode it
in tool schemas and code, not prompt prose.

## Migration Plan

### PR 1 - Browser tool service and snapshot refs

Objective:

Create `claw_v2/browser_tools.py` with a local Chrome CDP backend and tests.
Do not register tools yet.

Implementation:

- Add `BrowserToolResult`, `BrowserElementRef`, `BrowserToolSession`.
- Add `ChromeCdpBrowserBackend`.
- Reuse `BrowserCapability.ensure_ready()`.
- Implement `navigate`, `snapshot`, `click`, `type`, `press`, `scroll`, `back`.
- Implement compact snapshot and ref generation.
- Emit observe events:
  - `browser_tool_action_started`
  - `browser_tool_action_completed`
  - `browser_tool_action_failed`
- Redact URLs and text snippets before emitting.

Tests:

- fake backend/session tests for ref lifecycle.
- snapshot generation caps output and preserves refs.
- stale refs fail clearly.
- login/challenge text does not produce success claims.
- existing focused suite still passes:
  - `tests/test_browser.py`
  - `tests/test_browser_capability.py`
  - `tests/test_browser_profiles.py`
  - `tests/test_chrome.py`

Acceptance:

- `BrowserToolService.navigate()` returns URL, title, compact snapshot, and
  refs without using `browser_use.Agent`.
- no daemon startup side effects unless the service is called.

### PR 2 - Register atomic browser tools

Objective:

Expose the new service through `ToolRegistry`.

Implementation:

- Add default tool names to `DEFAULT_TOOL_AGENT_CLASSES`.
- Register `ToolDefinition`s with strict schemas.
- Add sanitizer fields for browser output.
- Add tier and success contracts for tier 2/3 browser tools.
- Add capability checks so CDP tools degrade when Chrome CDP is disabled.

Tests:

- `openai_tool_schemas()` includes browser tools for allowed classes.
- researcher cannot use `BrowserClick`, `BrowserType`, `BrowserEval`, or
  `BrowserCdp`.
- operator can use interaction tools subject to tier gates.
- external browser output is sanitized when malicious patterns appear.
- tier 2/3 tools do not emit contract warnings.

Acceptance:

- The brain can call atomic browser tools directly.
- raw CDP is not visible to researcher agents.

### PR 3 - Migrate handlers to the service

Objective:

Make `/browse`, `/chrome_pages`, `/chrome_browse`, `/chrome_shot`,
`browser_cli`, and deterministic social tasks use the same browser tool
service where possible.

Implementation:

- Keep command names and user-facing behavior stable.
- Replace duplicated CDP navigation/screenshot logic with service calls.
- Keep `BrowseHandler` public/authenticated strategy decisions.
- Keep `browser_profiles.py` health gate before X/Instagram autonomous work.
- Preserve Jina/text fallback for public reading.

Tests:

- existing Telegram imperative router tests still pass.
- `/chrome_browse` uses the configured CDP endpoint.
- authenticated URL still prefers local CDP.
- public URL still prefers Jina/text path when appropriate.

Acceptance:

- no loss of current slash-command behavior.
- less duplicated CDP wiring.

### PR 4 - Demote autonomous browser_use to fallback

Objective:

Ensure `browser_use.Agent` is only used after deterministic browser tools are
insufficient.

Implementation:

- Add a `BrowserExecutionPolicy` decision function:
  - `atomic_only`
  - `deterministic_social`
  - `autonomous_browser_use`
  - `blocked_needs_human`
- Route simple URL opens, snapshots, clicks, field fills, and screenshots to
  atomic tools.
- Route X/Instagram known opens through deterministic CDP first.
- Route broad objectives to `BrowserUseService` only after capability/profile
  checks.
- Emit `browser_execution_policy_decided`.

Tests:

- single URL objective does not instantiate `BrowserUseService`.
- one-click objective uses `BrowserClick`.
- broad multi-step objective can still call `BrowserUseService`.
- sensitive high-risk browser_use action still interrupts for approval.

Acceptance:

- `browser_use.Agent` is not the default browser executor.
- current delegated browser tasks still work.

### PR 5 - CDP escape hatches and dialogs

Objective:

Add explicit raw CDP and dialog handling after the safe atomic surface exists.

Implementation:

- Add `BrowserCdp` with Tier 3 approval.
- Add `BrowserEval` as separate from console read.
- Add `BrowserDialog`.
- Add pending-dialog reporting to snapshot when feasible.
- Gate all three tools on CDP capability.

Tests:

- tools are unavailable when CDP is disabled.
- `BrowserCdp` requires approval.
- `BrowserDialog` requires a pending dialog or returns clear failure.
- no tool hangs indefinitely on a JS dialog.

Acceptance:

- CDP escape hatches exist but cannot silently bypass policy.

### PR 6 - Optional provider registry

Objective:

Introduce backend/provider abstraction only after local CDP tooling is stable.

Implementation:

- Add provider protocol and resolver.
- Start with local Chrome CDP provider only.
- Add Browserbase explicit provider if still needed.
- Do not auto-select Browser Use cloud from environment alone.

Tests:

- explicit provider wins.
- unknown provider reports clear configuration error.
- no paid/cloud provider is selected silently.

Acceptance:

- provider abstraction exists without changing default local behavior.

### PR 7 - Prompt cleanup

Objective:

Move browser operation instructions out of persona files and into schemas,
playbooks, and runbooks.

Implementation:

- Slim `SOUL.md` browser bullets.
- Update `claw_v2/playbooks/browser_cdp.md`.
- Update `docs/OPERATIONS_RUNBOOK.md`.
- Add a browser tool quick reference.

Tests:

- focused startup/context tests still pass:
  - `tests/test_workspace.py`
  - `tests/test_lifecycle.py`
- no test asserts old browser prose in `SOUL.md`.

Acceptance:

- prompt context states capability, code enforces behavior.

## Success Conditions

The work is successful when:

- the brain has first-class atomic browser tools.
- navigating a page returns a compact snapshot with refs.
- click/type/press use refs, not raw selectors from the model.
- simple browser tasks do not instantiate `browser_use.Agent`.
- authenticated browser tasks still use local Chrome CDP and profile gates.
- logged-out/challenge states are reported clearly and not treated as success.
- high-risk browser actions require approval.
- raw CDP requires approval and runtime CDP availability.
- browser output is sanitized as external content.
- existing browser, Chrome, computer gate, lifecycle, and Telegram router tests
  pass.

## Verification Matrix

Focused local tests after each PR:

```bash
.venv/bin/python -m pytest \
  tests/test_browser.py \
  tests/test_browser_capability.py \
  tests/test_browser_profiles.py \
  tests/test_chrome.py \
  tests/test_computer_gate.py \
  -q
```

Additional tests once ToolRegistry registration lands:

```bash
.venv/bin/python -m pytest \
  tests/test_computer.py \
  tests/test_computer_diagnostics.py \
  tests/test_telegram_imperative_router.py \
  tests/test_lifecycle.py \
  -q
```

Manual smoke after implementation:

1. Start/restart daemon.
2. Confirm startup health reports Chrome CDP state.
3. Run `/chrome_pages`.
4. Run `/chrome_browse https://example.com`.
5. Run one brain task that uses `BrowserNavigate` and `BrowserSnapshot`.
6. Run one X/Instagram profile-gated task while logged in.
7. Run the same profile-gated task while logged out or behind challenge and
   verify it stops with a human state.
8. Confirm observe stream includes browser action events.

## Open Questions

1. Should `BrowserNavigate` be Tier 1 or Tier 2 for authenticated domains?
   Recommended: Tier 1 for initial public navigation, automatic elevation when
   current/target URL matches `sensitive_urls`.

2. Should snapshots use Playwright DOM enumeration first or CDP
   `Accessibility.getFullAXTree` first?
   Recommended: DOM enumeration first for speed/testability, CDP AX tree later
   if refs are weak.

3. Should Claw adopt `agent-browser`?
   Recommended: no for the first implementation. Existing Playwright CDP is
   enough, and adding a Node browser daemon would expand the runtime surface.

4. Should Browser Use cloud be added?
   Recommended: only if there is a real use case for remote disposable browsers.
   It should never become the default for authenticated personal sessions.

## Current Baseline Verification

This spec was written after the following focused suite passed locally:

```bash
.venv/bin/python -m pytest \
  tests/test_browser.py \
  tests/test_browser_capability.py \
  tests/test_browser_profiles.py \
  tests/test_chrome.py \
  tests/test_computer_gate.py \
  -q
```

Result: 125 passed.
