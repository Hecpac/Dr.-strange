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
