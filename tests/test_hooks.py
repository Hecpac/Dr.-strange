from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.adapters.base import LLMRequest, PreLLMHook, PostLLMHook
from claw_v2.hooks import make_anti_distillation_hook, make_daily_cost_gate, make_decision_logger, _select_decoys, _DECOY_POOL
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
    audit_sink=None,
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
        audit_sink=audit_sink,
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


class DecisionLoggerTests(unittest.TestCase):
    def test_emits_llm_decision_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            hook = make_decision_logger(observe)
            request = LLMRequest(
                prompt="test prompt",
                system_prompt=None,
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-6",
                effort="high",
                session_id="sess-1",
                max_budget=0.5,
                evidence_pack=None,
                allowed_tools=None,
                agents=None,
                hooks=None,
                timeout=30.0,
            )
            response = LLMResponse(
                content="hello world",
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-6",
                confidence=0.85,
                cost_estimate=0.03,
            )
            result = hook(request, response)
            self.assertIs(result, response)

            events = observe.recent_events(limit=5)
            decision_events = [e for e in events if e["event_type"] == "llm_decision"]
            self.assertEqual(len(decision_events), 1)
            payload = decision_events[0]["payload"]
            self.assertEqual(payload["session_id"], "sess-1")
            self.assertAlmostEqual(payload["confidence"], 0.85)
            self.assertAlmostEqual(payload["cost_estimate"], 0.03)
            self.assertEqual(payload["response_length"], len("hello world"))
            self.assertEqual(payload["effort"], "high")
            self.assertFalse(payload["has_evidence_pack"])
            self.assertIsNotNone(decision_events[0]["trace_id"])
            self.assertIsNotNone(decision_events[0]["span_id"])

    def test_emits_agent_name_for_agent_scoped_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            hook = make_decision_logger(observe)
            request = LLMRequest(
                prompt="fix issue",
                system_prompt=None,
                lane="worker",
                provider="anthropic",
                model="claude-sonnet-4-6",
                effort="high",
                session_id="sess-2",
                max_budget=0.5,
                evidence_pack={"agent_name": "self-improve"},
                allowed_tools=["Read"],
                agents=None,
                hooks=None,
                timeout=30.0,
            )
            response = LLMResponse(
                content="done",
                lane="worker",
                provider="anthropic",
                model="claude-sonnet-4-6",
                confidence=0.8,
                cost_estimate=0.02,
            )

            hook(request, response)

            self.assertEqual(observe.cost_per_agent_today(), {"self-improve": 0.02})

    def test_handles_multimodal_prompt_length(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            hook = make_decision_logger(observe)
            request = LLMRequest(
                prompt=[{"type": "text", "text": "hello"}, {"type": "image", "source": {}}],
                system_prompt=None,
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-6",
                effort="high",
                session_id=None,
                max_budget=0.5,
                evidence_pack={"data": "test"},
                allowed_tools=None,
                agents=None,
                hooks=None,
                timeout=30.0,
            )
            response = LLMResponse(
                content="ok",
                lane="brain",
                provider="anthropic",
                model="claude-opus-4-6",
                confidence=0.5,
                cost_estimate=0.01,
            )
            hook(request, response)

            events = observe.recent_events(limit=5)
            decision_events = [e for e in events if e["event_type"] == "llm_decision"]
            payload = decision_events[0]["payload"]
            self.assertEqual(payload["prompt_length"], 2)
            self.assertTrue(payload["has_evidence_pack"])

    def test_router_uses_shared_trace_for_decision_and_response_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")

            def audit_sink(event: dict) -> None:
                observe.emit(
                    event["action"],
                    lane=event["lane"],
                    provider=event["provider"],
                    model=event["model"],
                    trace_id=event["metadata"].get("trace_id"),
                    root_trace_id=event["metadata"].get("root_trace_id"),
                    span_id=event["metadata"].get("span_id"),
                    parent_span_id=event["metadata"].get("parent_span_id"),
                    job_id=event["metadata"].get("job_id"),
                    artifact_id=event["metadata"].get("artifact_id"),
                    payload={"cost_estimate": event["cost_estimate"]},
                )

            router = _make_router(post_hooks=[make_decision_logger(observe)], audit_sink=audit_sink)
            router.ask(
                "hello",
                lane="brain",
                system_prompt="test",
                evidence_pack={"agent_name": "hex", "artifact_id": "brain-turn"},
            )

            events = observe.recent_events(limit=5)
            response_event = next(event for event in events if event["event_type"] == "llm_response")
            decision_event = next(event for event in events if event["event_type"] == "llm_decision")
            self.assertEqual(response_event["trace_id"], decision_event["trace_id"])
            self.assertEqual(response_event["span_id"], decision_event["span_id"])
            self.assertEqual(response_event["artifact_id"], "brain-turn")
            replay = observe.trace_events(response_event["trace_id"])
            self.assertEqual([event["event_type"] for event in replay], ["llm_decision", "llm_response"])


class AntiDistillationTests(unittest.TestCase):
    def _make_request(self, *, lane: str = "brain", session_id: str | None = None, system_prompt: str | None = None) -> LLMRequest:
        return LLMRequest(
            prompt="test",
            system_prompt=system_prompt,
            lane=lane,
            provider="anthropic",
            model="claude-opus-4-6",
            effort="high",
            session_id=session_id,
            max_budget=0.5,
            evidence_pack=None,
            allowed_tools=None,
            agents=None,
            hooks=None,
            timeout=30.0,
        )

    def test_injects_decoys_on_brain_lane(self) -> None:
        hook = make_anti_distillation_hook()
        request = self._make_request(system_prompt="You are Claw.")
        result = hook(request)
        self.assertIsNotNone(result)
        self.assertIn("You are Claw.", result.system_prompt)
        # Should contain at least one decoy tool mention
        has_decoy = any(decoy in result.system_prompt for decoy in _DECOY_POOL)
        self.assertTrue(has_decoy)

    def test_skips_advisory_lanes(self) -> None:
        hook = make_anti_distillation_hook()
        for lane in ("research", "verifier", "judge"):
            request = self._make_request(lane=lane, system_prompt="base")
            result = hook(request)
            self.assertEqual(result.system_prompt, "base")

    def test_disabled_hook_passes_through(self) -> None:
        hook = make_anti_distillation_hook(enabled=False)
        request = self._make_request(system_prompt="base")
        result = hook(request)
        self.assertEqual(result.system_prompt, "base")

    def test_handles_none_system_prompt(self) -> None:
        hook = make_anti_distillation_hook()
        request = self._make_request(system_prompt=None)
        result = hook(request)
        self.assertIsNotNone(result.system_prompt)
        has_decoy = any(decoy in result.system_prompt for decoy in _DECOY_POOL)
        self.assertTrue(has_decoy)

    def test_decoys_rotate_by_session(self) -> None:
        d1 = _select_decoys("session-aaa")
        d2 = _select_decoys("session-zzz")
        # Different sessions should (usually) pick different decoys
        # Not guaranteed but extremely likely with sha256
        self.assertEqual(len(d1), 2)
        self.assertEqual(len(d2), 2)
        # Both should be from the pool
        for d in d1 + d2:
            self.assertIn(d, _DECOY_POOL)

    def test_select_decoys_deterministic(self) -> None:
        d1 = _select_decoys("fixed-session")
        d2 = _select_decoys("fixed-session")
        self.assertEqual(d1, d2)

    def test_select_decoys_returns_requested_count(self) -> None:
        d = _select_decoys("test", count=3)
        self.assertEqual(len(d), 3)
        # All unique
        self.assertEqual(len(set(id(x) for x in d)), 3)


if __name__ == "__main__":
    unittest.main()
