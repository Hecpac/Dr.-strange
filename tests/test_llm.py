from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.adapters.base import AdapterUnavailableError, LLMRequest
from claw_v2.eval_mocks import StaticAdapter, echo_response
from claw_v2.llm import LLMRouter
from claw_v2.types import LLMResponse

from tests.helpers import make_config


class LLMRouterTests(unittest.TestCase):
    def test_secondary_lane_requires_evidence_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={"anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic"))},
            )
            with self.assertRaises(ValueError):
                router.ask("judge this", lane="judge")

    def test_secondary_lane_falls_back_to_anthropic(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))

            def failing(_: LLMRequest) -> LLMResponse:
                raise AdapterUnavailableError("provider unavailable")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=failing),
                },
            )
            response = router.ask("verify", lane="verifier", evidence_pack={"diff": "x"})
            self.assertEqual(response.provider, "anthropic")
            self.assertTrue(response.degraded_mode)
            self.assertIn("fallback_reason", response.artifacts)

    def test_worker_lane_rejects_non_tool_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
                    "openai": StaticAdapter("openai", tool_capable=False, responder=echo_response("openai")),
                },
            )
            with self.assertRaises(ValueError):
                router.ask("do work", lane="worker", provider="openai")


if __name__ == "__main__":
    unittest.main()
