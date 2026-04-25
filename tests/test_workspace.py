from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.workspace import AgentWorkspace


class AgentWorkspaceTests(unittest.TestCase):
    def test_ensure_creates_required_workspace_files_without_overwriting_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "SOUL.md").write_text("# Custom Soul\n\nHuman edited.", encoding="utf-8")

            workspace = AgentWorkspace(root)
            result = workspace.ensure()

            self.assertIn("SOUL.md", result.existing_files)
            self.assertNotIn("SOUL.md", result.created_files)
            self.assertEqual((root / "SOUL.md").read_text(encoding="utf-8"), "# Custom Soul\n\nHuman edited.")
            for name in AgentWorkspace.REQUIRED_FILES:
                self.assertTrue((root / name).exists(), name)
            self.assertTrue((root / "memory").is_dir())

    def test_stable_context_loads_workspace_files_in_runtime_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = AgentWorkspace(root)
            workspace.ensure()
            (root / "MEMORY.md").write_text("# MEMORY.md\n\nPrefers concise updates.", encoding="utf-8")

            context = workspace.stable_context()

            self.assertIn("## SOUL.md", context)
            self.assertIn("## AGENTS.md", context)
            self.assertIn("## USER.md", context)
            self.assertIn("## TOOLS.md", context)
            self.assertIn("Prefers concise updates.", context)
            self.assertLess(context.index("## SOUL.md"), context.index("## AGENTS.md"))

    def test_system_prompt_falls_back_when_workspace_has_no_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = AgentWorkspace(Path(tmpdir))

            self.assertEqual(workspace.system_prompt(fallback="fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
