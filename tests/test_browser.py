from __future__ import annotations

import json
import unittest

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
