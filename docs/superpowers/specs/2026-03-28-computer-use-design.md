# Claude Computer Use Integration — Design Spec

**Date:** 2026-03-28
**Status:** Approved
**Branch:** feat/pending-items

## Goal

Give the bot full autonomy to see and control the user's Mac — browse authenticated sites via the active Chrome session, interact with any app, and automate multi-app flows. Uses Claude Computer Use API (self-hosted, beta) with an approval gate for destructive actions on financial sites.

## Approach

Add a second LLM mode alongside the existing Brain (Claude Agent SDK). Computer Use runs via the Anthropic API directly (`client.beta.messages.create`) with tool type `computer_20251124` and beta header `computer-use-2025-11-24`. The bot captures screenshots with macOS `screencapture`, executes mouse/keyboard with `pyautogui`, and runs the agent loop until the task completes.

## Architecture

### Two LLM Modes

| Mode | When | API Surface |
|------|------|-------------|
| **Brain** (existing) | Chat, commands, code tools | Claude Agent SDK (CLI wrapper) |
| **Computer Use** (new) | Screen control, UI automation | Anthropic API direct (`anthropic` package) |

### Agent Loop Flow

```
User: "/computer abre Google Ads y dame el resumen"
  → Bot creates ComputerSession
  → Takes initial screenshot
  → Sends to Claude with computer_20251124 tool
  → Claude returns tool_use (e.g., left_click at [x,y])
  → ActionGate classifies: safe → execute, needs_approval → pause
  → Execute action via pyautogui
  → Take new screenshot
  → Send result back to Claude
  → Repeat until end_turn or max_iterations
  → Send final response to Telegram
```

### Chrome Session Integration

For authenticated sites (Google Ads, etc.):
- Activate Chrome window: `osascript -e 'tell app "Google Chrome" to activate'`
- Open URL: `open -a "Google Chrome" "https://ads.google.com"`
- Screenshot captures the live, already-authenticated session
- No login automation needed for sites the user is already logged into

For fresh logins:
- The agent loop navigates normally: opens Chrome, goes to URL, fills forms
- Credentials via `~/.claw/credentials/` config or provided by user on demand

## Components

### `claw_v2/computer.py` — ComputerUseService

Three responsibilities:

**1. Screenshot capture:**
```python
# macOS native — no Docker/Xvfb needed
subprocess.run(["screencapture", "-x", path])
```
- Resizes to `display_width x display_height` (default 1280x800) for the API
- Computes scale factor for coordinate mapping (Retina displays)
- Returns base64-encoded PNG

**2. Action executor:**
```python
# pyautogui for mouse/keyboard
pyautogui.click(x, y)
pyautogui.typewrite("text", interval=0.02)
pyautogui.hotkey("cmd", "t")  # new tab
pyautogui.scroll(amount)
```
- Maps all Computer Use actions: screenshot, left_click, right_click, double_click, middle_click, type, key, mouse_move, scroll, left_click_drag
- Scales coordinates from API space to screen space using scale factor
- Adds configurable delay between actions (default 0.3s)

**3. Agent loop:**
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
- Iterates until `stop_reason != "tool_use"` or `max_iterations` reached
- Each iteration: extract tool_use → gate check → execute → screenshot → append result
- Returns final text response

### `claw_v2/computer_gate.py` — ActionGate

Classifies each Computer Use action before execution:

**Safe (auto-execute):**
- `screenshot`
- `mouse_move`
- `scroll`
- `key` with navigation keys (arrows, escape, tab, enter for non-financial)
- `zoom`

**Needs approval (pause and ask via Telegram):**
- `left_click` when current URL matches a sensitive pattern
- `type` when current URL matches a sensitive pattern
- `key` with destructive combos (cmd+delete, etc.) on sensitive sites

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

The gate receives the action dict and the current page URL (extracted from the last screenshot metadata or Chrome AppleScript query). When an action needs approval:
1. Bot sends the current screenshot + action description to Telegram
2. Session status changes to `awaiting_approval`
3. User sends `/approve` or `/abort`
4. Session resumes or cancels

