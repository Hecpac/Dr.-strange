from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.adapters.base import LLMRequest, PreLLMHook, PostLLMHook
from claw_v2.hooks import make_daily_cost_gate
from claw_v2.llm import LLMRouter
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse


def _fake_adapter_complete(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="adapter response",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
        confidence=0.9,
        cost_estimate=0.05,
    )


def _make_router(
    *,
    pre_hooks: list[PreLLMHook] | None = None,
    post_hooks: list[PostLLMHook] | None = None,
) -> LLMRouter:
    from tests.helpers import make_config
    from pathlib import Path
    import tempfile

    tmpdir = tempfile.mkdtemp()
    config = make_config(Path(tmpdir))
    return LLMRouter.default(
        config,
        anthropic_executor=_fake_adapter_complete,
        pre_hooks=pre_hooks,
        post_hooks=post_hooks,
    )


class PreHookTests(unittest.TestCase):
    def test_pre_hook_can_block_request(self) -> None:
        def blocker(request: LLMRequest) -> LLMRequest | None:
            return None

        router = _make_router(pre_hooks=[blocker])
        response = router.ask("hello", lane="brain", system_prompt="test")
        self.assertEqual(response.provider, "none")
        self.assertIn("blocked_by", response.artifacts)

    def test_pre_hook_can_mutate_request(self) -> None:
        seen_efforts: list[str | None] = []

        def set_effort(request: LLMRequest) -> LLMRequest:
            request.effort = "low"
            return request

        def capture_effort(request: LLMRequest, response: LLMResponse) -> LLMResponse:
            seen_efforts.append(request.effort)
            return response

        router = _make_router(pre_hooks=[set_effort], post_hooks=[capture_effort])
        router.ask("hello", lane="brain", system_prompt="test")
        self.assertEqual(seen_efforts, ["low"])

    def test_multiple_pre_hooks_run_in_order(self) -> None:
        order: list[str] = []

        def hook_a(request: LLMRequest) -> LLMRequest:
            order.append("a")
            return request

        def hook_b(request: LLMRequest) -> LLMRequest:
            order.append("b")
            return request

        router = _make_router(pre_hooks=[hook_a, hook_b])
        router.ask("hello", lane="brain", system_prompt="test")
        self.assertEqual(order, ["a", "b"])

    def test_second_pre_hook_not_called_after_block(self) -> None:
        order: list[str] = []

        def blocker(request: LLMRequest) -> LLMRequest | None:
            order.append("blocker")
            return None

        def should_not_run(request: LLMRequest) -> LLMRequest:
            order.append("second")
            return request

        router = _make_router(pre_hooks=[blocker, should_not_run])
        router.ask("hello", lane="brain", system_prompt="test")
        self.assertEqual(order, ["blocker"])


class PostHookTests(unittest.TestCase):
    def test_post_hook_can_mutate_response(self) -> None:
        def add_artifact(request: LLMRequest, response: LLMResponse) -> LLMResponse:
            response.artifacts["tagged"] = True
            return response

        router = _make_router(post_hooks=[add_artifact])
        response = router.ask("hello", lane="brain", system_prompt="test")
        self.assertTrue(response.artifacts.get("tagged"))
        self.assertEqual(response.content, "adapter response")

    def test_multiple_post_hooks_run_in_order(self) -> None:
        order: list[str] = []

        def hook_a(request: LLMRequest, response: LLMResponse) -> LLMResponse:
            order.append("a")
            return response

        def hook_b(request: LLMRequest, response: LLMResponse) -> LLMResponse:
            order.append("b")
            return response

        router = _make_router(post_hooks=[hook_a, hook_b])
        router.ask("hello", lane="brain", system_prompt="test")
        self.assertEqual(order, ["a", "b"])

    def test_no_hooks_works_as_before(self) -> None:
        router = _make_router()
        response = router.ask("hello", lane="brain", system_prompt="test")
        self.assertEqual(response.content, "adapter response")


class TotalCostTodayTests(unittest.TestCase):
    def test_returns_zero_when_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            self.assertAlmostEqual(observe.total_cost_today(), 0.0)

    def test_sums_cost_from_todays_llm_response_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="opus", payload={"cost_estimate": 1.5})
            observe.emit("llm_response", lane="worker", provider="anthropic", model="sonnet", payload={"cost_estimate": 0.3})
            # This event has a different type — should NOT be counted
            observe.emit("llm_decision", lane="brain", provider="anthropic", model="opus", payload={"cost_estimate": 1.5})
            self.assertAlmostEqual(observe.total_cost_today(), 1.8)

    def test_ignores_events_from_previous_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            # Insert an old event directly with a past timestamp
            observe._conn.execute(
                "INSERT INTO observe_stream (event_type, lane, provider, model, payload, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                ("llm_response", "brain", "anthropic", "opus", '{"cost_estimate": 99.0}', "2020-01-01 00:00:00"),
            )
            observe._conn.commit()
            observe.emit("llm_response", lane="brain", provider="anthropic", model="opus", payload={"cost_estimate": 2.0})
            self.assertAlmostEqual(observe.total_cost_today(), 2.0)


class DailyCostGateTests(unittest.TestCase):
    def _make_request(self) -> LLMRequest:
        return LLMRequest(
            prompt="test prompt",
            system_prompt=None,
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-6",
            effort="high",
            session_id=None,
            max_budget=0.5,
            evidence_pack=None,
            allowed_tools=None,
            agents=None,
            hooks=None,
            timeout=30.0,
        )

    def test_allows_request_when_under_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="opus", payload={"cost_estimate": 5.0})
            gate = make_daily_cost_gate(observe, daily_limit=10.0)
            request = self._make_request()
            result = gate(request)
            self.assertIsNotNone(result)
            self.assertIs(result, request)

    def test_blocks_request_when_at_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="opus", payload={"cost_estimate": 10.0})
            gate = make_daily_cost_gate(observe, daily_limit=10.0)
            result = gate(self._make_request())
            self.assertIsNone(result)

    def test_blocks_request_when_over_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="opus", payload={"cost_estimate": 12.0})
            gate = make_daily_cost_gate(observe, daily_limit=10.0)
            result = gate(self._make_request())
            self.assertIsNone(result)

    def test_allows_when_no_prior_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            gate = make_daily_cost_gate(observe, daily_limit=10.0)
            result = gate(self._make_request())
            self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
