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

    def test_sandbox_capability_profile_defaults_to_engineer_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertEqual(config.sandbox_capability_profile, "engineer")

                with patch.dict(os.environ, {"SANDBOX_CAPABILITY_PROFILE": "surgical"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertEqual(configured.sandbox_capability_profile, "surgical")
            finally:
                os.chdir(previous_cwd)

    def test_sdk_bypass_permissions_defaults_to_enabled_and_accepts_override(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                with patch.dict(os.environ, {}, clear=True):
                    config = AppConfig.from_env()
                self.assertTrue(config.sdk_bypass_permissions)

                with patch.dict(os.environ, {"SDK_BYPASS_PERMISSIONS": "false"}, clear=True):
                    configured = AppConfig.from_env()
                self.assertFalse(configured.sdk_bypass_permissions)
            finally:
                os.chdir(previous_cwd)

    def test_runtime_config_path_loads_monitored_sites_and_sub_agent_jobs(self) -> None:
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                runtime_config = Path(tmpdir) / "runtime.yml"
                runtime_config.write_text(
                    "monitored_sites:\n"
                    "  - name: status page\n"
                    "    url: https://status.example.com\n"
                    "    interval_seconds: 900\n"
                    "scheduled_sub_agents:\n"
                    "  - agent: alma\n"
                    "    skill: daily-brief\n"
                    "    interval_seconds: 7200\n"
                    "    lane: worker\n",
                    encoding="utf-8",
                )
                with patch.dict(os.environ, {"RUNTIME_CONFIG_PATH": str(runtime_config)}, clear=True):
                    config = AppConfig.from_env()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(config.runtime_config_path, runtime_config)
        self.assertEqual(len(config.monitored_sites), 1)
        self.assertEqual(config.monitored_sites[0].name, "status page")
        self.assertEqual(config.monitored_sites[0].interval_seconds, 900)
        self.assertEqual(len(config.scheduled_sub_agents), 1)
        self.assertEqual(config.scheduled_sub_agents[0].agent, "alma")
        self.assertEqual(config.scheduled_sub_agents[0].skill, "daily-brief")


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

    def test_anthropic_advisory_model_does_not_reuse_codex_worker_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from tests.helpers import make_config
            config = make_config(Path(tmpdir))
            config.worker_provider = "codex"
            config.worker_model = "codex-mini-latest"
            config.research_provider = None
            config.research_model = None
            self.assertEqual(config.provider_for_lane("research"), "anthropic")
            self.assertEqual(config.model_for_lane("research"), "claude-sonnet-4-6")


if __name__ == "__main__":
    unittest.main()
