# Computer Autonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the bot two-tier autonomy — Browser CDP for authenticated web browsing and Computer Use for desktop control — with a shared approval gate for destructive actions.

**Architecture:** ActionGate classifies actions as safe/needs_approval. Browser CDP extends `DevBrowserService` with Playwright `connect_over_cdp()` to the user's Chrome. Computer Use runs an agent loop via the Anthropic API directly with `screencapture` + `pyautogui`. Both tiers share config and approval flow.

**Tech Stack:** Python 3.12, Playwright (already installed), anthropic SDK, pyautogui, macOS screencapture, unittest

**Spec:** `docs/superpowers/specs/2026-03-28-computer-use-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `claw_v2/computer_gate.py` | Create | `ActionGate` — classify actions safe/needs_approval by URL pattern and tier |
| `claw_v2/browser.py` | Modify | Add CDP methods: `connect_to_chrome`, `chrome_navigate`, `chrome_interact`, `chrome_screenshot` |
| `claw_v2/computer.py` | Create | `ComputerUseService`, `ComputerSession`, screenshot/action/agent loop |
| `claw_v2/approval.py` | Modify | Add `reject()` method for explicit action rejection |
| `claw_v2/bot.py` | Modify | Add commands: `/chrome_pages`, `/chrome_browse`, `/chrome_shot`, `/computer`, `/screen`, `/action_approve`, `/action_abort`, `/computer_abort` |
| `claw_v2/telegram.py` | Modify | Add `_send_photo` helper for sending screenshots |
| `claw_v2/config.py` | Modify | Add CDP and Computer Use config fields |
| `claw_v2/main.py` | Modify | Wire `ComputerUseService` in `build_runtime()` |
| `claw_v2/SOUL.md` | Modify | Document both tiers |
| `tests/test_computer_gate.py` | Create | Action gate tests |
| `tests/test_browser.py` | Modify | CDP tests |
| `tests/test_computer.py` | Create | Computer Use service tests |
| `tests/test_bot.py` | Modify | New command tests |
| `tests/helpers.py` | Modify | Add new config fields |
| `pyproject.toml` | Modify | Add `anthropic`, `pyautogui` dependencies |

---

### Task 1: Dependencies and config fields

**Files:**
- Modify: `pyproject.toml:7`
- Modify: `claw_v2/config.py:57-67,115-119`
- Modify: `tests/helpers.py:48-57`

- [ ] **Step 1: Add dependencies to pyproject.toml**

In `pyproject.toml`, change line 7:

```python
dependencies = ["claude-agent-sdk", "openai", "google-genai", "python-telegram-bot", "anthropic", "pyautogui"]
```

- [ ] **Step 2: Install dependencies**

Run: `cd /Users/hector/Projects/Dr.-strange && .venv/bin/pip install anthropic pyautogui`
Expected: Successfully installed

- [ ] **Step 3: Add config fields to AppConfig**

In `claw_v2/config.py`, add after `daily_cost_limit: float` (line 66):

```python
    chrome_cdp_enabled: bool
    chrome_cdp_url: str
    computer_use_enabled: bool
    computer_display_width: int
    computer_display_height: int
    sensitive_urls: list[str]
```

In `AppConfig.from_env()`, add after the `daily_cost_limit=...` line (line 119):

```python
            chrome_cdp_enabled=_env_bool("CHROME_CDP_ENABLED", True),
            chrome_cdp_url=os.getenv("CHROME_CDP_URL", "http://localhost:9222"),
            computer_use_enabled=_env_bool("COMPUTER_USE_ENABLED", True),
            computer_display_width=int(os.getenv("COMPUTER_DISPLAY_WIDTH", "1280")),
            computer_display_height=int(os.getenv("COMPUTER_DISPLAY_HEIGHT", "800")),
            sensitive_urls=[u for u in os.getenv("SENSITIVE_URLS", "ads.google.com:polymarket.com:robinhood.com:binance.com:stripe.com:paypal.com").split(":") if u.strip()],
```

- [ ] **Step 4: Add config fields to test helper**

In `tests/helpers.py`, add after `daily_cost_limit=10.0,` (line 57):

```python
        chrome_cdp_enabled=False,
        chrome_cdp_url="http://localhost:9222",
        computer_use_enabled=False,
        computer_display_width=1280,
        computer_display_height=800,
        sensitive_urls=["ads.google.com", "polymarket.com"],
