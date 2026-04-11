from __future__ import annotations

import json
import unittest.mock as mock
import unittest

from claw_v2 import browser_cli
from claw_v2.browser import BrowserError, BrowseResult, DevBrowserService, ScriptResult, _js_escape


def _make_runner(stdout: str = "", stderr: str = "", return_code: int = 0):
    calls = []

    def runner(cmd, stdin, env, timeout):
        calls.append({"cmd": cmd, "stdin": stdin, "env": env, "timeout": timeout})
        return ScriptResult(stdout=stdout, stderr=stderr, return_code=return_code)

    return runner, calls


class TestDevBrowserService(unittest.TestCase):
    def test_browse_parses_json_output(self) -> None:
        payload = json.dumps({"url": "https://example.com/", "title": "Example", "content": "- heading"})
        runner, _ = _make_runner(stdout=payload)
        svc = DevBrowserService(command_runner=runner)
        result = svc.browse("https://example.com")
        self.assertIsInstance(result, BrowseResult)
        self.assertEqual(result.url, "https://example.com/")
        self.assertEqual(result.title, "Example")
        self.assertEqual(result.content, "- heading")

    def test_browse_raises_on_failure(self) -> None:
        runner, _ = _make_runner(stderr="crash", return_code=1)
        svc = DevBrowserService(command_runner=runner)
        with self.assertRaises(BrowserError):
            svc.browse("https://example.com")

    def test_browse_raises_on_invalid_json(self) -> None:
        runner, _ = _make_runner(stdout="not json")
        svc = DevBrowserService(command_runner=runner)
        with self.assertRaises(BrowserError):
            svc.browse("https://example.com")

    def test_run_script_passes_correct_flags(self) -> None:
        runner, calls = _make_runner()
        svc = DevBrowserService(
            dev_browser_path="/usr/local/bin/dev-browser",
            browsers_path="/custom/path",
            timeout=15,
            headless=True,
            command_runner=runner,
        )
        svc.run_script("console.log('hi')", browser_name="test-browser")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertIn("--headless", call["cmd"])
        self.assertIn("--browser", call["cmd"])
        self.assertIn("test-browser", call["cmd"])
        self.assertIn("--timeout", call["cmd"])
        self.assertIn("15", call["cmd"])
        self.assertEqual(call["env"]["PLAYWRIGHT_BROWSERS_PATH"], "/custom/path")
        self.assertEqual(call["timeout"], 20)  # 15 + 5

    def test_js_escape_prevents_injection(self) -> None:
        self.assertEqual(_js_escape('http://x.com/a"b'), 'http://x.com/a\\"b')
        self.assertEqual(_js_escape("line1\nline2"), "line1\\nline2")
        self.assertEqual(_js_escape("back\\slash"), "back\\\\slash")

    def test_screenshot_returns_path(self) -> None:
        payload = json.dumps({
            "url": "https://example.com/",
            "title": "Example",
            "content": "snapshot",
            "screenshot_path": "/tmp/screenshot.png",
        })
        runner, _ = _make_runner(stdout=payload)
        svc = DevBrowserService(command_runner=runner)
        result = svc.screenshot("https://example.com")
        self.assertEqual(result.screenshot_path, "/tmp/screenshot.png")

    def test_headless_flag_omitted_when_disabled(self) -> None:
        runner, calls = _make_runner()
        svc = DevBrowserService(headless=False, command_runner=runner)
        svc.run_script("console.log('hi')")
        self.assertNotIn("--headless", calls[0]["cmd"])

    def test_interact_runs_structured_actions(self) -> None:
        payload = json.dumps({
            "url": "https://example.com/login",
            "title": "Login",
            "content": "form snapshot",
            "screenshot_path": "/tmp/form.png",
        })
        runner, calls = _make_runner(stdout=payload)
        svc = DevBrowserService(command_runner=runner)
        result = svc.interact(
            "https://example.com/login",
            actions=[
                {"type": "fill", "label": "Email", "value": "hector@example.com"},
                {"type": "click", "role": "button", "name": "Continue"},
                {"type": "screenshot", "name": "login.png"},
            ],
        )
        self.assertEqual(result.url, "https://example.com/login")
        self.assertEqual(result.screenshot_path, "/tmp/form.png")
        self.assertEqual(len(calls), 1)
        script = calls[0]["stdin"]
        self.assertIn('const actions = JSON.parse(', script)
        self.assertIn('\\"type\\": \\"fill\\"', script)
        self.assertIn('\\"label\\": \\"Email\\"', script)
        self.assertIn('\\"type\\": \\"click\\"', script)
        self.assertIn('\\"type\\": \\"screenshot\\"', script)


