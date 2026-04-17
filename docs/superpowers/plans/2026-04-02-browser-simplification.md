# Browser Simplification — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 3-strategy browse pipeline with Jina Reader for reading + ManagedChrome for interaction, eliminating manual Chrome CDP setup.

**Architecture:** `ManagedChrome` auto-launches a dedicated Chrome process on port 9250 with persistent profile. `/browse` uses Jina Reader (HTTP GET) for public URLs, ManagedChrome CDP for auth domains. All `/chrome_*` commands and BrowserUseService point to ManagedChrome's port.

**Tech Stack:** httpx (Jina Reader), subprocess + lsof (ManagedChrome), Playwright CDP (existing)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `claw_v2/chrome.py` | **New** — ManagedChrome: launch, stop, ensure, port cleanup |
| `tests/test_chrome.py` | **New** — ManagedChrome unit tests |
| `tests/test_browse.py` | **New** — Jina Reader browse tests |
| `claw_v2/config.py` | Replace `chrome_cdp_url` with `claw_chrome_port` |
| `tests/helpers.py` | Update `make_config` |
| `claw_v2/bot.py` | Rewrite `_browse_response`, update `/chrome_*` handlers, add `/chrome_login` + `/chrome_headless`, update error messages |
| `claw_v2/lifecycle.py` | Wire ManagedChrome start/stop, move BrowserUseService |
| `claw_v2/main.py` | Remove BrowserUseService construction |
| `claw_v2/telegram.py` | Add new commands to menu |

---

### Task 1: ManagedChrome — core class

**Files:**
- Create: `claw_v2/chrome.py`
- Create: `tests/test_chrome.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_chrome.py
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch, call

from claw_v2.chrome import ManagedChrome, ChromeStartError


class ManagedChromeTests(unittest.TestCase):
    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_launches_chrome(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []  # port free
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        args = mock_popen.call_args[0][0]
        self.assertIn("--remote-debugging-port=9250", args)
        self.assertIn("--user-data-dir=/tmp/test-profile", args)
        self.assertIn("--headless=new", args)
        self.assertIn("--no-first-run", args)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._kill_pid")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_kills_existing_chrome(self, mock_ready, mock_popen, mock_wait, mock_kill, mock_pids) -> None:
        mock_pids.return_value = [(1234, "Google Chrome")]
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mock_kill.assert_called_once_with(1234)
        mock_wait.assert_called_once()

    @patch("claw_v2.chrome._check_port_pids")
    def test_start_errors_non_chrome_on_port(self, mock_pids) -> None:
        mock_pids.return_value = [(5678, "node")]
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        with self.assertRaises(ChromeStartError) as ctx:
            mc.start()
        self.assertIn("node", str(ctx.exception))
        self.assertIn("9250", str(ctx.exception))

    def test_stop_kills_subprocess(self) -> None:
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        proc = MagicMock()
        mc._process = proc
        mc.stop()
        proc.terminate.assert_called_once()
        self.assertIsNone(mc._process)

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_ensure_idempotent(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []
        proc = MagicMock()
        proc.poll.return_value = None  # alive
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start()
        mc.ensure()  # should not call Popen again
        self.assertEqual(mock_popen.call_count, 1)

    def test_custom_port(self) -> None:
        mc = ManagedChrome(port=9999, profile_dir="/tmp/p")
        self.assertEqual(mc.cdp_url, "http://localhost:9999")

    @patch("claw_v2.chrome._check_port_pids")
    @patch("claw_v2.chrome._wait_for_port_free")
    @patch("subprocess.Popen")
    @patch("claw_v2.chrome._wait_for_cdp_ready")
    def test_start_headless_false(self, mock_ready, mock_popen, mock_wait, mock_pids) -> None:
        mock_pids.return_value = []
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc
        mc = ManagedChrome(port=9250, profile_dir="/tmp/test-profile")
        mc.start(headless=False)
        args = mock_popen.call_args[0][0]
        self.assertNotIn("--headless=new", args)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_chrome.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claw_v2.chrome'`

- [ ] **Step 3: Implement ManagedChrome**

