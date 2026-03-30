from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.config import AppConfig
from claw_v2.sandbox import SandboxPolicy, sandbox_hook


class AppConfigDefaultsTests(unittest.TestCase):
    def test_workspace_root_defaults_to_current_working_directory(self) -> None:
        home = str(Path.home())
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"HOME": home}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(config.workspace_root, Path(tmpdir).resolve())

    def test_default_allowed_read_paths_allow_reading_from_home(self) -> None:
        home = Path.home()
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {"HOME": str(home)}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertIn(home, config.allowed_read_paths)
        policy = SandboxPolicy(
            workspace_root=config.workspace_root,
            allowed_paths=config.allowed_read_paths,
            writable_paths=[config.workspace_root],
        )
        decision = sandbox_hook("Read", {"file_path": str(home / "agents" / "notes.txt")}, policy=policy)
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
