# Computer Autonomy — Design Spec

**Date:** 2026-03-28
**Status:** Approved (revised)
**Branch:** feat/pending-items

## Goal

Give the bot controlled autonomy to browse authenticated sites and control the user's Mac. Two tiers: Browser CDP for web (90% of use cases), Computer Use for desktop apps (10%). Reads should be easy; writes must be explicit and approval-gated.

## Two-Tier Architecture

| Tier | When | How | Speed/Cost |
|------|------|-----|------------|
| **Browser CDP** | Any website (Google Ads, Polymarket, dashboards) | Playwright `connect_over_cdp()` to user's Chrome | Fast, cheap (no screenshots to API) |
| **Computer Use** | Desktop apps, multi-app flows (Figma, Finder, etc.) | Anthropic API + `screencapture` + `pyautogui` | Slower, more expensive (screenshots per iteration) |

### Why Browser CDP First

The existing `browser.py` uses Playwright with a bundled dev-browser. By connecting to the user's real Chrome via CDP instead, we get:
- Access to all authenticated sessions (Google Ads, analytics, trading)
- DOM-level interaction (click by selector/role, fill by label) — more reliable than pixel coordinates
- Page snapshots (accessibility tree) without sending screenshots to the API
- The existing isolated browser flow already works and remains available for non-authenticated browsing

Computer Use is reserved for when the task genuinely needs desktop scope: non-browser apps, multi-app copy/paste, or controlling UI that isn't web-based.

## Tier 1: Browser CDP

### How It Works

Chrome must be running with remote debugging enabled:
```bash
# User adds this to Chrome launch (or bot does it via osascript)
open -a "Google Chrome" --args --remote-debugging-port=9222
```

Playwright connects to the running Chrome:
```python
browser = await playwright.chromium.connect_over_cdp("http://localhost:9222")
context = browser.contexts[0]  # user's existing browser context with all cookies/auth
page = select_page(context, page_url_pattern="ads.google.com") or context.new_page()
```

### Changes to `claw_v2/browser.py`

Extend `DevBrowserService` with a new mode: `connect_to_chrome()`.

```python
class DevBrowserService:
    # Existing: run_script(), browse(), screenshot(), interact()

    # New: connect to user's real Chrome
    def connect_to_chrome(self, *, cdp_url: str = "http://localhost:9222") -> BrowseResult:
        """Connect to Chrome via CDP. Returns list of open pages."""

    def chrome_navigate(self, url: str, *, page_index: int | None = None, page_title: str | None = None, page_url_pattern: str | None = None) -> BrowseResult:
        """Navigate a matched Chrome tab, or a dedicated Claw tab, to URL and return page snapshot."""

    def chrome_interact(self, *, actions: list[dict], page_index: int | None = None, page_title: str | None = None, page_url_pattern: str | None = None) -> BrowseResult:
        """Run structured actions on a matched Chrome tab (same action format as interact())."""

    def chrome_screenshot(self, *, page_index: int | None = None, page_title: str | None = None, page_url_pattern: str | None = None, name: str = "chrome.png") -> BrowseResult:
        """Screenshot a matched Chrome tab, return path."""
```

These methods use the same `BrowseResult` dataclass and action format as the existing `interact()` method. The difference is the browser instance: CDP-connected Chrome vs. bundled dev-browser.

### Chrome Page Selection

Never assume `context.pages[0]`.

Page selection order:
1. Exact `page_index`, if explicitly provided
2. First page whose URL matches `page_url_pattern`
3. First page whose title matches `page_title`
4. Otherwise create a dedicated new tab for Claw via `context.new_page()`

The bot must never navigate an arbitrary existing user tab by default.

### Implementation: Playwright Script over CDP

The existing `run_script()` method spawns `dev-browser` with a JS script. For Chrome CDP, we need a different runner that connects to Chrome instead. Two options:

**Option A (recommended): New script runner using Playwright Python directly**

```python
def _run_cdp_script(self, script_fn: Callable, cdp_url: str, timeout: int) -> ScriptResult:
    """Run a Playwright Python function against Chrome CDP."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        context = browser.contexts[0]
        result = script_fn(context)
        browser.close()
        return result
```

**Option B: Reuse dev-browser with CDP flag**

If dev-browser supports CDP connection, pass the URL as a flag. Less likely given the current implementation.

Going with Option A. This means `playwright` Python package is needed (already likely installed as a dep of dev-browser, but confirm).

### Chrome Lifecycle