### `ComputerSession` — Session State

```python
@dataclass
class ComputerSession:
    task: str                    # original instruction
    messages: list[dict]         # agent loop history
    status: str                  # "running" | "awaiting_approval" | "done" | "aborted"
    pending_action: dict | None  # action awaiting approval
    screenshot_path: str | None  # latest screenshot file path
    max_iterations: int = 30     # safety limit
    iteration: int = 0
    current_url: str | None = None  # last known URL for gate classification
```

One active session per Telegram chat. `/computer` while a session is running returns "session already active, use /abort to cancel".

### Bot Commands

| Command | Action |
|---------|--------|
| `/computer <instruction>` | Start Computer Use session |
| `/screen` | Take screenshot, send to Telegram as photo |
| `/approve` | Approve pending destructive action |
| `/abort` | Cancel active Computer Use session |

### Telegram Photo Sending

`telegram.py` needs a method to send screenshot images as Telegram photos (not just text). Uses `update.message.reply_photo(photo=open(path, "rb"))` or `context.bot.send_photo(chat_id, photo=...)`.

### Config

New fields in `AppConfig`:

```python
computer_use_enabled: bool          # env: COMPUTER_USE_ENABLED, default: True
computer_sensitive_urls: list[str]  # env: COMPUTER_SENSITIVE_URLS, default: "ads.google.com:polymarket.com:..."
computer_display_width: int         # env: COMPUTER_DISPLAY_WIDTH, default: 1280
computer_display_height: int        # env: COMPUTER_DISPLAY_HEIGHT, default: 800
```

### Wiring in main.py

```python
computer = ComputerUseService(config=config, observe=observe)
bot = BotService(..., computer=computer)
```

## Files Changed

| File | Change |
|------|--------|
| `claw_v2/computer.py` | **New** — `ComputerUseService`, `ComputerSession`, screenshot/action/loop |
| `claw_v2/computer_gate.py` | **New** — `ActionGate`, URL pattern matching, action classification |
| `claw_v2/bot.py` | Add `/computer`, `/screen`, `/approve`, `/abort` commands |
| `claw_v2/telegram.py` | Add `send_photo` capability for screenshots |
| `claw_v2/config.py` | Add `computer_use_enabled`, `computer_sensitive_urls`, display dimensions |
| `claw_v2/main.py` | Wire `ComputerUseService` in `build_runtime()` |
| `claw_v2/SOUL.md` | Document Computer Use capability |
| `tests/test_computer.py` | **New** — unit tests for service |
| `tests/test_computer_gate.py` | **New** — unit tests for action gate |
| `tests/helpers.py` | Add computer config fields to `make_config()` |
| `pyproject.toml` | Add `anthropic`, `pyautogui` dependencies |

## Testing Strategy

- **Action gate:** Test classification of all action types against sensitive/non-sensitive URLs. Pure function, no mocking needed.
- **Screenshot:** Mock `subprocess.run` for `screencapture`, verify base64 encoding and resize logic.
- **Action executor:** Mock `pyautogui` calls, verify coordinate scaling.
- **Agent loop:** Mock `anthropic.Anthropic().beta.messages.create`, simulate multi-step tool_use → end_turn flow.
- **Bot commands:** Mock `ComputerUseService`, verify `/computer` starts session, `/approve` resumes, `/abort` cancels.
- **Integration:** Full loop with mocked API and mocked pyautogui, verify gate pauses on sensitive URL.

## Security

- Computer Use runs on the user's real Mac, not a sandbox. All actions are visible and affect real apps.
- The approval gate prevents automated clicks/typing on financial sites without explicit user consent.
- `COMPUTER_SENSITIVE_URLS` is configurable — the user can add/remove patterns.
- Max iterations (30) prevents runaway loops.
- Screenshots are stored in `/tmp/` and cleaned up after each session.
- No credentials are stored in code — `~/.claw/credentials/` or user-provided via Telegram.

## Out of Scope

- MCP connector for Google Ads API (programmatic access without UI)
- Session recording/replay
- Multi-display support
- Docker/Xvfb virtual display (uses real Mac screen)
