from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from claw_v2.bus import AgentBus, _new_message
from claw_v2.kairos import KairosService, TickDecision, KairosState


@dataclass
class _Site:
    name: str
    url: str


def _make_service(**overrides):
    router = MagicMock()
    heartbeat = MagicMock()
    observe = MagicMock()
    # Default heartbeat.collect() returns a reasonable snapshot
    heartbeat.collect.return_value = MagicMock(
        pending_approvals=0,
        pending_approval_ids=[],
        agents={},
        lane_metrics={},
    )
    defaults = dict(
        router=router,
        heartbeat=heartbeat,
        observe=observe,
        action_budget=15.0,
        brief=True,
    )
    defaults.update(overrides)
    svc = KairosService(**defaults)
    return svc, router, heartbeat, observe


class GatherContextTests(unittest.TestCase):
    def test_includes_heartbeat_data(self) -> None:
        svc, _, heartbeat, _ = _make_service()
        heartbeat.collect.return_value = MagicMock(
            pending_approvals=2,
            pending_approval_ids=["a1", "a2"],
            agents={"hex": {"paused": False, "last_metric": 0.9}},
            lane_metrics={},
        )
        ctx = svc._gather_context()
        self.assertIn("Pending approvals: 2", ctx)
        self.assertIn("a1", ctx)
        self.assertIn("hex", ctx)

    def test_includes_recent_events(self) -> None:
        svc, _, _, observe = _make_service()
        observe.recent_events.return_value = [
            {"event_type": "heartbeat", "payload": {"ts": "now"}},
        ]
        ctx = svc._gather_context()
        self.assertIn("heartbeat", ctx)

    def test_handles_heartbeat_failure(self) -> None:
        svc, _, heartbeat, _ = _make_service()
        heartbeat.collect.side_effect = RuntimeError("db locked")
        ctx = svc._gather_context()
        self.assertIn("unavailable", ctx)

    def test_handles_observe_failure(self) -> None:
        svc, _, _, observe = _make_service()
        observe.recent_events.side_effect = RuntimeError("db locked")
        ctx = svc._gather_context()
        self.assertIn("unavailable", ctx)