The bot doesn't manage Chrome's lifecycle. Chrome is assumed to be running. If CDP connection fails:
1. Bot tries `osascript -e 'tell app "Google Chrome" to activate'` to ensure Chrome is running
2. If Chrome isn't running with `--remote-debugging-port`, bot tells the user: "Chrome no está corriendo con debug port. Ejecuta: `open -a 'Google Chrome' --args --remote-debugging-port=9222`"
3. Config option `chrome_cdp_url` (default `http://localhost:9222`) for custom port

### Bot Commands (Tier 1)

`/browse <url>` keeps its current behavior: isolated bundled browser, no authenticated Chrome session.

CDP access is explicit:

| Command | Action |
|---------|--------|
| `/chrome_pages` | List open Chrome tabs (title + URL) |
| `/chrome_browse <url>` | Open URL in a dedicated Claw-controlled Chrome tab and return page snapshot |
| `/chrome_shot [page selector]` | Screenshot the selected Chrome tab |

### Approval Gate for Browser

The same sensitive URL pattern list applies. For Tier 1:
- **Read actions** (`chrome_pages`, navigate, snapshot, screenshot) → autonomous
- **Write actions** (`click`, `fill`, `press`, `check`, `select`, `submit`) against the user's real Chrome session → require explicit approval by default
- **Sensitive URL matches** raise the action risk to `high_risk` and always require approval
- **Submit/confirm/payment-like actions** always require approval, regardless of URL

## Tier 2: Computer Use

### When to Use

Computer Use is invoked explicitly via `/computer <instruction>`. It is NOT auto-triggered by the brain. The user decides when desktop control is needed.

### How It Works

Uses Anthropic API directly (not Claude Agent SDK):

```python
client = anthropic.Anthropic()
response = client.beta.messages.create(
    model="claude-opus-4-6",
    max_tokens=4096,
    tools=[{
        "type": "computer_20251124",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 800,
    }],
    messages=messages,
    betas=["computer-use-2025-11-24"],
)
```

Agent loop: screenshot → Claude decides action → execute via pyautogui → screenshot → repeat.

### `claw_v2/computer.py` — ComputerUseService

**Screenshot capture:**
```python
subprocess.run(["screencapture", "-x", path])  # macOS native
```
- Resizes to configured display dimensions for the API
- Computes scale factor for coordinate mapping (Retina)
- Returns base64-encoded PNG

**Action executor:**
```python
pyautogui.click(x, y)
pyautogui.typewrite("text", interval=0.02)
pyautogui.hotkey("cmd", "t")
pyautogui.scroll(amount)
```
- Maps all Computer Use actions: screenshot, left_click, right_click, double_click, middle_click, type, key, mouse_move, scroll, left_click_drag
- Scales coordinates from API space to screen space
- Configurable delay between actions (default 0.3s)

**Agent loop:**
- Iterates until `stop_reason != "tool_use"` or `max_iterations` (30) reached
- Each iteration: extract tool_use → gate check → execute → screenshot → append result
- Returns final text response

### `claw_v2/computer_gate.py` — ActionGate

Shared between Tier 1 and Tier 2. Classifies actions:

**Safe (auto-execute):**
- screenshot, mouse_move, scroll, zoom
- navigate/goto (browser)
- key with navigation keys (arrows, escape, tab)

**Needs approval (pause):**
- Tier 1: all write actions in Browser CDP (`click`, `fill`, `press`, `check`, `uncheck`, `select`, `submit`)
- Tier 2: any state-changing desktop action without a trustworthy browser URL context (`click`, `double_click`, `middle_click`, `right_click`, `left_click_drag`, `type`, `hotkey`, `enter`)
- key with destructive combos on sensitive sites or in desktop context

**Sensitive URL patterns (configurable):**
```python
DEFAULT_SENSITIVE_URLS = [
    "ads.google.com",
    "polymarket.com",
    "robinhood.com",
    "binance.com",
    "stripe.com",
    "paypal.com",
]
```

When paused:
1. Bot sends screenshot + action description to Telegram as photo
2. Bot creates an action-scoped approval via `ApprovalManager`
3. User sends `/action_approve <approval_id> <token>` or `/action_abort <approval_id>`

The gate is context-aware:
- Browser CDP may use the real page URL
- Computer Use may use the current URL when Chrome is frontmost
- If URL is missing or stale, Tier 2 defaults to the stricter desktop-write policy

### ComputerSession — Session State

```python
@dataclass
class ComputerSession:
    task: str
    messages: list[dict]
    status: str                  # "running" | "awaiting_approval" | "done" | "aborted"
    pending_action: dict | None
    screenshot_path: str | None
    max_iterations: int = 30
    iteration: int = 0
    current_url: str | None = None
```

In-memory only. If the bot restarts mid-session, the session is lost — fail-safe (no pending action executes). Sessions are short-lived (~2-5 min max). One active session per Telegram chat.