class TestBrowserCli(unittest.TestCase):
    def test_main_reads_json_payload_and_prints_result(self) -> None:
        with mock.patch("claw_v2.browser_cli.DevBrowserService") as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.interact.return_value = BrowseResult(
                url="https://example.com/form",
                title="Example",
                content="snapshot",
                screenshot_path="/tmp/example.png",
            )
            with mock.patch("sys.stdout.write") as mock_stdout:
                exit_code = browser_cli.main([
                    json.dumps({
                        "url": "https://example.com/form",
                        "actions": [{"type": "click", "selector": "button"}],
                    })
                ])
        self.assertEqual(exit_code, 0)
        mock_service.interact.assert_called_once()
        written = "".join(call.args[0] for call in mock_stdout.call_args_list)
        self.assertIn('"url": "https://example.com/form"', written)


class TestChromeCDP(unittest.TestCase):
    def test_connect_to_chrome_requires_playwright_dependency(self) -> None:
        with mock.patch("claw_v2.browser.sync_playwright", None):
            svc = DevBrowserService()
            with self.assertRaises(BrowserError):
                svc.connect_to_chrome()

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
        mock_new_page.content.return_value = "Campaign overview: ..."

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
        mock_new_page.goto.assert_called_once_with("https://ads.google.com", wait_until="domcontentloaded", timeout=30_000)

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
        mock_existing.goto.assert_called_once_with("https://ads.google.com", wait_until="domcontentloaded", timeout=30_000)

    def test_chrome_screenshot_returns_path(self) -> None:
        mock_page = mock.MagicMock()
        mock_page.url = "https://example.com"
        mock_page.title.return_value = "Example"
        mock_page.content.return_value = "page content"

        mock_context = mock.MagicMock()
        mock_context.pages = [mock_page]

        mock_browser = mock.MagicMock()
        mock_browser.contexts = [mock_context]

        with mock.patch("claw_v2.browser.sync_playwright") as mock_pw:
            mock_pw.return_value.__enter__ = mock.MagicMock(return_value=mock_pw.return_value)
            mock_pw.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pw.return_value.chromium.connect_over_cdp.return_value = mock_browser

            svc = DevBrowserService()
            result = svc.chrome_screenshot(cdp_url="http://localhost:9222", page_index=0)

        self.assertEqual(result.url, "https://example.com")
        self.assertIsNotNone(result.screenshot_path)
        mock_page.screenshot.assert_called_once()


class TestBrowserbaseCDP(unittest.TestCase):
    @mock.patch("httpx.post")
    def test_browserbase_browse_creates_and_releases_session(self, mock_post) -> None:
        create_response = mock.MagicMock()
        create_response.json.return_value = {
            "id": "sess_123",
            "connectUrl": "wss://connect.browserbase.example/devtools/browser/abc",
        }
        create_response.raise_for_status.return_value = None
        release_response = mock.MagicMock()
        release_response.raise_for_status.return_value = None
        mock_post.side_effect = [create_response, release_response]

        mock_body = mock.MagicMock()
        mock_body.inner_text.return_value = "page content"
        mock_page = mock.MagicMock()
        mock_page.url = "https://example.com"
        mock_page.title.return_value = "Example"
        mock_page.query_selector.return_value = mock_body

        mock_context = mock.MagicMock()
        mock_context.pages = [mock_page]

        mock_browser = mock.MagicMock()
        mock_browser.contexts = [mock_context]

        with mock.patch("claw_v2.browser.sync_playwright") as mock_pw:
            mock_pw.return_value.__enter__ = mock.MagicMock(return_value=mock_pw.return_value)
            mock_pw.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pw.return_value.chromium.connect_over_cdp.return_value = mock_browser

            svc = DevBrowserService()
            result = svc.browserbase_browse(
                "https://example.com",
                api_key="bb-key",
                project_id="proj-123",
            )

        self.assertEqual(result.title, "Example")
        self.assertEqual(mock_post.call_count, 2)
        create_call = mock_post.call_args_list[0]
        self.assertEqual(create_call.kwargs["headers"]["X-BB-API-Key"], "bb-key")
        self.assertEqual(create_call.kwargs["json"]["projectId"], "proj-123")
        release_call = mock_post.call_args_list[1]
        self.assertEqual(release_call.kwargs["json"]["status"], "REQUEST_RELEASE")