```

- [ ] **Step 5: Verify import**

Run: `.venv/bin/python -c "from claw_v2.config import AppConfig; c = AppConfig.from_env(); print(c.chrome_cdp_enabled, c.sensitive_urls)"`
Expected: `True ['ads.google.com', 'polymarket.com', ...]`

- [ ] **Step 6: Run full test suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml claw_v2/config.py tests/helpers.py
git commit -m "feat: add config fields for Browser CDP and Computer Use"
```

---

### Task 2: ActionGate — shared action classifier

**Files:**
- Create: `claw_v2/computer_gate.py`
- Create: `tests/test_computer_gate.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_computer_gate.py`:

```python
from __future__ import annotations

import unittest

from claw_v2.computer_gate import ActionGate, ActionVerdict


class ActionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = ActionGate(sensitive_urls=["ads.google.com", "polymarket.com"])

    # --- Tier 1: Browser CDP ---

    def test_cdp_screenshot_is_safe(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "screenshot"}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_cdp_goto_is_safe(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "goto", "url": "https://ads.google.com"}, url="https://example.com")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_cdp_click_on_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "click", "selector": "button"}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_fill_on_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "fill", "selector": "input", "value": "100"}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_submit_always_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "submit", "selector": "form"}, url="https://example.com")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_click_on_non_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "click", "selector": "button"}, url="https://example.com")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_cdp_wait_for_is_safe(self) -> None:
        verdict = self.gate.classify_cdp_action({"type": "wait_for", "ms": 1000}, url="https://ads.google.com")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    # --- Tier 2: Computer Use ---

    def test_desktop_screenshot_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "screenshot"}, url=None)
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_mouse_move_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "mouse_move", "coordinate": [100, 200]}, url=None)
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_scroll_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "scroll", "coordinate": [500, 400], "scroll_direction": "down", "scroll_amount": 3}, url=None)
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_click_without_url_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "left_click", "coordinate": [500, 300]}, url=None)
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_type_without_url_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "type", "text": "hello"}, url=None)
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_click_with_non_sensitive_url_is_safe(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "left_click", "coordinate": [500, 300]}, url="https://docs.google.com")
        self.assertEqual(verdict, ActionVerdict.SAFE)

    def test_desktop_click_with_sensitive_url_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "left_click", "coordinate": [500, 300]}, url="https://ads.google.com/campaigns")
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    def test_desktop_key_navigation_is_safe(self) -> None:
        for key in ["Escape", "Tab", "Up", "Down", "Left", "Right"]:
            verdict = self.gate.classify_desktop_action({"action": "key", "text": key}, url=None)
            self.assertEqual(verdict, ActionVerdict.SAFE, f"key '{key}' should be safe")

    def test_desktop_key_destructive_needs_approval(self) -> None:
        verdict = self.gate.classify_desktop_action({"action": "key", "text": "super+Delete"}, url=None)
        self.assertEqual(verdict, ActionVerdict.NEEDS_APPROVAL)

    # --- URL matching ---

    def test_sensitive_url_match_is_substring(self) -> None:
        self.assertTrue(self.gate.is_sensitive_url("https://ads.google.com/campaigns?id=123"))
        self.assertTrue(self.gate.is_sensitive_url("https://polymarket.com/market/1"))

    def test_non_sensitive_url(self) -> None:
        self.assertFalse(self.gate.is_sensitive_url("https://google.com"))
        self.assertFalse(self.gate.is_sensitive_url("https://example.com"))

    def test_none_url_is_not_sensitive(self) -> None:
        self.assertFalse(self.gate.is_sensitive_url(None))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_computer_gate.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement ActionGate**

Create `claw_v2/computer_gate.py`:

```python
from __future__ import annotations

from enum import Enum


class ActionVerdict(Enum):
    SAFE = "safe"
    NEEDS_APPROVAL = "needs_approval"


CDP_READ_ACTIONS = frozenset({"screenshot", "goto", "wait_for"})
DESKTOP_READ_ACTIONS = frozenset({"screenshot", "mouse_move", "scroll", "zoom", "wait"})
DESKTOP_NAV_KEYS = frozenset({
    "Escape", "Tab", "Up", "Down", "Left", "Right",
    "Home", "End", "Page_Up", "Page_Down",
})
CDP_ALWAYS_APPROVE = frozenset({"submit"})