```python
# claw_v2/chrome.py
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CHROME_NAMES = ("google chrome", "chrome", "chromium")


class ChromeStartError(RuntimeError):
    """Raised when ManagedChrome cannot start."""


class ManagedChrome:
    """Auto-managed Chrome process with CDP for the bot."""

    def __init__(self, port: int = 9250, profile_dir: str = "~/.claw/chrome-profile") -> None:
        self.port = port
        self.profile_dir = str(Path(profile_dir).expanduser())
        self._process: subprocess.Popen | None = None

    @property
    def cdp_url(self) -> str:
        return f"http://localhost:{self.port}"

    def start(self, *, headless: bool = True) -> None:
        """Kill any Chrome on our port, launch fresh."""
        # Step 1-2: Check port, kill Chrome zombies
        pids = _check_port_pids(self.port)
        for pid, name in pids:
            if any(cn in name.lower() for cn in _CHROME_NAMES):
                logger.info("Killing stale Chrome (PID %d) on port %d", pid, self.port)
                _kill_pid(pid)
            else:
                raise ChromeStartError(
                    f"Port {self.port} occupied by '{name}' (PID {pid}). "
                    f"Set CLAW_CHROME_PORT to use a different port."
                )

        # Step 3: Wait for port release
        if pids:
            _wait_for_port_free(self.port, timeout=5)

        # Step 4: Launch Chrome
        chrome_path = _find_chrome()
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        cmd = [
            chrome_path,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--disable-default-apps",
        ]
        if headless:
            cmd.append("--headless=new")

        self._process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # Step 5: Wait for CDP ready
        _wait_for_cdp_ready(self.port, timeout=10)
        logger.info("ManagedChrome started on port %d (PID %d)", self.port, self._process.pid)

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            logger.info("ManagedChrome stopped")

    def ensure(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return  # still alive
        self.start()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None


def _find_chrome() -> str:
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    raise ChromeStartError("Chrome not found. Install Google Chrome.")


def _check_port_pids(port: int) -> list[tuple[int, str]]:
    """Return [(pid, process_name)] for processes listening on port."""
    try:
        output = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    if not output:
        return []
    results = []
    for line in output.splitlines():
        pid = int(line.strip())
        try:
            name = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "comm="], text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            name = "unknown"
        results.append((pid, name))
    return results


def _kill_pid(pid: int) -> None:
    import signal
    import os
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _wait_for_port_free(port: int, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _check_port_pids(port):
            return
        time.sleep(0.5)
    logger.warning("Port %d not free after %ds, proceeding anyway", port, timeout)


def _wait_for_cdp_ready(port: int, timeout: float = 10) -> None:
    import urllib.request
    url = f"http://localhost:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.5)
    raise ChromeStartError(f"Chrome CDP not responding on port {port} after {timeout}s")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_chrome.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Commit**

```bash
git add claw_v2/chrome.py tests/test_chrome.py
git commit -m "feat(browser): add ManagedChrome with auto port cleanup and lifecycle"
```

---

### Task 2: Config — replace `chrome_cdp_url` with `claw_chrome_port`

**Files:**
- Modify: `claw_v2/config.py:71,100,145`
- Modify: `tests/helpers.py:65-66`

- [ ] **Step 1: Update config.py — field declaration**

In `claw_v2/config.py`, replace:
```python
    chrome_cdp_url: str
```
with:
```python
    claw_chrome_port: int
```

- [ ] **Step 2: Update config.py — from_env()**

In `claw_v2/config.py:145`, replace:
```python
            chrome_cdp_url=os.getenv("CHROME_CDP_URL", "http://localhost:9222"),
```
with:
```python
            claw_chrome_port=int(os.getenv("CLAW_CHROME_PORT", "9250")),
```

- [ ] **Step 3: Update config.py — validate()**

No change needed. `chrome_cdp_url` is not validated currently.

- [ ] **Step 4: Update tests/helpers.py**

In `tests/helpers.py:65-66`, replace:
```python
        chrome_cdp_url="http://localhost:9222",
```
with:
```python
        claw_chrome_port=9250,
```

- [ ] **Step 5: Fix any remaining references to chrome_cdp_url**

Search for `chrome_cdp_url` in the codebase. The only remaining reference should be in `claw_v2/main.py:297` (`BrowserUseService(cdp_url=config.chrome_cdp_url)`) — this will be fixed in Task 5 (lifecycle wiring). For now, change it to use the port:

In `claw_v2/main.py:297`, replace:
```python
    browser_use = BrowserUseService(cdp_url=config.chrome_cdp_url)
