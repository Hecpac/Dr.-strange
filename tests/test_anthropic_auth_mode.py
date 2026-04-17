from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.anthropic import ClaudeSDKExecutor
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
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                opts = executor._build_options(fake_sdk, request)
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
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                executor._build_options(fake_sdk, request)
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "test-key")


if __name__ == "__main__":
    unittest.main()
