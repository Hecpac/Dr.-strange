from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.bot import _extract_url_candidate, _is_login_wall
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
    def test_url_extractor_handles_querystrings_and_local_ips(self) -> None:
        self.assertEqual(_extract_url_candidate("revisa example.com?q=ai"), "example.com?q=ai")
        self.assertEqual(_extract_url_candidate("127.0.0.1:3000/dashboard"), "127.0.0.1:3000/dashboard")

    def test_short_content_is_not_treated_as_login_wall_without_signals(self) -> None:
        self.assertFalse(_is_login_wall("# OK\n\nDocumento corto pero valido."))

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
        bot.config.browse_backend = "chrome_cdp"
        bot.managed_chrome = chrome
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://x.com/post/123")
        mock_jina.assert_not_called()
        self.assertIn("error", result.lower())

    @patch("claw_v2.bot._tweet_fxtwitter_read")
    @patch("claw_v2.bot._jina_read")
    def test_browse_tweet_login_wall_falls_back_to_tweet_reader(self, mock_jina, mock_tweet_read) -> None:
        tweet_url = "https://x.com/tendenciatuits/status/2039116558836936982?s=46"
        mock_tweet_read.return_value = f"**Autor on X** ({tweet_url})\n\nTexto limpio del tweet."
        browser = MagicMock()
        browser.chrome_navigate.return_value = BrowseResult(
            url=tweet_url,
            title="X",
            content="Don't miss what's happening\nLog in\nSign up\nSee new posts " + "x" * 200,
        )
        chrome = MagicMock()
        chrome.cdp_url = "http://localhost:9250"
        bot = _make_bot(browser=browser)
        bot.managed_chrome = chrome

        result = bot.handle_text(user_id="123", session_id="s1", text=f"/browse {tweet_url}")

        mock_jina.assert_not_called()
        browser.chrome_navigate.assert_called_once()
        self.assertIn("Texto limpio del tweet", result)

    @patch("claw_v2.bot._jina_read")
    def test_browse_jina_short_valid_content_is_accepted(self, mock_jina) -> None:
        mock_jina.return_value = "# OK\n\nDocumento corto pero valido."
        bot = _make_bot()
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://example.com/short")
        self.assertIn("Documento corto pero valido", result)

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
        bot.config.browse_backend = "chrome_cdp"
        bot.managed_chrome = chrome
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://example.com")
        self.assertIn("Example", result)
        browser.chrome_navigate.assert_called_once()

    @patch("claw_v2.bot._jina_read")
    def test_browse_playwright_local_backend_uses_browser_browse_before_cdp(self, mock_jina) -> None:
        mock_jina.return_value = ""
        browser = MagicMock()
        browser.browse.return_value = BrowseResult(
            url="https://example.com/docs",
            title="Docs",
            content="Playwright local content " + "x" * 200,
        )
        bot = _make_bot(browser=browser)
        bot.config.browse_backend = "playwright_local"
        bot.managed_chrome = None

        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://example.com/docs")

        self.assertIn("Docs", result)
        browser.browse.assert_called_once_with("https://example.com/docs")
        browser.chrome_navigate.assert_not_called()

    @patch("claw_v2.bot._jina_read")
    def test_browse_browserbase_backend_uses_remote_session(self, mock_jina) -> None:
        mock_jina.return_value = ""
        browser = MagicMock()
        browser.browserbase_browse.return_value = BrowseResult(
            url="https://example.com/pricing",
            title="Pricing",
            content="Browserbase remote content " + "x" * 200,
        )
        bot = _make_bot(browser=browser)
        bot.config.browse_backend = "browserbase_cdp"
        bot.config.browserbase_api_key = "bb-key"
        bot.config.browserbase_project_id = "proj-123"
        bot.managed_chrome = None

        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://example.com/pricing")

        self.assertIn("Pricing", result)
        browser.browserbase_browse.assert_called_once_with(
            "https://example.com/pricing",
            api_key="bb-key",
            project_id="proj-123",
            api_url="https://api.browserbase.com",
            region=None,
            keep_alive=False,
        )

    @patch("claw_v2.bot._jina_read")
    def test_browse_no_chrome_jina_only(self, mock_jina) -> None:
        """When managed_chrome is None, all URLs go through Jina best-effort."""
        mock_jina.return_value = "# Some Content\n\n" + "x" * 200
        bot = _make_bot()
        bot.managed_chrome = None
        result = bot.handle_text(user_id="123", session_id="s1", text="/browse https://x.com/post/123")
        mock_jina.assert_called_once()
        self.assertIn("Some Content", result)

    @patch("claw_v2.bot._jina_read")
    def test_natural_language_bare_host_with_query_uses_browse(self, mock_jina) -> None:
        mock_jina.return_value = "# Search\n\nResultados utiles." + "x" * 40
        bot = _make_bot()
        result = bot.handle_text(user_id="123", session_id="s1", text="revisa example.com?q=ai")
        self.assertIn("Search", result)
        mock_jina.assert_called_once_with("https://example.com?q=ai")

    @patch("claw_v2.bot._jina_read")
    def test_standalone_localhost_url_is_detected(self, mock_jina) -> None:
        mock_jina.return_value = "# Local App\n\nDashboard local." + "x" * 40
        bot = _make_bot()
        result = bot.handle_text(user_id="123", session_id="s1", text="127.0.0.1:3000/dashboard")
        self.assertIn("Local App", result)
        mock_jina.assert_called_once_with("https://127.0.0.1:3000/dashboard")


if __name__ == "__main__":
    unittest.main()