```
with:
```python
    browser_use = BrowserUseService(cdp_url=f"http://localhost:{config.claw_chrome_port}")
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ -x --tb=short`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add claw_v2/config.py tests/helpers.py claw_v2/main.py
git commit -m "feat(browser): replace chrome_cdp_url with claw_chrome_port (default 9250)"
```

---

### Task 3: Rewrite `_browse_response` with Jina Reader

**Files:**
- Create: `tests/test_browse.py`
- Modify: `claw_v2/bot.py:648-687`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_browse.py
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.browser import BrowseResult

from tests.helpers import make_config


def _make_bot(**overrides):
    from claw_v2.bot import BotService
    tmpdir = tempfile.mkdtemp()
    config = make_config(Path(tmpdir))
    brain = MagicMock()
    brain.handle_message.return_value = MagicMock(content="brain response")
    defaults = dict(
        brain=brain,
        auto_research=MagicMock(),
        heartbeat=MagicMock(),
        approvals=MagicMock(),
        allowed_user_id="123",
        config=config,
    )
    defaults.update(overrides)
    return BotService(**defaults)


class BrowseJinaTests(unittest.TestCase):
    @patch("claw_v2.bot._jina_read")
    def test_browse_jina_success(self, mock_jina) -> None:
        mock_jina.return_value = "# Article Title\n\nThis is a long article about AI trends in 2026. " + "x" * 200
        bot = _make_bot()
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://example.com/article")
        self.assertIn("Article Title", result)
        mock_jina.assert_called_once()

    @patch("claw_v2.bot._jina_read")
    def test_browse_auth_domain_goes_to_cdp(self, mock_jina) -> None:
        browser = MagicMock()
        browser.chrome_navigate.return_value = BrowseResult(
            url="https://x.com/post/123", title="Tweet", content="Hello world tweet content here " + "x" * 200,
        )
        chrome = MagicMock()
        chrome.cdp_url = "http://localhost:9250"
        bot = _make_bot(browser=browser)
        bot.managed_chrome = chrome
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://x.com/post/123")
        mock_jina.assert_not_called()
        browser.chrome_navigate.assert_called_once()
        self.assertIn("Tweet", result)

    @patch("claw_v2.bot._jina_read")
    def test_browse_auth_domain_cdp_fails_returns_error(self, mock_jina) -> None:
        browser = MagicMock()
        browser.chrome_navigate.side_effect = Exception("CDP down")
        chrome = MagicMock()
        chrome.cdp_url = "http://localhost:9250"
        bot = _make_bot(browser=browser)
        bot.managed_chrome = chrome
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://x.com/post/123")
        mock_jina.assert_not_called()
        self.assertIn("error", result.lower())

    @patch("claw_v2.bot._jina_read")
    def test_browse_jina_empty_falls_to_cdp(self, mock_jina) -> None:
        mock_jina.return_value = ""  # empty = validation fail
        browser = MagicMock()
        browser.chrome_navigate.return_value = BrowseResult(
            url="https://example.com", title="Example", content="Real content from CDP " + "x" * 200,
        )
        chrome = MagicMock()
        chrome.cdp_url = "http://localhost:9250"
        bot = _make_bot(browser=browser)
        bot.managed_chrome = chrome
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://example.com")
        self.assertIn("Example", result)
        browser.chrome_navigate.assert_called_once()

    @patch("claw_v2.bot._jina_read")
    def test_browse_no_chrome_jina_only(self, mock_jina) -> None:
        """When managed_chrome is None, all URLs go through Jina best-effort."""
        mock_jina.return_value = "# Some Content\n\n" + "x" * 200
        bot = _make_bot()
        bot.managed_chrome = None
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://x.com/post/123")
        mock_jina.assert_called_once()
        self.assertIn("Some Content", result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_browse.py -v`
Expected: FAIL — `AttributeError: 'BotService' object has no attribute 'managed_chrome'`

- [ ] **Step 3: Add `managed_chrome` attribute and `_jina_read` helper**

In `claw_v2/bot.py`, add to `__init__` after `self.notebooklm`:
```python
        self.managed_chrome: Any | None = None
```

Add helper function near the bottom of `bot.py` (before `_format_chrome_cdp_error`):

```python
def _jina_read(url: str, *, timeout: float = 10) -> str:
    """Fetch URL content as markdown via Jina Reader."""
    import httpx
    try:
        response = httpx.get(
            f"https://r.jina.ai/{url}",
            headers={"Accept": "text/markdown"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        content = response.text.strip()
        if len(content) < 100:
            return ""
        if _is_login_wall(content):
            return ""
        return content
    except Exception:
        return ""
```

- [ ] **Step 4: Rewrite `_browse_response`**

Replace the entire `_browse_response` method (`bot.py:648-687`) with:

```python
    def _browse_response(self, url: str) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)

        cdp_available = self.managed_chrome is not None and self.browser is not None

        # Auth domains → CDP (needs cookies)
        if _needs_real_browser(normalized_url):
            if not cdp_available:
                # Degrade: try Jina best-effort
                content = _jina_read(normalized_url)
                if content:
                    return f"Contenido parcial (CDP no disponible):\n\n{content[:6000]}"
                return f"browse error: {normalized_url} requiere Chrome CDP pero no está disponible."
            try:
                from urllib.parse import urlparse
                host = urlparse(normalized_url).netloc.lower()
                result = self.browser.chrome_navigate(
                    normalized_url,
                    cdp_url=self.managed_chrome.cdp_url,
                    page_url_pattern=host,
                )
                if result.content.strip():
                    return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"
            except Exception as exc:
                return _format_chrome_cdp_error(exc, prefix="browse error")

        # Public URLs → Jina first
        content = _jina_read(normalized_url)
        if content:
            return content[:6000]

        # Jina failed → CDP fallback
        if cdp_available:
            try:
                result = self.browser.chrome_navigate(
                    normalized_url, cdp_url=self.managed_chrome.cdp_url,
                )
                if result.content.strip():
                    return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"
            except Exception:
                pass

        return f"browse error: no se pudo leer {normalized_url}"
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_browse.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Run full suite**

Run: `uv run pytest tests/ -x --tb=short`
Expected: Some existing browse tests may fail (they mock `browser.browse()` which no longer gets called). Fix in next step.

- [ ] **Step 7: Update existing bot tests that relied on old browse pipeline**

Existing tests in `tests/test_bot.py` that call `browser.browse()` need updating:
- `test_natural_language_url_uses_isolated_browse` — change to mock `_jina_read` instead
- `test_natural_language_bare_domain_is_normalized_for_browse` — same
- `test_browse_command_normalizes_bare_domain` — same

For each: patch `claw_v2.bot._jina_read` to return content, remove `browser.browse` mocks.

- [ ] **Step 8: Run full suite again**

Run: `uv run pytest tests/ -x --tb=short`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add claw_v2/bot.py tests/test_browse.py tests/test_bot.py
git commit -m "feat(browser): rewrite _browse_response with Jina Reader + CDP fallback"
```

---

### Task 4: Migrate `/chrome_*` handlers and error messages

**Files:**
- Modify: `claw_v2/bot.py:779-817,1305-1319`

- [ ] **Step 1: Update `_chrome_pages_response`**

In `bot.py:779-786`, replace:
```python
    def _chrome_pages_response(self) -> str:
        if self.browser is None:
            return "browser unavailable"
        try:
            pages = self.browser.connect_to_chrome()
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome CDP error")
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)
```
with:
```python
    def _chrome_pages_response(self) -> str:
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            pages = self.browser.connect_to_chrome(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome CDP error")
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)
```

- [ ] **Step 2: Update `_chrome_browse_response`**

In `bot.py:788-804`, replace the `chrome_navigate` call:
```python
            result = self.browser.chrome_navigate(
                normalized_url,
                page_url_pattern=host,
            )
```
with:
```python
            result = self.browser.chrome_navigate(
                normalized_url,
                cdp_url=self.managed_chrome.cdp_url,
                page_url_pattern=host,
            )
```

Also update the guard at the top:
```python
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
```

- [ ] **Step 3: Update `_chrome_shot_response`**

In `bot.py:806-817`, replace:
```python
            result = self.browser.chrome_screenshot()
```
with:
```python
            result = self.browser.chrome_screenshot(cdp_url=self.managed_chrome.cdp_url)
```

Also update the guard:
```python
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
```

- [ ] **Step 4: Update error message**

Replace `_format_chrome_cdp_error` function (`bot.py:1305-1319`):
```python
def _format_chrome_cdp_error(exc: Exception, *, prefix: str) -> str:
    message = str(exc)
    lowered = message.lower()
    if any(token in lowered for token in ("econnrefused", "connection refused", "connect_over_cdp", "browser_type.connect_over_cdp")):
        return "Chrome del bot no responde. Reinicia el bot o verifica que Chrome esté instalado."
    return f"{prefix}: {message}"
```

- [ ] **Step 5: Add `/chrome_login` and `/chrome_headless` commands**

In `bot.py` `handle_text`, add after the `/chrome_shot` handler (~line 217):
```python
        if stripped == "/chrome_login":
            return self._chrome_login_response()
        if stripped == "/chrome_headless":
            return self._chrome_headless_response()
```

Add response methods:
```python
    def _chrome_login_response(self) -> str:
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=False)
            return "Chrome reiniciado en modo visible. Haz login en los sitios que necesites. Cuando termines: /chrome_headless"
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"

    def _chrome_headless_response(self) -> str:
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=True)
            return "Chrome reiniciado en modo headless."
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/ -x --tb=short`
Expected: All tests PASS (existing chrome tests may need `managed_chrome` mock — fix inline)

- [ ] **Step 7: Commit**

```bash
git add claw_v2/bot.py
git commit -m "feat(browser): migrate /chrome_* to ManagedChrome, add /chrome_login + /chrome_headless"
```

---

### Task 5: Wire ManagedChrome in lifecycle.py

**Files:**
- Modify: `claw_v2/lifecycle.py:46-87`
- Modify: `claw_v2/main.py:297-309`
- Modify: `claw_v2/telegram.py:130-161`

- [ ] **Step 1: Add import in lifecycle.py**

```python
from claw_v2.chrome import ManagedChrome
```

- [ ] **Step 2: Wire ManagedChrome in `run()`**

In `lifecycle.py`, after `await transport.start()` and the NLM wiring block, add:

```python
        # Wire ManagedChrome
        managed_chrome = None
        if runtime.config.chrome_cdp_enabled:
            try:
                managed_chrome = ManagedChrome(
                    port=runtime.config.claw_chrome_port,
                )
                managed_chrome.start()
            except Exception:
                logger.warning("ManagedChrome failed to start, CDP features disabled", exc_info=True)
                managed_chrome = None
        runtime.bot.managed_chrome = managed_chrome

        # Re-wire BrowserUseService with managed CDP URL
        if managed_chrome is not None:
            from claw_v2.browser_use import BrowserUseService
            runtime.bot.browser_use = BrowserUseService(cdp_url=managed_chrome.cdp_url)
