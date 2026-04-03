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

**`_browse_response` flow — single decision tree:**

`x.com` and `twitter.com` are in `_AUTH_DOMAINS`. There is no fxtwitter transform. All auth domains go through CDP.

```
URL arrives
  ├── domain in _AUTH_DOMAINS? → ManagedChrome CDP (has cookies + JS)
  │     └── CDP fails? → return error (no Jina fallback — auth content needs cookies)
  └── public URL → Jina Reader
        ├── httpx.get("https://r.jina.ai/{url}", headers={"Accept": "text/markdown"}, timeout=10)
        ├── Content validation (Jina 200 is not enough):
        │     - Response has >100 chars (not empty/stub)
        │     - No login wall signals (_is_login_wall() reused)
        │     - Content-type is text (not binary/error)
        ├── Validation passes? → return markdown
        └── Validation fails? → fallback to ManagedChrome CDP
```

Eliminates: Playwright headless for reading, Firecrawl, fxtwitter transform, multi-strategy cascade.

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
3. **Wait for port release:** poll `lsof -ti :{port}` until empty or 5s timeout. Old process needs time to die and release the port + profile lock.
4. Launch: `Google Chrome --remote-debugging-port={port} --user-data-dir={profile_dir} --no-first-run --headless=new`
5. Wait until `GET http://localhost:{port}/json/version` responds (max 10s)

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

- Replace `chrome_cdp_url: str` with `claw_chrome_port: int` (default 9250, from `CLAW_CHROME_PORT`). The actual URL is always derived from ManagedChrome.
- **Keep `chrome_cdp_enabled: bool`** (default True, from `CHROME_CDP_ENABLED`). When False, ManagedChrome is not started in lifecycle.py and all CDP features gracefully degrade (browse falls back to Jina only, `/chrome_*` commands return "Chrome not enabled"). Tests set this to False via `make_config` so no Chrome process is needed in CI.

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
- `test_browse_jina_success` — mock httpx.get, returns markdown for public URL
- `test_browse_jina_fallback_to_cdp` — Jina returns empty content, falls through to CDP
- `test_browse_jina_login_wall_falls_to_cdp` — Jina 200 but login wall detected, falls to CDP
- `test_browse_auth_domain_goes_to_cdp` — x.com goes to CDP, not Jina
- `test_browse_auth_domain_cdp_fails_returns_error` — x.com CDP fails, returns error (no Jina fallback)

**`tests/test_chrome.py` (new):**
- `test_start_kills_existing_chrome_on_port` — mock lsof returning Chrome PID, verify kill called
- `test_start_waits_for_port_release` — verify poll loop after kill before relaunch
- `test_start_errors_if_port_occupied_by_non_chrome` — mock lsof returning non-Chrome PID, verify error
- `test_start_launches_chrome` — mock subprocess.Popen, verify correct flags
- `test_stop_kills_subprocess` — verify process terminated
- `test_ensure_idempotent` — calling twice doesn't launch two Chrome processes
- `test_custom_port` — verify configured port used

**`tests/test_bot.py` (modify):**
- Update CDP error message test to match new message
- `test_chrome_login_restarts_visible` — `/chrome_login` calls stop then start(headless=False)
- `test_chrome_headless_restarts` — `/chrome_headless` calls stop then start(headless=True)

**`tests/test_lifecycle.py` or inline:**
- `test_browser_use_service_gets_managed_cdp_url` — verify BrowserUseService constructed with managed port

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

## Open Clarifications

1. **`/browse` when `chrome_cdp_enabled=False`:**
   The spec says auth domains (`x.com`, `twitter.com`) must go through CDP and return an error if CDP fails, with no Jina fallback. It also says that when `chrome_cdp_enabled=False`, `/browse` falls back to Jina-only. These rules conflict for auth domains and for public URLs whose Jina validation fails while CDP is disabled. The implementation should define one deterministic behavior for both cases.

2. **ManagedChrome startup failure policy:**
   `lifecycle.py` starts ManagedChrome eagerly after transport startup. The spec defines runtime error messaging for CDP failures, but not lifecycle behavior if Chrome is missing, the configured port is occupied by a non-Chrome process, or `/json/version` never comes up. The implementation should explicitly choose whether bot startup degrades without CDP or fails fast before announcing the bot as online.

## Out of Scope

- Auto-login to social media accounts
- Screenshot via Jina (text only)
