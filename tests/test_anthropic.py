from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.anthropic import create_claude_sdk_executor
from claw_v2.adapters.base import AdapterUnavailableError
from claw_v2.llm import LLMRouter

from tests.helpers import make_config


class AnthropicIntegrationTests(unittest.TestCase):
    def test_executor_fails_explicitly_when_sdk_package_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            executor = create_claude_sdk_executor(config)
            router = LLMRouter.default(config, anthropic_executor=executor)
            with patch("claw_v2.adapters.anthropic.import_module", side_effect=ModuleNotFoundError):
                with self.assertRaises(AdapterUnavailableError):
                    router.ask("hello", lane="brain", system_prompt="You are Claw.")


if __name__ == "__main__":
    unittest.main()
