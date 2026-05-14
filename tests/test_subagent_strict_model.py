"""Wave 3.6: strict mode for SOUL.md model parsing.

Default (non-strict) preserves the legacy fallback to claude-sonnet-4-6 for
backward compat with existing subagent specs. CLAW_STRICT_SOUL_MODEL=1
opts in to raising :class:`SubAgentConfigError` when a SOUL.md cannot be
parsed cleanly — typos in subagent specs no longer silently dispatch to
the wrong model.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.agents import FileAgentStore, SubAgentConfigError, SubAgentService


class ParseModelFromSoulTests(unittest.TestCase):
    def test_explicit_model_line_is_parsed(self) -> None:
        # Parser keys off lowercased keywords like "claude opus" (with space);
        # the canonical SOUL.md phrasing in production matches that.
        soul = "# Title\n- **Model:** Claude Opus 4.7 — runs the brain lane.\n"
        provider, model = SubAgentService._parse_model_from_soul(soul)
        self.assertEqual(provider, "anthropic")
        self.assertEqual(model, "claude-opus-4-7")

    def test_keyword_in_body_works_when_model_line_missing(self) -> None:
        soul = "# Title\n\nThis agent uses Claude Opus for synthesis.\n"
        provider, model = SubAgentService._parse_model_from_soul(soul)
        self.assertEqual(provider, "anthropic")
        self.assertEqual(model, "claude-opus-4-7")

    def test_silent_default_when_no_match_in_non_strict_mode(self) -> None:
        soul = "# Title\n\nNo model info here.\n"
        provider, model = SubAgentService._parse_model_from_soul(soul, strict=False)
        self.assertEqual(provider, "anthropic")
        self.assertEqual(model, "claude-sonnet-4-6")

    def test_strict_mode_raises_when_no_model_line_and_no_keyword_match(self) -> None:
        soul = "# Title\n\nNo model info here.\n"
        with self.assertRaises(SubAgentConfigError) as ctx:
            SubAgentService._parse_model_from_soul(soul, strict=True)
        self.assertIn("SOUL.md", str(ctx.exception))
        self.assertIn("- **Model:**", str(ctx.exception))

    def test_strict_mode_passes_when_model_line_present(self) -> None:
        soul = "# Title\n- **Model:** Claude Opus 4.7\n"
        provider, model = SubAgentService._parse_model_from_soul(soul, strict=True)
        self.assertEqual(provider, "anthropic")

    def test_strict_mode_passes_when_keyword_in_body(self) -> None:
        soul = "# Title\n\nThis agent runs on Codex CLI.\n"
        provider, model = SubAgentService._parse_model_from_soul(soul, strict=True)
        self.assertEqual(provider, "codex")


class LoadDefinitionStrictModeTests(unittest.TestCase):
    def _write_subagent(self, root: Path, *, name: str, soul: str) -> Path:
        agent_dir = root / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "SOUL.md").write_text(soul, encoding="utf-8")
        return agent_dir

    def test_load_succeeds_with_strict_off_when_model_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_subagent(root, name="zeta", soul="# title only\n")
            store = FileAgentStore(root / "store")
            with patch.dict(os.environ, {"CLAW_STRICT_SOUL_MODEL": "0"}, clear=False):
                service = SubAgentService(definitions_root=root, router=None, store=store)
                names = service.discover()
                self.assertIn("zeta", names)

    def test_load_raises_with_strict_on_when_model_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_subagent(root, name="omega", soul="# title only\n")
            store = FileAgentStore(root / "store")
            with patch.dict(os.environ, {"CLAW_STRICT_SOUL_MODEL": "1"}, clear=False):
                service = SubAgentService(definitions_root=root, router=None, store=store)
                with self.assertRaises(SubAgentConfigError) as ctx:
                    service.discover()
                self.assertIn("omega", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
