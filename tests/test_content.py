from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.content import ContentEngine, PostDraft

STRATEGY = """# test — Social Strategy

## Platforms
- x: @testaccount
- linkedin: company/test

## Content Pillars
1. Tech tips
2. Product updates

## Tone
Casual and direct. English only.

## Cadence
- x: 3 posts/week
- linkedin: 2 posts/week
"""


class GenerateSingleTests(unittest.TestCase):
    def test_generates_post_for_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="Great tech tip! #coding #dev")
            engine = ContentEngine(router=router, accounts_root=root)
            draft = engine.generate_single("testaccount", "x")
            self.assertIsInstance(draft, PostDraft)
            self.assertEqual(draft.account, "testaccount")
            self.assertEqual(draft.platform, "x")
            self.assertTrue(len(draft.text) <= 280)
            router.ask.assert_called_once()

    def test_enforces_x_char_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="a" * 500)
            engine = ContentEngine(router=router, accounts_root=root)
            draft = engine.generate_single("testaccount", "x")
            self.assertTrue(len(draft.text) <= 280)

    def test_generates_with_topic_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="Post about AI trends")
            engine = ContentEngine(router=router, accounts_root=root)
            draft = engine.generate_single("testaccount", "linkedin", topic="AI trends")
            self.assertEqual(draft.platform, "linkedin")
            prompt = router.ask.call_args.args[0]
            self.assertIn("AI trends", prompt)


class GenerateBatchTests(unittest.TestCase):
    def test_generates_posts_for_all_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            router = MagicMock()
            router.ask.return_value = MagicMock(content="A great post")
            engine = ContentEngine(router=router, accounts_root=root)
            drafts = engine.generate_batch("testaccount")
            platforms = {d.platform for d in drafts}
            self.assertEqual(platforms, {"x", "linkedin"})


class ParseStrategyTests(unittest.TestCase):
    def test_extracts_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            acct = root / "testaccount"
            acct.mkdir()
            (acct / "strategy.md").write_text(STRATEGY)
            engine = ContentEngine(router=MagicMock(), accounts_root=root)
            strategy = engine._load_strategy("testaccount")
            self.assertIn("x", strategy["platforms"])
            self.assertIn("linkedin", strategy["platforms"])


if __name__ == "__main__":
    unittest.main()