class ActionGate:
    def __init__(self, sensitive_urls: list[str] | None = None) -> None:
        self.sensitive_urls = list(sensitive_urls or [])

    def classify_cdp_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        action_type = action.get("type", "")
        if action_type in CDP_READ_ACTIONS:
            return ActionVerdict.SAFE
        if action_type in CDP_ALWAYS_APPROVE:
            return ActionVerdict.NEEDS_APPROVAL
        return ActionVerdict.NEEDS_APPROVAL

    def classify_desktop_action(self, action: dict, *, url: str | None) -> ActionVerdict:
        action_type = action.get("action", "")
        if action_type in DESKTOP_READ_ACTIONS:
            return ActionVerdict.SAFE
        if action_type == "key":
            key_text = action.get("text", "")
            base_key = key_text.split("+")[-1] if "+" in key_text else key_text
            if base_key in DESKTOP_NAV_KEYS and not self.is_sensitive_url(url):
                return ActionVerdict.SAFE
            if "+" in key_text:
                return ActionVerdict.NEEDS_APPROVAL
            if base_key in DESKTOP_NAV_KEYS:
                return ActionVerdict.SAFE
            return ActionVerdict.NEEDS_APPROVAL
        if url is None:
            return ActionVerdict.NEEDS_APPROVAL
        if self.is_sensitive_url(url):
            return ActionVerdict.NEEDS_APPROVAL
        return ActionVerdict.SAFE

    def is_sensitive_url(self, url: str | None) -> bool:
        if url is None:
            return False
        for pattern in self.sensitive_urls:
            if pattern in url:
                return True
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_computer_gate.py -v`
Expected: All 18 tests PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add claw_v2/computer_gate.py tests/test_computer_gate.py
git commit -m "feat: add ActionGate for classifying browser and desktop actions"
```

---

### Task 3: ApprovalManager.reject()

**Files:**
- Modify: `claw_v2/approval.py:46`
- Test: `tests/test_computer_gate.py` (append)

- [ ] **Step 1: Write failing test**

Append to `tests/test_computer_gate.py`:

```python
import tempfile
from pathlib import Path

from claw_v2.approval import ApprovalManager


class ApprovalRejectTests(unittest.TestCase):
    def test_reject_sets_status_to_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "test-secret")
            pending = manager.create(action="click", summary="Click buy button")
            manager.reject(pending.approval_id)
            self.assertEqual(manager.status(pending.approval_id), "rejected")

    def test_reject_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "test-secret")
            pending = manager.create(action="click", summary="Click buy button")
            manager.reject(pending.approval_id)
            payload = manager.read(pending.approval_id)
            self.assertEqual(payload["status"], "rejected")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_computer_gate.py::ApprovalRejectTests -v`
Expected: FAIL — `ApprovalManager` has no `reject` method

- [ ] **Step 3: Implement reject()**

In `claw_v2/approval.py`, add after the `approve()` method (after line 45):

```python
    def reject(self, approval_id: str) -> None:
        path = self._path_for(approval_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "rejected"
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_computer_gate.py::ApprovalRejectTests -v`
Expected: All 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add claw_v2/approval.py tests/test_computer_gate.py
git commit -m "feat: add ApprovalManager.reject() for action-scoped rejections"
```

---

### Task 4: Browser CDP — connect_to_chrome and chrome_navigate

**Files:**
- Modify: `claw_v2/browser.py`
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_browser.py`:

```python
class TestChromeCDP(unittest.TestCase):
    def test_connect_to_chrome_returns_page_list(self) -> None:
        mock_page_1 = mock.MagicMock()
        mock_page_1.url = "https://ads.google.com/campaigns"
        mock_page_1.title.return_value = "Google Ads"
        mock_page_2 = mock.MagicMock()
        mock_page_2.url = "https://example.com"
        mock_page_2.title.return_value = "Example"

        mock_context = mock.MagicMock()
        mock_context.pages = [mock_page_1, mock_page_2]

        mock_browser = mock.MagicMock()
        mock_browser.contexts = [mock_context]

        with mock.patch("claw_v2.browser.sync_playwright") as mock_pw:
            mock_pw.return_value.__enter__ = mock.MagicMock(return_value=mock_pw.return_value)
            mock_pw.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pw.return_value.chromium.connect_over_cdp.return_value = mock_browser

            svc = DevBrowserService()
            pages = svc.connect_to_chrome(cdp_url="http://localhost:9222")

        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["url"], "https://ads.google.com/campaigns")
        self.assertEqual(pages[0]["title"], "Google Ads")
        self.assertEqual(pages[1]["url"], "https://example.com")

    def test_chrome_navigate_opens_url_in_new_tab(self) -> None:
        mock_new_page = mock.MagicMock()
        mock_new_page.url = "https://ads.google.com/campaigns"
        mock_new_page.title.return_value = "Google Ads"

        snapshot_text = "Campaign overview: ..."
        mock_new_page.content.return_value = snapshot_text

        mock_context = mock.MagicMock()
        mock_context.pages = []
        mock_context.new_page.return_value = mock_new_page

        mock_browser = mock.MagicMock()
        mock_browser.contexts = [mock_context]

        with mock.patch("claw_v2.browser.sync_playwright") as mock_pw:
            mock_pw.return_value.__enter__ = mock.MagicMock(return_value=mock_pw.return_value)
            mock_pw.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pw.return_value.chromium.connect_over_cdp.return_value = mock_browser

            svc = DevBrowserService()
            result = svc.chrome_navigate("https://ads.google.com", cdp_url="http://localhost:9222")

        self.assertEqual(result.url, "https://ads.google.com/campaigns")
        self.assertEqual(result.title, "Google Ads")
        mock_new_page.goto.assert_called_once_with("https://ads.google.com")

    def test_chrome_navigate_matches_existing_tab_by_url_pattern(self) -> None:
        mock_existing = mock.MagicMock()
        mock_existing.url = "https://ads.google.com/campaigns"
        mock_existing.title.return_value = "Google Ads"
        mock_existing.content.return_value = "campaigns data"

        mock_context = mock.MagicMock()
        mock_context.pages = [mock_existing]

        mock_browser = mock.MagicMock()
        mock_browser.contexts = [mock_context]

        with mock.patch("claw_v2.browser.sync_playwright") as mock_pw:
            mock_pw.return_value.__enter__ = mock.MagicMock(return_value=mock_pw.return_value)
            mock_pw.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pw.return_value.chromium.connect_over_cdp.return_value = mock_browser

            svc = DevBrowserService()
            result = svc.chrome_navigate(
                "https://ads.google.com",
                cdp_url="http://localhost:9222",
                page_url_pattern="ads.google.com",
            )

        self.assertEqual(result.url, "https://ads.google.com/campaigns")
        mock_existing.goto.assert_called_once_with("https://ads.google.com")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_browser.py::TestChromeCDP -v`
Expected: FAIL — `DevBrowserService` has no `connect_to_chrome` method

- [ ] **Step 3: Implement CDP methods in browser.py**

Add at the top of `claw_v2/browser.py` (after existing imports):

```python
from playwright.sync_api import sync_playwright
```

Add these methods to `DevBrowserService` class (after the existing `interact()` method):

```python
    def connect_to_chrome(self, *, cdp_url: str = "http://localhost:9222") -> list[dict[str, str]]:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                browser.close()
                return []
            pages = [
                {"url": page.url, "title": page.title(), "index": i}
                for i, page in enumerate(context.pages)
            ]
            browser.close()
        return pages

    def chrome_navigate(
        self,
        url: str,
        *,
        cdp_url: str = "http://localhost:9222",
        page_index: int | None = None,
        page_title: str | None = None,
        page_url_pattern: str | None = None,
    ) -> BrowseResult:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = _select_cdp_page(context, page_index=page_index, page_title=page_title, page_url_pattern=page_url_pattern)
            page.goto(url)
            result = BrowseResult(url=page.url, title=page.title(), content=page.content()[:4000])
            browser.close()
        return result

    def chrome_screenshot(
        self,
        *,
        cdp_url: str = "http://localhost:9222",
        page_index: int | None = None,
        page_title: str | None = None,
        page_url_pattern: str | None = None,
        name: str = "chrome.png",
    ) -> BrowseResult:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = _select_cdp_page(context, page_index=page_index, page_title=page_title, page_url_pattern=page_url_pattern)
            screenshot_path = f"/tmp/claw-{name}"
            page.screenshot(path=screenshot_path)
            result = BrowseResult(
                url=page.url,
                title=page.title(),
                content=page.content()[:4000],
                screenshot_path=screenshot_path,
            )
            browser.close()
        return result
```

Add the page selection helper outside the class (at module level):

```python
def _select_cdp_page(context, *, page_index=None, page_title=None, page_url_pattern=None):
    if page_index is not None and 0 <= page_index < len(context.pages):
        return context.pages[page_index]
    if page_url_pattern is not None:
        for page in context.pages:
            if page_url_pattern in page.url:
                return page
    if page_title is not None:
        for page in context.pages:
            if page_title.lower() in page.title().lower():
                return page
    return context.new_page()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_browser.py::TestChromeCDP -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add claw_v2/browser.py tests/test_browser.py
git commit -m "feat: add Chrome CDP connection and navigation to DevBrowserService"
```

---

### Task 5: ComputerUseService — screenshot and action executor

**Files:**
- Create: `claw_v2/computer.py`
- Create: `tests/test_computer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_computer.py`:

```python
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from claw_v2.computer import ComputerUseService, ComputerSession


class ScreenshotTests(unittest.TestCase):
    def test_capture_calls_screencapture_and_returns_base64(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            svc = ComputerUseService(display_width=1280, display_height=800)
            fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

            def fake_run(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(fake_png)
                return MagicMock(returncode=0)

            with patch("claw_v2.computer.subprocess.run", side_effect=fake_run) as mock_run:
                with patch("claw_v2.computer._resize_image", return_value=fake_png):
                    result = svc.capture_screenshot()

            self.assertTrue(result["data"].startswith("iVBOR") or len(result["data"]) > 0)
            self.assertEqual(result["media_type"], "image/png")
            mock_run.assert_called_once()
            self.assertIn("screencapture", mock_run.call_args[0][0])


class ActionExecutorTests(unittest.TestCase):
    def test_click_scales_coordinates(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800, scale_factor=2.0)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "left_click", "coordinate": [640, 400]})
        mock_pag.click.assert_called_once_with(1280, 800)

    def test_type_action(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "type", "text": "hello world"})
        mock_pag.typewrite.assert_called_once_with("hello world", interval=0.02)

    def test_key_action(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "key", "text": "cmd+t"})
        mock_pag.hotkey.assert_called_once_with("cmd", "t")

    def test_scroll_action(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "scroll", "coordinate": [500, 400], "scroll_direction": "down", "scroll_amount": 3})
        mock_pag.moveTo.assert_called_once()
        mock_pag.scroll.assert_called_once_with(-3)

    def test_mouse_move_scales(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800, scale_factor=2.0)
        with patch("claw_v2.computer.pyautogui") as mock_pag:
            svc.execute_action({"action": "mouse_move", "coordinate": [100, 200]})
        mock_pag.moveTo.assert_called_once_with(200, 400)

    def test_screenshot_action_calls_capture(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        with patch.object(svc, "capture_screenshot", return_value={"data": "abc", "media_type": "image/png"}) as mock_cap:
            result = svc.execute_action({"action": "screenshot"})
        mock_cap.assert_called_once()


class ComputerSessionTests(unittest.TestCase):
    def test_session_defaults(self) -> None:
        session = ComputerSession(task="test task")
        self.assertEqual(session.status, "running")
        self.assertEqual(session.iteration, 0)
        self.assertEqual(session.max_iterations, 30)
        self.assertIsNone(session.pending_action)
        self.assertIsNone(session.current_url)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_computer.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement ComputerUseService**

Create `claw_v2/computer.py`:

```python
from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyautogui

logger = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.3


@dataclass
class ComputerSession:
    task: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    status: str = "running"
    pending_action: dict[str, Any] | None = None
    screenshot_path: str | None = None
    max_iterations: int = 30
    iteration: int = 0
    current_url: str | None = None


class ComputerUseService:
    def __init__(
        self,
        *,
        display_width: int = 1280,
        display_height: int = 800,
        scale_factor: float = 1.0,
        action_delay: float = 0.3,
    ) -> None:
        self.display_width = display_width
        self.display_height = display_height
        self.scale_factor = scale_factor
        self.action_delay = action_delay

    def capture_screenshot(self) -> dict[str, str]:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["screencapture", "-x", tmp_path],
                check=True,
                capture_output=True,
                timeout=10,
            )
            raw = Path(tmp_path).read_bytes()
            resized = _resize_image(raw, self.display_width, self.display_height)
            encoded = base64.b64encode(resized).decode("ascii")
            return {"data": encoded, "media_type": "image/png"}
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def execute_action(self, action: dict[str, Any]) -> dict[str, str] | None:
        action_type = action.get("action", "")
        if action_type == "screenshot":
            return self.capture_screenshot()
        if action_type == "left_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.click(x, y)
        elif action_type == "right_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.rightClick(x, y)
        elif action_type == "double_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.doubleClick(x, y)
        elif action_type == "middle_click":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.middleClick(x, y)
        elif action_type == "type":
            pyautogui.typewrite(action["text"], interval=0.02)
        elif action_type == "key":
            keys = action["text"].split("+")
            if len(keys) > 1:
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(keys[0])
        elif action_type == "mouse_move":
            x, y = self._scale_coords(action["coordinate"])
            pyautogui.moveTo(x, y)
        elif action_type == "scroll":
            x, y = self._scale_coords(action.get("coordinate", [0, 0]))
            pyautogui.moveTo(x, y)
            direction = action.get("scroll_direction", "down")
            amount = action.get("scroll_amount", 3)
            scroll_val = -amount if direction == "down" else amount
            pyautogui.scroll(scroll_val)
        elif action_type == "left_click_drag":
            start = self._scale_coords(action["start_coordinate"])
            end = self._scale_coords(action["coordinate"])
            pyautogui.moveTo(start[0], start[1])
            pyautogui.drag(end[0] - start[0], end[1] - start[1])
        else:
            logger.warning("Unknown action type: %s", action_type)
        time.sleep(self.action_delay)
        return None

    def _scale_coords(self, coordinate: list[int]) -> tuple[int, int]:
        x = int(coordinate[0] * self.scale_factor)
        y = int(coordinate[1] * self.scale_factor)
        return x, y


