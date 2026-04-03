# Browser Simplification — Design Spec

**Date:** 2026-04-02
**Branch:** feat/pending-items

## Problem

The bot's `/browse` command tries 3 strategies in sequence (Playwright headless → Firecrawl → Chrome CDP), taking up to 90 seconds before failing. Chrome CDP requires the user to manually launch Chrome with `--remote-debugging-port=9222`, which breaks constantly (profile locked, flag forgotten, Chrome restarted).

## Goal

Make URL reading instant and browser interaction frictionless.

## Two Pieces

### 1. Reading: Jina Reader

Replace the 3-strategy pipeline for `/browse`.

**`_browse_response` flow:**

For auth/social domains (`_AUTH_DOMAINS`):
1. ManagedChrome first (has cookies, JS rendering)
2. If CDP fails → Jina as fallback

For public URLs:
1. If tweet (`x.com`, `twitter.com`) → transform to `fxtwitter.com`
2. `httpx.get(f"https://r.jina.ai/{url}", headers={"Accept": "text/markdown"}, timeout=10)`
3. **Content validation** — Jina 200 is not enough. Check:
   - Response has >100 chars of content (not empty/stub)
   - No login wall signals (`_is_login_wall()` reused from current code)
   - Content-type is text (not binary/error page)
4. If Jina fails OR content validation fails → fallback to ManagedChrome CDP
5. Pass result to brain for analysis

Eliminates: Playwright headless for reading, Firecrawl, multi-strategy cascade.

Response time: 2-5 seconds for public URLs (vs 30-90 current).

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
- Pass `managed_chrome` to `BotService` which stores it and uses `managed_chrome.cdp_url` everywhere

**First-time setup:** Chrome opens with empty profile. User logs into Twitter/etc manually via `/chrome_login`. Cookies persist in `~/.claw/chrome-profile` across bot restarts.

**Headless:** Launch with `--headless=new` by default.

## CDP Consumer Migration

**All CDP consumers must use ManagedChrome's port.** No more hardcoded 9222.

### DevBrowserService (`claw_v2/browser.py`)

Methods `connect_to_chrome()`, `chrome_navigate()`, `chrome_screenshot()` already accept `cdp_url` as a kwarg with default `http://localhost:9222`. The bot handlers must now pass `cdp_url=managed_chrome.cdp_url` explicitly:

- `_chrome_pages_response()` (`bot.py:779`) → pass `cdp_url`
- `_chrome_browse_response()` (`bot.py:798`) → pass `cdp_url`
- `_chrome_shot_response()` (`bot.py:806`) → pass `cdp_url`
- `_browse_response()` CDP fallback → pass `cdp_url`

### BrowserUseService (`claw_v2/main.py:297`)

Currently: `BrowserUseService(cdp_url=config.chrome_cdp_url)` where `chrome_cdp_url` defaults to `http://localhost:9222`.

Change: `BrowserUseService(cdp_url=managed_chrome.cdp_url)`. This means `BrowserUseService` must be constructed AFTER `ManagedChrome.start()`, so move its construction into `lifecycle.py` (after chrome starts) instead of `main.py`.

### Config change

`chrome_cdp_url` in `AppConfig` becomes vestigial. Replace with `claw_chrome_port: int` (default 9250, from `CLAW_CHROME_PORT`). The actual URL is always derived from ManagedChrome.

## Error Messages

Update `_format_chrome_cdp_error()` (`bot.py:1305`). Current message tells user to launch Chrome manually with `--remote-debugging-port=9222`. New message:

```
"Chrome del bot no responde. Reinicia el bot o verifica que Chrome esté instalado."
```

No more instructions to launch Chrome manually — that's ManagedChrome's job.

## Commands

All existing `/chrome_*` commands continue working, now pointed at ManagedChrome's port.

New:
- `/chrome_login` — restart Chrome in visible (non-headless) mode so user can login to sites
- `/chrome_headless` — restart Chrome back to headless mode after login

Both commands call `managed_chrome.stop()` then `managed_chrome.start(headless=True/False)`.

## Testing

**`tests/test_browse.py` (new):**
- `test_browse_jina_success` — mock httpx.get, returns markdown
- `test_browse_tweet_transforms_url` — x.com → fxtwitter.com
- `test_browse_jina_fallback_to_cdp` — Jina returns empty content, falls through to CDP
- `test_browse_jina_login_wall_falls_to_cdp` — Jina 200 but login wall detected, falls to CDP
- `test_browse_auth_domain_skips_jina` — x.com goes to CDP first, not Jina

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
| `claw_v2/bot.py` | Simplify `_browse_response`, pass `cdp_url` to all `/chrome_*` handlers, update error messages, add `/chrome_login` + `/chrome_headless` |
| `claw_v2/config.py` | Replace `chrome_cdp_url` with `claw_chrome_port` |
| `claw_v2/lifecycle.py` | Wire ManagedChrome start/stop, move BrowserUseService construction here |
| `claw_v2/main.py` | Remove BrowserUseService construction (moved to lifecycle) |
| `claw_v2/telegram.py` | Add `/chrome_login`, `/chrome_headless` to menu |
| `tests/helpers.py` | Update `make_config` for new config field |
| `tests/test_browse.py` | **New** — Jina browse tests |
| `tests/test_chrome.py` | **New** — ManagedChrome tests |
| `tests/test_bot.py` | Update CDP error message test |

## Out of Scope

- Auto-login to social media accounts
- Screenshot via Jina (text only)