### Bot Commands (Tier 2)

| Command | Action |
|---------|--------|
| `/computer <instruction>` | Start Computer Use session |
| `/screen` | Take desktop screenshot, send to Telegram |
| `/action_approve <approval_id> <token>` | Approve pending Browser CDP or Computer Use action |
| `/action_abort <approval_id>` | Reject/cancel a pending Browser CDP or Computer Use action |
| `/computer_abort` | Cancel the active Computer Use session for the current chat |

## Config

New fields in `AppConfig`:

```python
# Tier 1: Browser CDP
chrome_cdp_enabled: bool        # env: CHROME_CDP_ENABLED, default: True
chrome_cdp_url: str             # env: CHROME_CDP_URL, default: "http://localhost:9222"

# Tier 2: Computer Use
computer_use_enabled: bool      # env: COMPUTER_USE_ENABLED, default: True
computer_display_width: int     # env: COMPUTER_DISPLAY_WIDTH, default: 1280
computer_display_height: int    # env: COMPUTER_DISPLAY_HEIGHT, default: 800

# Shared
sensitive_urls: list[str]       # env: SENSITIVE_URLS, default: "ads.google.com:polymarket.com:..."
```

## Files Changed

| File | Change |
|------|--------|
| `claw_v2/browser.py` | Add CDP connection methods: `connect_to_chrome`, `chrome_navigate`, `chrome_interact`, `chrome_screenshot` |
| `claw_v2/computer.py` | **New** — `ComputerUseService`, `ComputerSession`, screenshot/action/agent loop |
| `claw_v2/computer_gate.py` | **New** — `ActionGate`, URL pattern matching, action classification (shared by both tiers) |
| `claw_v2/bot.py` | Add `/computer`, `/screen`, `/chrome_pages`, `/chrome_browse`, `/chrome_shot`, `/action_approve`, `/action_abort`, `/computer_abort`; keep `/browse` isolated |
| `claw_v2/telegram.py` | Add photo sending capability for screenshots |
| `claw_v2/config.py` | Add CDP and Computer Use config fields |
| `claw_v2/main.py` | Wire `ComputerUseService` in `build_runtime()` |
| `claw_v2/SOUL.md` | Document both tiers |
| `claw_v2/approval.py` | Add reject/cancel support for action-scoped approvals |
| `tests/test_computer.py` | **New** — Computer Use service tests |
| `tests/test_computer_gate.py` | **New** — Action gate tests |
| `tests/test_browser.py` | CDP connection tests |
| `tests/test_bot.py` | Command routing and approval command tests |
| `tests/test_telegram.py` | Photo delivery tests for approval screenshots |
| `tests/helpers.py` | Add new config fields to `make_config()` |
| `pyproject.toml` | Add `anthropic`, `pyautogui` dependencies |

## Testing Strategy

- **Action gate:** Pure function tests — all action types × sensitive/non-sensitive URLs
- **Desktop gate fallback:** Verify that missing/unknown URL in Tier 2 still blocks state-changing actions
- **Browser CDP:** Mock Playwright CDP connection, verify navigate/interact/screenshot
- **Page selection:** Verify explicit selector matching and that the service creates a dedicated tab instead of reusing an arbitrary existing tab
- **Computer Use screenshot:** Mock `subprocess.run` for `screencapture`, verify resize + base64
- **Computer Use actions:** Mock `pyautogui`, verify coordinate scaling
- **Agent loop:** Mock `anthropic.Anthropic().beta.messages.create`, simulate multi-step flow
- **Bot commands:** Mock services, verify `/browse` stays isolated, `/chrome_browse` uses CDP, `/computer` starts session, `/action_approve`/`/action_abort` work
- **Gate integration:** Full flow with mocked API, verify pause on sensitive URL
- **Restart safety:** Verify that no pending Computer Use action is resumed after process restart

## Security

- Browser CDP accesses the user's real Chrome with all cookies/auth. Treat all write actions as approval-gated by default; sensitive-site writes are always `high_risk`.
- Computer Use runs on the real Mac desktop. All actions visible and affect real apps.
- Approval gate shared between both tiers, but Tier 2 defaults to stricter behavior when there is no trustworthy URL context.
- Max iterations (30) prevents runaway Computer Use loops.
- Screenshots stored in `/tmp/`, cleaned up after session.
- CDP connection is localhost only — no remote access.

## Out of Scope

- MCP connector for Google Ads API (programmatic access)
- Session recording/replay
- Multi-display support
- Docker/Xvfb virtual display
- Auto-launching Chrome with debug port (user responsibility, documented in setup)
