from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import (
    _format_chrome_cdp_error,
    _is_tweet_url,
    _is_usable_browse_content,
    _normalize_url,
    _tweet_fxtwitter_read,
)


class ChromeHandler:
    def __init__(
        self,
        browser: Any | None = None,
        capability_check: Callable[[str, str], str | None] | None = None,
        remember_url: Callable[[str | None, str], None] | None = None,
    ) -> None:
        self.browser = browser
        self.managed_chrome: Any | None = None
        self._check_capability = capability_check or (lambda name, fallback: None)
        self._remember_url = remember_url or (lambda sid, url: None)

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "chrome",
                self.handle_command,
                exact=("/chrome_pages", "/chrome_browse", "/chrome_login", "/chrome_headless", "/chrome_download"),
                prefixes=("/chrome_browse ", "/chrome_shot", "/chrome_download "),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/chrome_pages":
            return self.pages_response()
        if stripped == "/chrome_browse":
            return "usage: /chrome_browse <url>"
        if stripped.startswith("/chrome_browse "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /chrome_browse <url>"
            return self.browse_response(parts[1], session_id=context.session_id)
        if stripped.startswith("/chrome_shot"):
            return self.shot_response(stripped)
        if stripped == "/chrome_login":
            return self.login_response()
        if stripped.startswith("/chrome_download"):
            return self.download_response(stripped)
        return self.headless_response()

    def pages_response(self) -> str:
        degraded = self._check_capability("chrome_cdp", "Chrome no disponible.")
        if degraded is not None:
            return degraded
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            pages = self.browser.connect_to_chrome(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome CDP error")
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)

    def browse_response(self, url: str, *, session_id: str | None = None) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        self._remember_url(session_id, normalized_url)
        degraded = self._check_capability("chrome_cdp", "Chrome no disponible.")
        if degraded is not None:
            return degraded
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        tweet_fallback = _tweet_fxtwitter_read(normalized_url) if _is_tweet_url(normalized_url) else ""
        try:
            host = urlparse(normalized_url).netloc.lower()
            result = self.browser.chrome_navigate(
                normalized_url,
                cdp_url=self.managed_chrome.cdp_url,
                page_url_pattern=host,
            )
        except Exception as exc:
            if tweet_fallback:
                return tweet_fallback[:6000]
            return _format_chrome_cdp_error(exc, prefix="chrome browse error")
        if not _is_usable_browse_content(normalized_url, result.content) and tweet_fallback:
            return tweet_fallback[:6000]
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def shot_response(self, command: str) -> str:
        degraded = self._check_capability("chrome_cdp", "Chrome no disponible.")
        if degraded is not None:
            return degraded
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            result = self.browser.chrome_screenshot(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome screenshot error")
        return json.dumps({
            "url": result.url,
            "title": result.title,
            "screenshot_path": result.screenshot_path,
        }, indent=2)

    def login_response(self) -> str:
        degraded = self._check_capability("chrome_cdp", "Chrome no disponible.")
        if degraded is not None:
            return degraded
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=False)
            return "Chrome reiniciado en modo visible. Haz login en los sitios que necesites. Cuando termines: /chrome_headless"
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"

    def download_response(self, command: str) -> str:
        """Wait for a file download triggered via CDP and return its path."""
        from claw_v2.browser import DevBrowserService, _CDP_DOWNLOAD_DIR
        import glob

        parts = command.split(maxsplit=1)
        ext = parts[1].strip() if len(parts) > 1 else None
        if ext and not ext.startswith("."):
            ext = f".{ext}"

        path = DevBrowserService.chrome_download_wait(extension=ext, timeout=30)
        if path:
            return json.dumps({"downloaded": path, "size": __import__("os").path.getsize(path)})

        existing = sorted(glob.glob(f"{_CDP_DOWNLOAD_DIR}/*"), key=__import__("os").path.getmtime, reverse=True)
        if existing:
            latest = existing[0]
            return json.dumps({"no_new_download": True, "latest_existing": latest, "size": __import__("os").path.getsize(latest)})
        return json.dumps({"error": "No download detected in 30s", "download_dir": _CDP_DOWNLOAD_DIR})

    def headless_response(self) -> str:
        degraded = self._check_capability("chrome_cdp", "Chrome no disponible.")
        if degraded is not None:
            return degraded
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=True)
            return "Chrome reiniciado en modo headless."
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"
