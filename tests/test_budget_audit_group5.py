"""Tests for the 2026-06-10 audit, budget group (B4/B5/B6).

1. The cost_per_hour breaker now actually blocks LLM calls (it only
   announced the block before) and self-heals when the rolling window
   decays — matching its emitted "llm_calls_until_window_decays".
2. The OpenAI adapter enforces request.max_budget across tool-loop rounds
   (OpenAI is API-billed; Claude/Codex run under Max/Pro subscriptions and
   feed the breaker as notional costs, which are ignored upstream).
3. Multi-round tool turns meter usage/cost from every round, not just the
   final response.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.adapters.base import AdapterError, LLMRequest, tools_executed_before_failure
from claw_v2.adapters.openai import OpenAIAdapter
from claw_v2.observation_window import (
    ObservationWindowBlocked,
    ObservationWindowConfig,
    ObservationWindowState,
)


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs.get("payload") or {})))

    def types(self) -> list[str]:
        return [name for name, _ in self.events]


def _window(now: list[float], threshold: float = 1.0) -> tuple[ObservationWindowState, _RecordingObserve]:
    observe = _RecordingObserve()
    window = ObservationWindowState(
        observe=observe,
        state_path=Path(tempfile.mkdtemp()) / "observation_window.json",
        config=ObservationWindowConfig(cost_per_hour_threshold=threshold),
        clock=lambda: now[0],
    )
    return window, observe


class CostBreakerBlocksLlmTests(unittest.TestCase):
    def test_cost_breaker_blocks_llm_calls_and_decays(self) -> None:
        now = [1_000.0]
        window, observe = _window(now, threshold=1.0)

        window.handle_llm_audit_event(
            {"action": "llm_response", "cost_estimate": 1.5, "provider": "openai", "lane": "worker"}
        )
        self.assertTrue(window.frozen)
        self.assertEqual(window.freeze_reason, "circuit_breaker:cost_per_hour")

        with self.assertRaises(ObservationWindowBlocked):
            window.before_llm_request(lane="worker", provider="openai", model="gpt-5.4-mini")
        self.assertIn("llm_blocked_by_cost_breaker", observe.types())

        # Rolling hour decays -> the freeze self-heals and calls flow again.
        now[0] += 3601.0
        window.before_llm_request(lane="worker", provider="openai", model="gpt-5.4-mini")
        self.assertFalse(window.frozen)
        cleared = [
            payload
            for name, payload in observe.events
            if name == "observation_window_freeze_auto_cleared"
        ]
        self.assertTrue(any(p.get("stale_reason") == "circuit_breaker:cost_per_hour" for p in cleared))

    def test_notional_subscription_costs_do_not_trip_breaker(self) -> None:
        now = [1_000.0]
        observe = _RecordingObserve()
        window = ObservationWindowState(
            observe=observe,
            state_path=Path(tempfile.mkdtemp()) / "observation_window.json",
            config=ObservationWindowConfig(
                cost_per_hour_threshold=1.0,
                notional_cost_providers=("anthropic", "codex"),
            ),
            clock=lambda: now[0],
        )
        window.handle_llm_audit_event(
            {"action": "llm_response", "cost_estimate": 50.0, "provider": "anthropic", "lane": "brain"}
        )
        self.assertFalse(window.frozen)
        self.assertIn("llm_notional_cost_ignored", observe.types())
        window.before_llm_request(lane="brain", provider="anthropic", model="claude-opus-4-7")

    def test_manual_freeze_still_allows_llm_chat(self) -> None:
        now = [1_000.0]
        window, _observe = _window(now)
        window.freeze(reason="manual_dashboard", actor="dashboard")
        # /freeze pauses autoexec (tools), not conversation.
        window.before_llm_request(lane="brain", provider="anthropic", model="claude-opus-4-7")


def _tool_request(*, max_budget: float) -> LLMRequest:
    return LLMRequest(
        prompt="haz la tarea",
        system_prompt=None,
        lane="worker",
        provider="openai",
        model="gpt-5.4-mini",
        effort=None,
        session_id=None,
        max_budget=max_budget,
        evidence_pack=None,
        allowed_tools=None,
        agents=None,
        hooks=None,
        timeout=30.0,
    )


def _fake_round(*, calls: int, usage_tokens: int = 200_000) -> SimpleNamespace:
    output = [
        SimpleNamespace(type="function_call", name="shell.run", call_id=f"c{i}", arguments="{}")
        for i in range(calls)
    ]
    return SimpleNamespace(
        id="resp-x",
        output=output,
        output_text="done",
        usage={"input_tokens": usage_tokens, "output_tokens": usage_tokens},
    )


class _FakeToolClient:
    def __init__(self, rounds: list[SimpleNamespace]) -> None:
        self._rounds = list(rounds)
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **kwargs):
        return self

    def _create(self, **kwargs):
        return self._rounds.pop(0)


class OpenAIBudgetEnforcementTests(unittest.TestCase):
    def _adapter(self) -> OpenAIAdapter:
        return OpenAIAdapter(
            api_key="sk-test",
            tool_executor=lambda _name, _args: {"ok": True},
            tool_schemas=[{"type": "function", "name": "shell.run", "parameters": {"type": "object"}}],
        )

    def test_tool_loop_aborts_when_max_budget_exceeded(self) -> None:
        # Cheap first round, expensive follow-up; tiny cap -> abort with
        # budget_exceeded after the tool ran, with the executed tools
        # recorded so fallback cannot replay them.
        rounds = [_fake_round(calls=1, usage_tokens=10), _fake_round(calls=1), _fake_round(calls=0)]
        client = _FakeToolClient(rounds)
        adapter = self._adapter()
        with patch.object(OpenAIAdapter, "_load_sdk", staticmethod(lambda: SimpleNamespace(OpenAI=lambda **kw: client))):
            with self.assertRaises(AdapterError) as ctx:
                adapter.complete(_tool_request(max_budget=0.01))
        self.assertEqual(ctx.exception.metadata.get("reason"), "budget_exceeded")
        self.assertEqual(tools_executed_before_failure(ctx.exception), ["shell.run"])

    def test_single_round_response_is_budget_checked(self) -> None:
        # PR #83 review (codex P2): advisory/no-tool turns never enter the
        # tool loop, so the first response must be budget-checked too.
        rounds = [_fake_round(calls=0, usage_tokens=50_000_000)]
        client = _FakeToolClient(rounds)
        adapter = OpenAIAdapter(api_key="sk-test")
        with patch.object(OpenAIAdapter, "_load_sdk", staticmethod(lambda: SimpleNamespace(OpenAI=lambda **kw: client))):
            with self.assertRaises(AdapterError) as ctx:
                adapter.complete(_tool_request(max_budget=0.01))
        self.assertEqual(ctx.exception.metadata.get("reason"), "budget_exceeded")

    def test_multi_round_usage_and_cost_are_summed(self) -> None:
        rounds = [_fake_round(calls=1, usage_tokens=1000), _fake_round(calls=0, usage_tokens=1000)]
        client = _FakeToolClient(rounds)
        adapter = self._adapter()
        with patch.object(OpenAIAdapter, "_load_sdk", staticmethod(lambda: SimpleNamespace(OpenAI=lambda **kw: client))):
            response = adapter.complete(_tool_request(max_budget=5.0))
        usage = response.artifacts["usage"]
        self.assertEqual(usage["input_tokens"], 2000)
        self.assertEqual(usage["output_tokens"], 2000)
        self.assertEqual(usage["rounds"], 2)
        if not response.cost_unknown:
            from claw_v2.pricing import estimate_cost_usd

            per_round = estimate_cost_usd(
                "openai", "gpt-5.4-mini", {"input_tokens": 1000, "output_tokens": 1000}
            )
            self.assertAlmostEqual(response.cost_estimate, per_round.amount_usd * 2, places=9)


if __name__ == "__main__":
    unittest.main()
