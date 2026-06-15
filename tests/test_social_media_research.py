"""Regression tests for claw_v2.social_media.research_competitor.

The function generates a Playwright script in-process and runs it via
subprocess. We don't drive a real browser here — instead we intercept the
script body before subprocess.run and assert it contains the correct JS
selector logic.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2 import social_media


class ResearchCompetitorScriptTests(unittest.TestCase):
    def test_post_link_filter_matches_real_ig_urls_not_escaped_garbage(self) -> None:
        """Regression: the JS filter used to be `/\\\\/p\\\\/|\\\\/reel\\\\//`
        which after f-string expansion produced `/\\/p\\/|\\/reel\\//` — a JS
        regex matching literal `\\p\\` / `\\reel\\` instead of `/p/` / `/reel/`.
        Every IG profile silently returned zero postLinks."""
        captured: dict[str, str] = {}

        original_write = social_media.Path.write_text  # type: ignore[attr-defined]

        def _capture(self, body, *a, **kw):  # noqa: ANN001
            captured["body"] = body
            return original_write(self, body, *a, **kw)

        with patch.object(social_media.Path, "write_text", _capture):
            with patch.object(
                social_media.subprocess,
                "run",
                return_value=SimpleNamespace(stdout='{"found": false}', stderr="", returncode=0),
            ):
                social_media.research_competitor(handle="anyone", recent_post_count=3)

        body = captured["body"]
        # The fixed filter must contain string-membership checks for the real
        # IG URL segments, not the over-escaped regex form.
        self.assertIn("'/p/'", body, "filter must check for literal '/p/' substring")
        self.assertIn("'/reel/'", body, "filter must check for literal '/reel/' substring")
        # The buggy form must be gone — these patterns would never match a real
        # IG href like https://www.instagram.com/p/ABC/.
        self.assertNotIn(r"/\\/p\\/", body, "over-escaped regex still present")
        self.assertNotIn(r"/\\\\/p\\\\/", body, "double-escaped regex still present")


class SocialCompetitorResearchToolTests(unittest.TestCase):
    """Echo SKILL doc tells the brain to pass cdp_url=... so the tool surface
    must accept and forward it."""

    def _get_tool(self):
        from pathlib import Path
        from claw_v2.tools import ToolRegistry

        registry = ToolRegistry.default(workspace_root=Path("/tmp"))
        return registry.get("SocialCompetitorResearch")

    def test_schema_declares_cdp_url_property(self) -> None:
        tool = self._get_tool()
        props = tool.parameter_schema.get("properties", {})
        self.assertIn("cdp_url", props, "cdp_url missing from tool schema")
        self.assertEqual(props["cdp_url"].get("type"), "string")

    def test_handler_forwards_cdp_url_to_research_competitor(self) -> None:
        tool = self._get_tool()
        captured: dict = {}

        def _fake_research(handle, recent_post_count=6, cdp_url="http://localhost:9250"):
            captured["handle"] = handle
            captured["recent_post_count"] = recent_post_count
            captured["cdp_url"] = cdp_url
            from claw_v2.social_media import CompetitorResearch

            return CompetitorResearch(handle=handle, url="x", found=False)

        with patch("claw_v2.social_media.research_competitor", _fake_research):
            tool.handler(
                {
                    "handle": "garyvee",
                    "recent_post_count": 4,
                    "cdp_url": "http://localhost:9333",
                }
            )

        self.assertEqual(captured["handle"], "garyvee")
        self.assertEqual(captured["recent_post_count"], 4)
        self.assertEqual(captured["cdp_url"], "http://localhost:9333")

    def test_handler_omits_cdp_url_when_not_supplied(self) -> None:
        """Default behavior must be preserved when the brain doesn't pass cdp_url."""
        tool = self._get_tool()
        captured: dict = {}

        def _fake_research(handle, recent_post_count=6, cdp_url="http://localhost:9250"):
            captured["cdp_url"] = cdp_url
            from claw_v2.social_media import CompetitorResearch

            return CompetitorResearch(handle=handle, url="x", found=False)

        with patch("claw_v2.social_media.research_competitor", _fake_research):
            tool.handler({"handle": "anyone"})

        self.assertEqual(captured["cdp_url"], "http://localhost:9250")


if __name__ == "__main__":
    unittest.main()
