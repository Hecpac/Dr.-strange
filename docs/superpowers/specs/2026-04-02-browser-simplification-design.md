# Browser Simplification — Design Spec

**Date:** 2026-04-02
**Branch:** feat/pending-items

## Problem

The bot's `/browse` command tries 3 strategies in sequence (Playwright headless → Firecrawl → Chrome CDP), taking up to 90 seconds before failing. Chrome CDP requires the user to manually launch Chrome with `--remote-debugging-port=9222`, which breaks constantly (profile locked, flag forgotten, Chrome restarted).

## Goal

Make URL reading instant and browser interaction frictionless.

## Two Pieces

### 1. Reading: Jina Reader

Replace the 3-strategy pipeline with a single HTTP GET to `https://r.jina.ai/{url}`.

**`_browse_response` flow:**
1. If tweet URL (`x.com`, `twitter.com`) → transform to `fxtwitter.com` equivalent
2. `httpx.get(f"https://r.jina.ai/{url}", timeout=10)` → returns markdown
3. If Jina fails → fallback to `chrome_navigate()` on ManagedChrome
4. Pass result to brain for analysis

Eliminates: Playwright headless for reading, Firecrawl, multi-strategy cascade.

Response time: 2-5 seconds (vs 30-90 current).

### 2. Interaction: ManagedChrome

New class in `claw_v2/chrome.py` that auto-manages a dedicated Chrome process.

```python
class ManagedChrome:
    def __init__(self, port: int = 9250, profile_dir: str = "~/.claw/chrome-profile")
```

**Port:** 9250 (configurable via `CLAW_CHROME_PORT`). Avoids collision with user's Chrome on 9222.

**`start()` logic:**
1. `lsof -ti :{port}` → get PID(s) on the port
2. For each PID → `ps -p {pid} -o comm=` → check if Chrome
   - If Chrome → `kill {pid}` (cleanup zombie from crash)
   - If not Chrome → raise error: "Port {port} occupied by {process}. Set CLAW_CHROME_PORT."
3. Launch: `Google Chrome --remote-debugging-port={port} --user-data-dir={profile_dir} --no-first-run --headless=new`
4. Wait until `GET http://localhost:{port}/json/version` responds (max 10s)

**No PID files. No lock files. No state.** The port is the state. Start always cleans up and launches fresh.

**`stop()`:** Kill the subprocess we launched.

**`ensure()`:** If subprocess is dead or None, call `start()`. Idempotent.

**`cdp_url`:** Property returning `http://localhost:{port}`.

**Lifecycle wiring:**
- `lifecycle.py`: `managed_chrome.start()` after transport starts
- `lifecycle.py`: `managed_chrome.stop()` in finally block
- `bot.browser.chrome_navigate()` uses `cdp_url=managed_chrome.cdp_url`

**First-time setup:** Chrome opens with empty profile. User logs into Twitter/etc manually once. Cookies persist in `~/.claw/chrome-profile` across bot restarts.

**Headless:** Launch with `--headless=new` by default. When user needs to login to a site for the first time, they can run `/chrome_login` which restarts Chrome in visible mode temporarily.

## Config

Add to `AppConfig`:
- `claw_chrome_port: int` — default 9250, from `CLAW_CHROME_PORT`

## Commands

Existing `/chrome_*` commands continue working, now pointed at ManagedChrome's port.

New:
- `/chrome_login` — restart Chrome in visible (non-headless) mode so user can login to sites. After login, `/chrome_headless` returns to headless.

## Testing

**`tests/test_browse.py` (new):**
- `test_browse_jina_success` — mock httpx.get, returns markdown
- `test_browse_tweet_transforms_url` — x.com → fxtwitter.com
- `test_browse_jina_fallback_to_cdp` — Jina fails, uses chrome_navigate

**`tests/test_chrome.py` (new):**
- `test_start_kills_existing_chrome_on_port` — mock lsof returning Chrome PID, verify kill called
- `test_start_errors_if_port_occupied_by_non_chrome` — mock lsof returning non-Chrome PID, verify error
- `test_start_launches_chrome` — mock subprocess.Popen, verify correct flags
- `test_stop_kills_subprocess` — verify process terminated
- `test_ensure_idempotent` — calling twice doesn't launch two Chrome processes
- `test_custom_port` — verify configured port used

## Files Changed

| File | Change |
|------|--------|
| `claw_v2/chrome.py` | **New** — ManagedChrome |
| `claw_v2/bot.py` | Simplify `_browse_response` with Jina + CDP fallback |
| `claw_v2/config.py` | Add `claw_chrome_port` |
| `claw_v2/lifecycle.py` | Wire ManagedChrome start/stop |
| `claw_v2/telegram.py` | Add `/chrome_login` to menu |
| `tests/test_browse.py` | **New** — Jina browse tests |
| `tests/test_chrome.py` | **New** — ManagedChrome tests |

## Out of Scope

- Auto-login to social media accounts
- Screenshot via Jina (text only)
- Replacing `/computer` command (stays as-is)