```

- [ ] **Step 3: Add ManagedChrome cleanup to finally block**

In `lifecycle.py`, the `finally` block currently just has `await transport.stop()`. Add chrome cleanup:
```python
        try:
            await runtime.daemon.run_loop(shutdown)
        finally:
            if managed_chrome is not None:
                managed_chrome.stop()
            await transport.stop()
```

- [ ] **Step 4: Remove BrowserUseService from main.py if moved**

In `claw_v2/main.py:297`, the `browser_use` construction can stay as a fallback default (it'll be overridden in lifecycle.py if ManagedChrome starts). No change needed here — the lifecycle override handles it.

- [ ] **Step 5: Add new commands to Telegram menu**

In `claw_v2/telegram.py`, add to the commands list:
```python
            BotCommand("chrome_login", "Chrome visible para login — /chrome_login"),
            BotCommand("chrome_headless", "Volver a Chrome headless — /chrome_headless"),
```

- [ ] **Step 6: Run full test suite**

Run: `uv run pytest tests/ -x --tb=short`
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add claw_v2/lifecycle.py claw_v2/telegram.py
git commit -m "feat(browser): wire ManagedChrome in lifecycle with graceful degradation"
```

---

### Task 6: Smoke test

**Files:** None (verification only)

- [ ] **Step 1: Restart bot**

```bash
kill $(pgrep -f 'claw_v2.main') && sleep 3
ps aux | grep 'claw_v2.main' | grep -v grep
```

- [ ] **Step 2: Verify Chrome launched**

```bash
lsof -ti :9250 | head -1
curl -s http://localhost:9250/json/version | head -1
```

Expected: PID and Chrome version JSON.

- [ ] **Step 3: Test via Telegram**

1. `/browse https://example.com` — should return Jina markdown instantly
2. `/browse https://x.com/elonmusk` — should use CDP (auth domain)
3. `/chrome_pages` — should list tabs
4. `/chrome_login` — should restart Chrome visible
5. `/chrome_headless` — should return to headless

- [ ] **Step 4: Commit any fixes**

If smoke test reveals issues, fix and commit.