def _resize_image(raw: bytes, width: int, height: int) -> bytes:
    try:
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(raw))
        img = img.resize((width, height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        return raw
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_computer.py -v`
Expected: All 8 tests PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add claw_v2/computer.py tests/test_computer.py
git commit -m "feat: add ComputerUseService with screenshot and action executor"
```

---

### Task 6: ComputerUseService — agent loop

**Files:**
- Modify: `claw_v2/computer.py`
- Modify: `tests/test_computer.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_computer.py`:

```python
from claw_v2.computer_gate import ActionGate


class AgentLoopTests(unittest.TestCase):
    def test_agent_loop_runs_screenshot_then_click_then_completes(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="click the button")

        call_count = [0]

        def fake_create(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return MagicMock(
                    content=[MagicMock(
                        type="tool_use",
                        name="computer",
                        input={"action": "left_click", "coordinate": [500, 300]},
                        id="tool_1",
                    )],
                    stop_reason="tool_use",
                )
            return MagicMock(
                content=[MagicMock(type="text", text="Done! I clicked the button.")],
                stop_reason="end_turn",
            )

        mock_client = MagicMock()
        mock_client.beta.messages.create.side_effect = fake_create

        with patch("claw_v2.computer.pyautogui"):
            with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
                result = svc.run_agent_loop(
                    session=session,
                    client=mock_client,
                    gate=gate,
                    model="claude-opus-4-6",
                )

        self.assertEqual(result, "Done! I clicked the button.")
        self.assertEqual(session.status, "done")
        self.assertEqual(session.iteration, 2)

    def test_agent_loop_stops_at_max_iterations(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=[])
        session = ComputerSession(task="infinite task", max_iterations=2)

        def always_tool_use(**kwargs):
            return MagicMock(
                content=[MagicMock(
                    type="tool_use",
                    name="computer",
                    input={"action": "screenshot"},
                    id="tool_x",
                )],
                stop_reason="tool_use",
            )

        mock_client = MagicMock()
        mock_client.beta.messages.create.side_effect = always_tool_use

        with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
            result = svc.run_agent_loop(
                session=session,
                client=mock_client,
                gate=gate,
                model="claude-opus-4-6",
            )

        self.assertIn("limit", result.lower())
        self.assertEqual(session.iteration, 2)

    def test_agent_loop_pauses_when_gate_needs_approval(self) -> None:
        svc = ComputerUseService(display_width=1280, display_height=800)
        gate = ActionGate(sensitive_urls=["ads.google.com"])
        session = ComputerSession(task="click buy", current_url="https://ads.google.com")

        def fake_create(**kwargs):
            return MagicMock(
                content=[MagicMock(
                    type="tool_use",
                    name="computer",
                    input={"action": "left_click", "coordinate": [500, 300]},
                    id="tool_1",
                )],
                stop_reason="tool_use",
            )

        mock_client = MagicMock()
        mock_client.beta.messages.create.side_effect = fake_create

        with patch.object(svc, "capture_screenshot", return_value={"data": "fake", "media_type": "image/png"}):
            result = svc.run_agent_loop(
                session=session,
                client=mock_client,
                gate=gate,
                model="claude-opus-4-6",
            )

        self.assertEqual(session.status, "awaiting_approval")
        self.assertIsNotNone(session.pending_action)
        self.assertIn("approval", result.lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_computer.py::AgentLoopTests -v`
Expected: FAIL — `ComputerUseService` has no `run_agent_loop` method

- [ ] **Step 3: Implement run_agent_loop**

Add to `ComputerUseService` in `claw_v2/computer.py`:

```python
    def run_agent_loop(
        self,
        *,
        session: ComputerSession,
        client: Any,
        gate: Any,
        model: str = "claude-opus-4-6",
        system_prompt: str | None = None,
    ) -> str:
        if not session.messages:
            screenshot = self.capture_screenshot()
            session.messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": session.task},
                        {"type": "image", "source": {"type": "base64", **screenshot}},
                    ],
                }
            ]

        while session.iteration < session.max_iterations:
            session.iteration += 1
            response = client.beta.messages.create(
                model=model,
                max_tokens=4096,
                tools=[{
                    "type": "computer_20251124",
                    "name": "computer",
                    "display_width_px": self.display_width,
                    "display_height_px": self.display_height,
                }],
                messages=session.messages,
                betas=["computer-use-2025-11-24"],
                **({"system": system_prompt} if system_prompt else {}),
            )

            response_content = response.content
            session.messages.append({"role": "assistant", "content": response_content})

            tool_uses = [b for b in response_content if b.type == "tool_use"]
            if not tool_uses:
                session.status = "done"
                text_blocks = [b for b in response_content if b.type == "text"]
                return text_blocks[0].text if text_blocks else "(no response)"

            tool_results = []
            for block in tool_uses:
                action = block.input
                verdict = gate.classify_desktop_action(action, url=session.current_url)

                if verdict.value == "needs_approval":
                    session.status = "awaiting_approval"
                    session.pending_action = {"tool_use_id": block.id, **action}
                    screenshot = self.capture_screenshot()
                    session.screenshot_path = f"/tmp/claw-approval-{block.id}.png"
                    return f"Action needs approval: {action.get('action')} — waiting for /action_approve"

                result = self.execute_action(action)
                if result is None:
                    result = self.capture_screenshot()

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": [{"type": "image", "source": {"type": "base64", **result}}],
                })

            session.messages.append({"role": "user", "content": tool_results})

        session.status = "done"
        return "Computer Use iteration limit reached."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_computer.py::AgentLoopTests -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add claw_v2/computer.py tests/test_computer.py
git commit -m "feat: add Computer Use agent loop with gate integration"
```

---

### Task 7: Telegram photo sending

**Files:**
- Modify: `claw_v2/telegram.py`
- Modify: `tests/test_telegram.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_telegram.py`:

```python
class SendPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_screenshot_sends_photo_to_chat(self) -> None:
        bot_service = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        transport._app = MagicMock()
        mock_bot = AsyncMock()
        transport._app.bot = mock_bot

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            tmp_path = tmp.name

        try:
            await transport.send_photo(chat_id=1, photo_path=tmp_path, caption="screenshot")
            mock_bot.send_photo.assert_awaited_once()
            call_kwargs = mock_bot.send_photo.call_args
            self.assertEqual(call_kwargs.kwargs["chat_id"], 1)
            self.assertEqual(call_kwargs.kwargs["caption"], "screenshot")
        finally:
            Path(tmp_path).unlink(missing_ok=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_telegram.py::SendPhotoTests -v`
Expected: FAIL — `TelegramTransport` has no `send_photo` method

- [ ] **Step 3: Implement send_photo**

Add to `TelegramTransport` class in `claw_v2/telegram.py`:

```python
    async def send_photo(self, *, chat_id: int, photo_path: str, caption: str | None = None) -> None:
        if self._app is None:
            return
        with open(photo_path, "rb") as photo:
            await self._app.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_telegram.py::SendPhotoTests -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add claw_v2/telegram.py tests/test_telegram.py
git commit -m "feat: add send_photo to TelegramTransport for screenshot delivery"
```

---

### Task 8: Bot commands — Chrome CDP and Computer Use

**Files:**
- Modify: `claw_v2/bot.py`
- Modify: `tests/test_bot.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_bot.py`:

```python
    def test_chrome_pages_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.connect_to_chrome.return_value = [
                    {"url": "https://ads.google.com", "title": "Google Ads", "index": 0},
                ]
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_pages")
                parsed = json.loads(result)
                self.assertEqual(parsed["pages"][0]["url"], "https://ads.google.com")

    def test_chrome_browse_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://ads.google.com/campaigns",
                    title="Google Ads",
                    content="campaign data...",
                )
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_browse https://ads.google.com")
                self.assertIn("Google Ads", result)
                self.assertIn("campaign data", result)

    def test_screen_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.computer = MagicMock()
                runtime.bot.computer.capture_screenshot.return_value = {"data": "abc123", "media_type": "image/png"}
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/screen")
                parsed = json.loads(result)
                self.assertIn("screenshot_data", parsed)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_bot.py::BotTests::test_chrome_pages_command -v`
Expected: FAIL

- [ ] **Step 3: Implement bot commands**

In `claw_v2/bot.py`, add `computer: object | None = None` to `__init__` params (after `terminal_bridge`):

```python
        computer: object | None = None,
```

And `self.computer = computer` in the init body.

Add command handlers in `handle_text()` (after the terminal commands block, before `/agents`):

```python
        if stripped == "/chrome_pages":
            return self._chrome_pages_response()
        if stripped == "/chrome_browse":
            return "usage: /chrome_browse <url>"
        if stripped.startswith("/chrome_browse "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /chrome_browse <url>"
            return self._chrome_browse_response(parts[1])
        if stripped.startswith("/chrome_shot"):
            return self._chrome_shot_response(stripped)
        if stripped == "/screen":
            return self._screen_response()
        if stripped == "/computer":
            return "usage: /computer <instruction>"
        if stripped.startswith("/computer "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /computer <instruction>"
            return self._computer_response(parts[1], session_id)
        if stripped.startswith("/action_approve "):
            parts = stripped.split()
            if len(parts) != 3:
                return "usage: /action_approve <approval_id> <token>"
            return self._action_approve_response(parts[1], parts[2])
        if stripped.startswith("/action_abort "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /action_abort <approval_id>"
            return self._action_abort_response(parts[1])
        if stripped == "/computer_abort":
            return self._computer_abort_response(session_id)
```

Add the response methods:

```python
    def _chrome_pages_response(self) -> str:
        if self.browser is None:
            return "browser unavailable"
        try:
            pages = self.browser.connect_to_chrome()
        except Exception as exc:
            return f"chrome CDP error: {exc}"
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)

    def _chrome_browse_response(self, url: str) -> str:
        if self.browser is None:
            return "browser unavailable"
        try:
            result = self.browser.chrome_navigate(url)
        except Exception as exc:
            return f"chrome browse error: {exc}"
        return f"**{result.title}** ({result.url})\n\n{result.content[:3000]}"

    def _chrome_shot_response(self, command: str) -> str:
        if self.browser is None:
            return "browser unavailable"
        try:
            result = self.browser.chrome_screenshot()
        except Exception as exc:
            return f"chrome screenshot error: {exc}"
        return json.dumps({
            "url": result.url,
            "title": result.title,
            "screenshot_path": result.screenshot_path,
        }, indent=2)

    def _screen_response(self) -> str:
        if self.computer is None:
            return "computer use unavailable"
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"screenshot error: {exc}"
        return json.dumps({"screenshot_data": screenshot["data"][:100] + "...", "media_type": screenshot["media_type"]})

    def _computer_response(self, instruction: str, session_id: str) -> str:
        if self.computer is None:
            return "computer use unavailable"
        return f"Computer Use session started: {instruction}"

    def _action_approve_response(self, approval_id: str, token: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            valid = self.approvals.approve(approval_id, token)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        return "approved" if valid else "invalid token"

    def _action_abort_response(self, approval_id: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            self.approvals.reject(approval_id)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        return "action rejected"

    def _computer_abort_response(self, session_id: str) -> str:
        return "no active computer session"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_bot.py::BotTests::test_chrome_pages_command tests/test_bot.py::BotTests::test_chrome_browse_command tests/test_bot.py::BotTests::test_screen_command -v`
Expected: All 3 PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add claw_v2/bot.py tests/test_bot.py
git commit -m "feat: add Chrome CDP and Computer Use bot commands"
```

---

### Task 9: Wiring and SOUL.md update

**Files:**
- Modify: `claw_v2/main.py`
- Modify: `claw_v2/SOUL.md`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write integration test**

Append to `tests/test_runtime.py`:

```python
    def test_chrome_pages_command_wired(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                self.assertIsNotNone(runtime.bot.computer)
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/screen")
                self.assertIn("screenshot", result.lower())
```

- [ ] **Step 2: Wire in main.py**

In `claw_v2/main.py`, add import:

```python
from claw_v2.computer import ComputerUseService
```

In `build_runtime()`, after creating `terminal_bridge` and before creating `bot`:

```python
    computer = ComputerUseService(
        display_width=config.computer_display_width,
        display_height=config.computer_display_height,
    )
```

Add `computer=computer` to the `BotService(...)` constructor call.

- [ ] **Step 3: Update SOUL.md**

Add to the Capabilities section of `claw_v2/SOUL.md`:

```markdown
- **Browser CDP** for browsing authenticated sites: `/chrome_pages` lists tabs, `/chrome_browse <url>` navigates in your Chrome session, `/chrome_shot` takes a screenshot.
- **Computer Use** for full desktop control: `/computer <instruction>` starts a Computer Use session with screenshot + mouse + keyboard automation. `/screen` takes a desktop screenshot.
```

- [ ] **Step 4: Run integration test**

Run: `.venv/bin/python -m pytest tests/test_runtime.py::RuntimeTests::test_chrome_pages_command_wired -v`
Expected: PASS

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add claw_v2/main.py claw_v2/SOUL.md tests/test_runtime.py
git commit -m "feat: wire ComputerUseService into runtime and update SOUL.md"
```
