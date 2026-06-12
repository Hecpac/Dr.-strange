from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.anthropic import ClaudeSDKExecutor
from claw_v2.adapters.anthropic_auth import resolve_anthropic_api_key
from claw_v2.adapters.base import AdapterUnavailableError
from tests.helpers import make_config


class AnthropicAuthModeTests(unittest.TestCase):
    def test_subscription_mode_clears_api_key_in_sdk_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "subscription"
            executor = ClaudeSDKExecutor(config)
            fake_sdk = MagicMock()
            request = MagicMock()
            request.lane = "brain"
            request.allowed_tools = None
            request.agents = None
            request.hooks = None
            request.model = "test"
            request.session_id = None
            request.max_budget = 1.0
            request.cwd = None
            request.effort = "high"
            request.evidence_pack = {}
            request.thinking_tokens = 0
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                executor._build_options(fake_sdk, request)
                env = fake_sdk.ClaudeAgentOptions.call_args[1].get("env", {})
                self.assertEqual(env.get("ANTHROPIC_API_KEY"), "")

    def test_api_key_mode_passes_key_in_sdk_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "api_key"
            executor = ClaudeSDKExecutor(config)
            fake_sdk = MagicMock()
            request = MagicMock()
            request.lane = "brain"
            request.allowed_tools = None
            request.agents = None
            request.hooks = None
            request.model = "test"
            request.session_id = None
            request.max_budget = 1.0
            request.cwd = None
            request.effort = "high"
            request.evidence_pack = {}
            request.thinking_tokens = 0
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                executor._build_options(fake_sdk, request)
                env = fake_sdk.ClaudeAgentOptions.call_args[1].get("env", {})
                self.assertEqual(env.get("ANTHROPIC_API_KEY"), "test-key")

    def test_subscription_mode_does_not_mutate_os_environ(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "subscription"
            executor = ClaudeSDKExecutor(config)
            fake_sdk = MagicMock()
            request = MagicMock()
            request.lane = "brain"
            request.allowed_tools = None
            request.agents = None
            request.hooks = None
            request.model = "test"
            request.session_id = None
            request.max_budget = 1.0
            request.cwd = None
            request.effort = "high"
            request.evidence_pack = {}
            request.thinking_tokens = 0
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                executor._build_options(fake_sdk, request)
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "test-key")


class EnvOnlyKeyResolutionTests(unittest.TestCase):
    """D4 / DA.4 — key resolution is env-only; shell dotfiles never count."""

    def test_dotfile_key_is_not_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            for dotfile in (".zshrc", ".zprofile", ".zshenv", ".profile"):
                (home / dotfile).write_text(
                    'export ANTHROPIC_API_KEY="sk-from-dotfile"\n', encoding="utf-8"
                )
            with patch.dict(
                os.environ, {"HOME": str(home), "ANTHROPIC_API_KEY": ""}, clear=False
            ):
                self.assertIsNone(resolve_anthropic_api_key())

    def test_claw_env_file_key_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / "env"
            env_file.write_text(
                "# daemon env\nexport ANTHROPIC_API_KEY='sk-from-claw-env'\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
                self.assertEqual(
                    resolve_anthropic_api_key(env_file=env_file), "sk-from-claw-env"
                )

    def test_process_env_wins_over_claw_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / "env"
            env_file.write_text("ANTHROPIC_API_KEY=sk-from-file\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"ANTHROPIC_API_KEY": "sk-from-env"}, clear=False
            ):
                self.assertEqual(
                    resolve_anthropic_api_key(env_file=env_file), "sk-from-env"
                )

    def test_default_env_file_is_claw_env_under_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            claw_dir = home / ".claw"
            claw_dir.mkdir()
            (claw_dir / "env").write_text(
                "export ANTHROPIC_API_KEY=sk-home-claw\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ, {"HOME": str(home), "ANTHROPIC_API_KEY": ""}, clear=False
            ):
                self.assertEqual(resolve_anthropic_api_key(), "sk-home-claw")

    def test_api_key_mode_without_key_raises_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            (home / ".zshrc").write_text(
                "export ANTHROPIC_API_KEY=sk-from-dotfile\n", encoding="utf-8"
            )
            config = make_config(home / "ws")
            config.claude_auth_mode = "api_key"
            executor = ClaudeSDKExecutor(config)
            fake_sdk = MagicMock()
            request = MagicMock()
            request.lane = "brain"
            request.allowed_tools = None
            request.agents = None
            request.hooks = None
            request.model = "test"
            request.session_id = None
            request.max_budget = 1.0
            request.cwd = None
            request.effort = "high"
            request.evidence_pack = {}
            request.thinking_tokens = 0
            with patch.dict(
                os.environ, {"HOME": str(home), "ANTHROPIC_API_KEY": ""}, clear=False
            ):
                with self.assertRaises(AdapterUnavailableError) as ctx:
                    executor._build_options(fake_sdk, request)
            self.assertIn("~/.claw/env", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
