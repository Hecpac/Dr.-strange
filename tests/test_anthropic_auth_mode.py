from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.anthropic import ClaudeSDKExecutor
from tests.helpers import make_config


class AnthropicAuthModeTests(unittest.TestCase):
    def test_subscription_mode_temporarily_hides_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "subscription"
            executor = ClaudeSDKExecutor(config)
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                self.assertIn("ANTHROPIC_API_KEY", os.environ)
                with executor._auth_environment():
                    self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
                self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "test-key")

    def test_api_key_mode_keeps_api_key_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            config.claude_auth_mode = "api_key"
            executor = ClaudeSDKExecutor(config)
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
                with executor._auth_environment():
                    self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "test-key")


if __name__ == "__main__":
    unittest.main()
