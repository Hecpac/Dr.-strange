"""Tests for the side-effect replay guard (2026-06-10 audit, group 1).

A turn that already executed tools must never be replayed wholesale by the
router's cross-provider fallback or by the brain's retry paths: the external
side effects (sends, pushes, publishes) would run twice. Adapters mark the
failure with ``tools_executed_before_failure`` metadata; the router and the
brain suppress their replays when the marker is present.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.adapters.base import (
    TOOLS_EXECUTED_METADATA_KEY,
    AdapterError,
    LLMRequest,
    record_tools_executed,
    tools_executed_before_failure,
)
from claw_v2.brain import BrainService
from claw_v2.eval_mocks import StaticAdapter, echo_response
from claw_v2.llm import LLMRouter
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse

from tests.helpers import make_config


def _side_effect_error(tools: list[str], message: str = "provider died mid-turn") -> AdapterError:
    exc = AdapterError(message)
    record_tools_executed(exc, tools)
    return exc


class ToolsExecutedMetadataTests(unittest.TestCase):
    def test_record_and_read_round_trip(self) -> None:
        exc = _side_effect_error(["Bash", "Write", "Bash"])
        self.assertEqual(tools_executed_before_failure(exc), ["Bash", "Write"])
        self.assertEqual(exc.metadata[TOOLS_EXECUTED_METADATA_KEY], ["Bash", "Write"])

    def test_empty_tools_leave_metadata_unset(self) -> None:
        exc = AdapterError("boom")
        record_tools_executed(exc, [])
        self.assertEqual(tools_executed_before_failure(exc), [])
        self.assertNotIn(TOOLS_EXECUTED_METADATA_KEY, exc.metadata)

    def test_non_adapter_exception_reads_empty(self) -> None:
        self.assertEqual(tools_executed_before_failure(RuntimeError("x")), [])


class RouterFallbackSuppressionTests(unittest.TestCase):
    def test_fallback_suppressed_when_tools_executed_before_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []
            openai_calls = {"count": 0}

            def failing_after_tools(_: LLMRequest) -> LLMResponse:
                raise _side_effect_error(["Bash"])

            def counting_openai(request: LLMRequest) -> LLMResponse:
                openai_calls["count"] += 1
                return echo_response("openai")(request)

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=failing_after_tools),
                    "openai": StaticAdapter("openai", tool_capable=True, responder=counting_openai),
                },
                audit_sink=audit_events.append,
            )

            with self.assertRaises(AdapterError):
                router.ask("ship it", lane="worker", provider="anthropic")

            self.assertEqual(openai_calls["count"], 0)
            suppressed = [e for e in audit_events if e["action"] == "llm_fallback_suppressed"]
            self.assertEqual(len(suppressed), 1)
            self.assertEqual(suppressed[0]["metadata"]["tools_executed"], ["Bash"])
            self.assertFalse(any(e["action"] == "llm_fallback" for e in audit_events))

    def test_fallback_still_runs_when_no_tools_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
            audit_events: list[dict] = []

            def failing_clean(_: LLMRequest) -> LLMResponse:
                raise AdapterError("temporary outage")

            router = LLMRouter(
                config=config,
                adapters={
                    "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=failing_clean),
                    "openai": StaticAdapter("openai", tool_capable=True, responder=echo_response("openai")),
                },
                audit_sink=audit_events.append,
            )

            response = router.ask("ship it", lane="worker", provider="anthropic")
            self.assertEqual(response.provider, "openai")
            self.assertTrue(any(e["action"] == "llm_fallback" for e in audit_events))


class BrainRetrySuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.memory = MemoryStore(self.db_path)
        self.router = MagicMock()
        self.router.config.max_budget_usd = 1.0
        self.observe = ObserveStream(self.db_path)
        self.brain = BrainService(
            router=self.router,
            memory=self.memory,
            system_prompt="You are Claw.",
            observe=self.observe,
        )

    def test_actionable_turn_with_side_effects_queues_recovery_without_retry(self) -> None:
        self.router.ask.side_effect = _side_effect_error(["Bash", "Write"])

        result = self.brain.handle_message("s1", "envía el reporte y termina el deploy")

        self.assertEqual(self.router.ask.call_count, 1)
        self.assertEqual(result.provider, "claw_recovery")
        self.assertEqual(
            result.artifacts["recovery_failure_reason"],
            "provider_error_after_side_effects",
        )
        event_types = [row["event_type"] for row in self.observe.recent_events(limit=100)]
        self.assertIn("brain_retry_suppressed_side_effects", event_types)

    def test_image_poison_error_with_side_effects_is_not_replayed(self) -> None:
        self.router.ask.side_effect = _side_effect_error(
            ["Bash"],
            message="an image in the conversation could not be processed",
        )

        result = self.brain.handle_message("s1", "envía el reporte del deploy")

        self.assertEqual(self.router.ask.call_count, 1)
        self.assertEqual(result.provider, "claw_recovery")


if __name__ == "__main__":
    unittest.main()