class DecideTests(unittest.TestCase):
    def test_decide_none(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content='{"action": "none"}')
        decision = svc._decide("some context")
        self.assertEqual(decision.action, "none")
        call_kwargs = router.ask.call_args.kwargs
        self.assertEqual(call_kwargs["lane"], "judge")
        self.assertIn("evidence_pack", call_kwargs)

    def test_decide_prompt_includes_few_shot_examples_and_configured_sites(self) -> None:
        svc, router, *_ = _make_service(monitored_sites=[_Site("status.example", "https://status.example")])
        router.ask.return_value = MagicMock(content='{"action": "none"}')

        svc._decide("Pending approvals: 0")

        prompt = router.ask.call_args.args[0]
        self.assertIn("## Examples", prompt)
        self.assertIn("critical approval pending", prompt)
        self.assertIn("status.example", prompt)
        self.assertNotIn("approve_pending", prompt)
        self.assertNotIn("premiumhome.design and pachanodesign.com", prompt)

    def test_decide_action(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(
            content='{"action": "notify", "reason": "approval pending", "detail": "approve deploy"}'
        )
        decision = svc._decide("ctx")
        self.assertEqual(decision.action, "notify")
        self.assertEqual(decision.reason, "approval pending")

    def test_decide_handles_bad_json(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content="not json")
        decision = svc._decide("ctx")
        self.assertEqual(decision.action, "none")

    def test_decide_handles_router_error(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.side_effect = RuntimeError("timeout")
        decision = svc._decide("ctx")
        self.assertEqual(decision.action, "none")
        self.assertIn("timeout", decision.error)


class ParseDecisionTests(unittest.TestCase):
    def test_parses_valid_json(self) -> None:
        d = KairosService._parse_decision('{"action": "alert", "reason": "high cost"}')
        self.assertEqual(d.action, "alert")
        self.assertEqual(d.reason, "high cost")

    def test_parses_json_with_surrounding_text(self) -> None:
        d = KairosService._parse_decision('Here is my decision: {"action": "none"} end.')
        self.assertEqual(d.action, "none")

    def test_returns_none_for_garbage(self) -> None:
        d = KairosService._parse_decision("no json here")
        self.assertEqual(d.action, "none")

    def test_returns_none_for_empty(self) -> None:
        d = KairosService._parse_decision("")
        self.assertEqual(d.action, "none")


class TickTests(unittest.TestCase):
    def test_tick_no_action(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(content='{"action": "none"}')

        result = svc.tick()

        self.assertEqual(result.action, "none")
        self.assertEqual(svc.state.ticks, 1)
        self.assertEqual(svc.state.actions_taken, 0)
        observe.emit.assert_called_once()
        call_payload = observe.emit.call_args.kwargs["payload"]
        self.assertEqual(call_payload["action"], "none")

    def test_tick_with_action(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(
            content='{"action": "alert", "reason": "stale agent", "detail": "hex paused 3 days"}'
        )

        result = svc.tick()

        self.assertEqual(result.action, "alert")
        self.assertEqual(svc.state.ticks, 1)
        self.assertEqual(svc.state.actions_taken, 1)
        self.assertEqual(svc.state.last_action, "alert")

    def test_tick_increments_counter(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content='{"action": "none"}')
        svc.tick()
        svc.tick()
        svc.tick()
        self.assertEqual(svc.state.ticks, 3)

    def test_tick_handles_exception(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.side_effect = RuntimeError("catastrophic")

        result = svc.tick()

        self.assertEqual(result.action, "none")
        self.assertIn("catastrophic", result.error)
        self.assertEqual(svc.state.ticks, 1)

    def test_tick_budget_exhaustion(self) -> None:
        svc, router, _, observe = _make_service(action_budget=0.0)
        router.ask.return_value = MagicMock(
            content='{"action": "notify", "reason": "test"}'
        )

        result = svc.tick()

        self.assertEqual(result.action, "notify")
        self.assertIn("budget_exhausted", result.error)
        # Action not counted since it didn't execute
        self.assertEqual(svc.state.actions_taken, 0)

    def test_handle_event_injects_event_context_and_emits_event(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(content='{"action": "none"}')

        result = svc.handle_event("github.notification", {"repo": "owner/repo"})

        self.assertEqual(result.action, "none")
        prompt = router.ask.call_args.args[0]
        self.assertIn("Event trigger: github.notification", prompt)
        self.assertIn('"repo": "owner/repo"', prompt)
        observe.emit.assert_any_call(
            "kairos_event",
            trace_id=ANY,
            root_trace_id=ANY,
            span_id=ANY,
            parent_span_id=ANY,
            artifact_id="kairos_event:github.notification",
            payload={"event_type": "github.notification", "action": "none", "reason": "", "error": ""},
        )


class StateTests(unittest.TestCase):
    def test_initial_state(self) -> None:
        svc, *_ = _make_service()
        self.assertEqual(svc.state.ticks, 0)
        self.assertEqual(svc.state.actions_taken, 0)
        self.assertEqual(svc.state.last_action, "")

    def test_state_tracks_last_tick(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content='{"action": "none"}')
        before = time.time()
        svc.tick()
        self.assertGreaterEqual(svc.state.last_tick_at, before)


class ExecuteActionTests(unittest.TestCase):
    def test_dispatch_to_agent_sends_bus_message(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        svc, router, heartbeat, observe = _make_service(bus=bus, approvals=MagicMock())
        decision = TickDecision(
            action="dispatch_to_agent",
            reason="test failure detected",
            detail=json.dumps({"to_agent": "hex", "topic": "test_failure", "payload": {"file": "bot.py"}}),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "dispatch_to_agent")
        self.assertEqual(result.error, "")
        self.assertEqual(bus.pending_count("hex"), 1)

    def test_pause_agent_emits_event(self) -> None:
        auto_research = MagicMock()
        svc, _, _, observe = _make_service(bus=MagicMock(), approvals=MagicMock(), auto_research=auto_research)
        decision = TickDecision(
            action="pause_agent",
            reason="budget exceeded",
            detail=json.dumps({"agent_name": "lux", "reason": "cost limit"}),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "pause_agent")
        auto_research.pause.assert_called_once_with("lux")
        observe.emit.assert_any_call(
            "agent_paused",
            trace_id=ANY,
            root_trace_id=ANY,
            span_id=ANY,
            parent_span_id=ANY,
            job_id=None,
            artifact_id="pause_agent",
            payload={"agent_name": "lux", "reason": "cost limit"},
        )

    def test_run_skill_executes_sub_agent_skill(self) -> None:
        sub_agents = MagicMock()
        sub_agents.run_skill.return_value = "skill-output"
        svc, _, _, observe = _make_service(sub_agents=sub_agents)
        decision = TickDecision(
            action="run_skill",
            reason="ops audit due",
            detail=json.dumps({"agent": "rook", "skill": "health-audit"}),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.error, "")
        sub_agents.run_skill.assert_called_once_with("rook", "health-audit", "", lane="worker")
        observe.emit.assert_any_call(
            "kairos_run_skill",
            trace_id=ANY,
            root_trace_id=ANY,
            span_id=ANY,
            parent_span_id=ANY,
            job_id=None,
            artifact_id="run_skill",
            payload={"agent": "rook", "skill": "health-audit", "lane": "worker", "result": "skill-output"},
        )

    def test_approve_pending_is_not_executable(self) -> None:
        approvals = MagicMock()
        svc, _, _, _ = _make_service(approvals=approvals)
        decision = TickDecision(
            action="approve_pending",
            reason="low risk",
            detail=json.dumps({"approval_id": "abc123"}),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertIn("unknown action", result.error)
        approvals.approve_internal.assert_not_called()

    def test_unknown_action_returns_error(self) -> None:
        svc, _, _, observe = _make_service(bus=MagicMock(), approvals=MagicMock())
        decision = TickDecision(action="unknown_thing", reason="test")
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "unknown_thing")
        self.assertIn("unknown", result.error)

    def test_notify_user_suppresses_noise(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(content='{"important": false}')
        decision = TickDecision(action="notify_user", reason="routine FYI", detail="Routine background update")

        svc._handle_notify_user(decision)

        observe.emit.assert_any_call(
            "kairos_notify_suppressed",
            trace_id=None,
            root_trace_id=None,
            span_id=None,
            parent_span_id=None,
            job_id=None,
            artifact_id=None,
            payload={"message": "Routine background update", "reason": "routine FYI"},
        )

    def test_notify_user_bypasses_noise_filter_for_critical_text(self) -> None:
        svc, router, _, observe = _make_service()
        decision = TickDecision(action="notify_user", reason="critical approval", detail="Critical approval pending")

        svc._handle_notify_user(decision)

        router.ask.assert_not_called()
        observe.emit.assert_any_call(
            "kairos_notify_user",
            trace_id=None,
            root_trace_id=None,
            span_id=None,
            parent_span_id=None,
            job_id=None,
            artifact_id=None,
            payload={"message": "Critical approval pending"},
        )


class EnhancedContextTests(unittest.TestCase):
    def test_context_includes_urgent_bus_messages(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        msg = _new_message(from_agent="rook", to_agent="hex", intent="escalate", topic="fire", payload={}, priority="urgent")
        bus.send(msg)
        svc, _, _, _ = _make_service(bus=bus)
        ctx = svc._gather_context()
        self.assertIn("Urgent bus messages: 1", ctx)
        self.assertIn("fire", ctx)

    def test_context_includes_expired_requests(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        msg = _new_message(from_agent="rook", to_agent="hex", intent="request", topic="help", payload={}, ttl_seconds=1)
        msg.created_at = time.time() - 10
        bus.send(msg)
        svc, _, _, _ = _make_service(bus=bus)
        ctx = svc._gather_context()
        self.assertIn("Expired requests: 1", ctx)


if __name__ == "__main__":
    unittest.main()
