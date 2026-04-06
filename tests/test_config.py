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

    def test_browse_backend_defaults_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertEqual(config.browse_backend, "auto")

                with patch.dict(os.environ, {"BROWSE_BACKEND": "playwright_local"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertEqual(configured.browse_backend, "playwright_local")

                with patch.dict(os.environ, {"BROWSE_BACKEND": "browserbase_cdp"}, clear=True):
                    browserbase_configured = AppConfig.from_env()
                self.assertEqual(browserbase_configured.browse_backend, "browserbase_cdp")
            finally:
                os.chdir(previous_cwd)


class CodexConfigTests(unittest.TestCase):
    def test_codex_worker_provider_passes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            # Should not raise
            config.validate()

    def test_codex_fields_have_defaults_from_env(self) -> None:
        import os
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)
        self.assertEqual(config.codex_model, "codex-mini-latest")
        self.assertEqual(config.computer_use_backend, "openai")

    def test_computer_use_backend_codex_passes_validate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.computer_use_backend = "codex"
            config.validate()


if __name__ == "__main__":
    unittest.main()
