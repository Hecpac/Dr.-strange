from __future__ import annotations

import ast
import datetime
import json
import os
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

from claw_v2.approval import ApprovalManager
from claw_v2.bus import AgentBus, _new_message
from claw_v2.kairos import KairosService, TickDecision, _classify_decide_error


@dataclass
class _Site:
    name: str
    url: str


class KairosSubprocessPolicyTests(unittest.TestCase):
    def test_kairos_uses_bounded_subprocess_runner_instead_of_raw_run(self) -> None:
        tree = ast.parse(Path("claw_v2/kairos.py").read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            ):
                offenders.append(f"claw_v2/kairos.py:{node.lineno}")

        self.assertEqual(offenders, [])


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
        self.assertEqual(call_kwargs["role"], "control_judge")
        self.assertEqual(call_kwargs["timeout"], 30.0)
        self.assertIn("evidence_pack", call_kwargs)

    def test_decide_prompt_includes_few_shot_examples_and_configured_sites(self) -> None:
        svc, router, *_ = _make_service(
            monitored_sites=[_Site("status.example", "https://status.example")]
        )
        router.ask.return_value = MagicMock(content='{"action": "none"}')

        svc._decide("Pending approvals: 0")

        prompt = router.ask.call_args.args[0]
        self.assertIn("## Examples", prompt)
        self.assertIn("critical approval pending", prompt)
        self.assertIn("status.example", prompt)
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

    def test_decide_emits_event_on_failure_with_codex_timeout_classification(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.side_effect = RuntimeError("Codex CLI timed out after 300.0s")
        decision = svc._decide("ctx")
        self.assertEqual(decision.action, "none")
        emit_calls = [
            call for call in observe.emit.call_args_list if call.args[0] == "kairos_decide_failed"
        ]
        self.assertEqual(len(emit_calls), 1)
        payload = emit_calls[0].kwargs["payload"]
        self.assertEqual(payload["error_kind"], "codex_timeout")
        self.assertIn("Codex CLI timed out", payload["error"])

    def test_decide_emits_event_with_general_classification_for_unknown_error(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.side_effect = RuntimeError("something unexpected")
        svc._decide("ctx")
        emit_calls = [
            call for call in observe.emit.call_args_list if call.args[0] == "kairos_decide_failed"
        ]
        self.assertEqual(emit_calls[0].kwargs["payload"]["error_kind"], "general")


class RunAgentLoopHandlerTests(unittest.TestCase):
    def test_run_agent_loop_invokes_factory_and_emits_outcome_event(self) -> None:
        from claw_v2.agent_loop import AgentLoopOutcome

        run_calls: list[str] = []

        class _StubLoop:
            def run(self, goal: str):
                run_calls.append(goal)
                return AgentLoopOutcome(status="passed", final_result=None, history=(), reason="ok")

        factory_calls: list[tuple] = []

        def factory(goal_id, project_id, milestone_id):
            factory_calls.append((goal_id, project_id, milestone_id))
            return _StubLoop()

        svc, _, _, observe = _make_service(agent_loop_factory=factory)
        decision = TickDecision(
            action="run_agent_loop",
            reason="milestone overdue",
            detail=json.dumps(
                {
                    "goal_id": "g_landing",
                    "project_id": "p_site",
                    "milestone_id": "m_hero",
                    "goal_text": "Ship hero copy",
                }
            ),
        )
        svc._handle_run_agent_loop(decision)

        self.assertEqual(factory_calls, [("g_landing", "p_site", "m_hero")])
        self.assertEqual(run_calls, ["Ship hero copy"])
        emit_calls = [
            call
            for call in observe.emit.call_args_list
            if call.args[0] == "kairos_agent_loop_complete"
        ]
        self.assertEqual(len(emit_calls), 1)
        payload = emit_calls[0].kwargs["payload"]
        self.assertEqual(payload["goal_id"], "g_landing")
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["iterations"], 0)

    def test_run_agent_loop_raises_when_factory_not_configured(self) -> None:
        svc, *_ = _make_service()  # no agent_loop_factory
        decision = TickDecision(action="run_agent_loop", detail='{"goal_id": "x"}')
        with self.assertRaises(RuntimeError):
            svc._handle_run_agent_loop(decision)

    def test_run_agent_loop_requires_goal_id(self) -> None:
        svc, *_ = _make_service(agent_loop_factory=lambda *a, **kw: None)
        decision = TickDecision(action="run_agent_loop", detail="{}")
        with self.assertRaises(ValueError):
            svc._handle_run_agent_loop(decision)

    def test_run_agent_loop_falls_back_goal_text_to_goal_id(self) -> None:
        from claw_v2.agent_loop import AgentLoopOutcome

        seen: list[str] = []

        class _StubLoop:
            def run(self, goal):
                seen.append(goal)
                return AgentLoopOutcome("passed", None, (), "ok")

        factory = lambda goal_id, project_id, milestone_id: _StubLoop()
        svc, *_ = _make_service(agent_loop_factory=factory)
        svc._handle_run_agent_loop(
            TickDecision(action="run_agent_loop", detail='{"goal_id": "g_solo"}')
        )
        self.assertEqual(seen, ["g_solo"])


class ClassifyDecideErrorTests(unittest.TestCase):
    def test_codex_timeout_recognized(self) -> None:
        self.assertEqual(
            _classify_decide_error("Codex CLI timed out after 300.0s"), "codex_timeout"
        )

    def test_codex_timeout_recognized_in_lowercase_variants(self) -> None:
        self.assertEqual(_classify_decide_error("codex provider timed out"), "codex_timeout")

    def test_circuit_open_recognized(self) -> None:
        self.assertEqual(
            _classify_decide_error("observation window frozen: circuit_breaker:cost_per_hour"),
            "circuit_open",
        )

    def test_generic_timeout_recognized(self) -> None:
        self.assertEqual(_classify_decide_error("HTTP request timeout"), "timeout")

    def test_other_errors_classified_general(self) -> None:
        self.assertEqual(_classify_decide_error("AttributeError: foo"), "general")


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

    def test_tick_suppresses_recently_started_duplicate_action(self) -> None:
        # AM-KAIROSIDEM (2026-06-12): the tick runs as an at-least-once job —
        # a crash-replay must not re-execute the same action+detail. The
        # kairos_action_started checkpoint within the window suppresses it.
        from datetime import datetime, timezone

        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(
            content='{"action": "alert", "reason": "stale agent", "detail": "hex paused 3 days"}'
        )
        import hashlib as _hashlib

        action_key = _hashlib.sha256(b"alert|hex paused 3 days").hexdigest()[:16]
        started_event = {
            "event_type": "kairos_action_started",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "payload": {"action": "alert", "action_key": action_key},
        }

        def _recent(limit: int = 20, *, event_type: str | None = None):
            if event_type == "kairos_action_started":
                return [started_event]
            return []

        observe.recent_events.side_effect = _recent

        result = svc.tick()

        self.assertEqual(result.error, "duplicate_action_suppressed")
        self.assertEqual(svc.state.actions_taken, 0)
        suppressed = [
            call
            for call in observe.emit.call_args_list
            if call.args and call.args[0] == "kairos_action_suppressed"
        ]
        self.assertEqual(len(suppressed), 1)

    def test_tick_retries_action_whose_previous_attempt_failed_cleanly(self) -> None:
        # PR #95 review (codex P2): a handler that failed cleanly never
        # produced the side effect — the kairos_action_failed checkpoint
        # must re-arm the retry instead of suppressing it for 30 min.
        from datetime import datetime, timezone

        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(
            content='{"action": "alert", "reason": "stale agent", "detail": "hex paused 3 days"}'
        )
        import hashlib as _hashlib

        action_key = _hashlib.sha256(b"alert|hex paused 3 days").hexdigest()[:16]
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        def _recent(limit: int = 20, *, event_type: str | None = None):
            if event_type == "kairos_action_started":
                return [
                    {
                        "event_type": "kairos_action_started",
                        "timestamp": now_str,
                        "payload": {"action": "alert", "action_key": action_key},
                    }
                ]
            if event_type == "kairos_action_failed":
                return [
                    {
                        "event_type": "kairos_action_failed",
                        "timestamp": now_str,
                        "payload": {"action": "alert", "action_key": action_key},
                    }
                ]
            return []

        observe.recent_events.side_effect = _recent

        result = svc.tick()

        self.assertEqual(result.action, "alert")
        self.assertNotEqual(result.error, "duplicate_action_suppressed")
        self.assertEqual(svc.state.actions_taken, 1)

    def test_tick_does_not_suppress_on_unparseable_started_timestamp(self) -> None:
        # PR #95 review (gemini): a malformed checkpoint timestamp must read
        # as "not started" — suppressing on parse failure could starve the
        # action permanently.
        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(
            content='{"action": "alert", "reason": "stale agent", "detail": "hex paused 3 days"}'
        )
        import hashlib as _hashlib

        action_key = _hashlib.sha256(b"alert|hex paused 3 days").hexdigest()[:16]

        def _recent(limit: int = 20, *, event_type: str | None = None):
            if event_type == "kairos_action_started":
                return [
                    {
                        "event_type": "kairos_action_started",
                        "timestamp": "garbage",
                        "payload": {"action": "alert", "action_key": action_key},
                    }
                ]
            return []

        observe.recent_events.side_effect = _recent

        result = svc.tick()

        self.assertEqual(result.action, "alert")
        self.assertEqual(svc.state.actions_taken, 1)

    def test_tick_executes_when_started_checkpoint_is_stale(self) -> None:
        from datetime import datetime, timedelta, timezone

        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(
            content='{"action": "alert", "reason": "stale agent", "detail": "hex paused 3 days"}'
        )
        import hashlib as _hashlib

        action_key = _hashlib.sha256(b"alert|hex paused 3 days").hexdigest()[:16]
        stale = datetime.now(timezone.utc) - timedelta(hours=2)
        observe.recent_events.return_value = [
            {
                "event_type": "kairos_action_started",
                "timestamp": stale.strftime("%Y-%m-%d %H:%M:%S"),
                "payload": {"action": "alert", "action_key": action_key},
            }
        ]

        result = svc.tick()

        self.assertEqual(result.action, "alert")
        self.assertEqual(svc.state.actions_taken, 1)
        started = [
            call
            for call in observe.emit.call_args_list
            if call.args and call.args[0] == "kairos_action_started"
        ]
        self.assertEqual(len(started), 1)

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
        router.ask.return_value = MagicMock(content='{"action": "notify", "reason": "test"}')

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
            payload={
                "event_type": "github.notification",
                "action": "none",
                "reason": "",
                "error": "",
            },
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
            detail=json.dumps(
                {"to_agent": "hex", "topic": "test_failure", "payload": {"file": "bot.py"}}
            ),
        )
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "dispatch_to_agent")
        self.assertEqual(result.error, "")
        self.assertEqual(bus.pending_count("hex"), 1)

    def test_pause_agent_emits_event(self) -> None:
        auto_research = MagicMock()
        svc, _, _, observe = _make_service(
            bus=MagicMock(), approvals=MagicMock(), auto_research=auto_research
        )
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
            payload={
                "agent": "rook",
                "skill": "health-audit",
                "lane": "worker",
                "result": "skill-output",
            },
        )

    def test_approve_pending_refuses_external_mutation(self) -> None:
        # #4: Kairos must NOT auto-approve a pending record that exists to force
        # a human decision (external mutation / not a daemon-allowlisted tool).
        with tempfile.TemporaryDirectory() as tmp:
            approvals = ApprovalManager(Path(tmp), "secret")
            pending = approvals.create(
                action="kairos:auto_publish_social",
                summary="tweet",
                metadata={"tweet": "x", "handle": "PachanoDesign"},
            )
            svc, _, _, _ = _make_service(approvals=approvals)
            decision = TickDecision(
                action="approve_pending",
                reason="llm asked",
                detail=json.dumps({"approval_id": pending.approval_id}),
            )
            result = svc._execute(decision, budget=10.0)
            self.assertNotEqual(result.error, "")
            self.assertEqual(approvals.status(pending.approval_id), "pending")

    def test_approve_pending_allows_daemon_safe_tool(self) -> None:
        # #4: the only safe auto-approve case — a pending record for a
        # read-only daemon-allowlisted tool.
        with tempfile.TemporaryDirectory() as tmp:
            approvals = ApprovalManager(Path(tmp), "secret")
            pending = approvals.create(
                action="tool:memory.read",
                summary="read memory",
                metadata={"tool": "memory.read"},
            )
            svc, _, _, observe = _make_service(approvals=approvals)
            decision = TickDecision(
                action="approve_pending",
                reason="safe",
                detail=json.dumps({"approval_id": pending.approval_id}),
            )
            result = svc._execute(decision, budget=10.0)
            self.assertEqual(result.error, "")
            self.assertEqual(approvals.status(pending.approval_id), "approved")
            observe.emit.assert_any_call(
                "kairos_auto_approved",
                trace_id=ANY,
                root_trace_id=ANY,
                span_id=ANY,
                parent_span_id=ANY,
                job_id=None,
                artifact_id="approve_pending",
                payload={"approval_id": pending.approval_id},
            )

    def test_unknown_action_returns_error(self) -> None:
        svc, _, _, observe = _make_service(bus=MagicMock(), approvals=MagicMock())
        decision = TickDecision(action="unknown_thing", reason="test")
        result = svc._execute(decision, budget=10.0)
        self.assertEqual(result.action, "unknown_thing")
        self.assertIn("unknown", result.error)

    def test_notify_user_suppresses_noise(self) -> None:
        svc, router, _, observe = _make_service()
        router.ask.return_value = MagicMock(content='{"important": false}')
        decision = TickDecision(
            action="notify_user", reason="routine FYI", detail="Routine background update"
        )

        svc._handle_notify_user(decision)

        self.assertEqual(router.ask.call_args.kwargs["role"], "control_judge")
        self.assertEqual(router.ask.call_args.kwargs["timeout"], 30.0)
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
        decision = TickDecision(
            action="notify_user", reason="critical approval", detail="Critical approval pending"
        )

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

    def test_notify_user_suppresses_duplicate_approval_backlog(self) -> None:
        svc, router, _, observe = _make_service()
        observed_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
        observe.recent_events.return_value = [
            {
                "event_type": "kairos_notify_user",
                "timestamp": observed_at,
                "payload": {
                    "message": ("5 approvals require review: 0e8fd603c8ccd46c, 4885d761ae58ee7d")
                },
            }
        ]
        decision = TickDecision(
            action="notify_user",
            reason="approval backlog",
            detail=("Pending approval IDs: 4885d761ae58ee7d, 0e8fd603c8ccd46c"),
        )

        svc._handle_notify_user(decision)

        router.ask.assert_not_called()
        observe.emit.assert_any_call(
            "kairos_notify_suppressed",
            trace_id=None,
            root_trace_id=None,
            span_id=None,
            parent_span_id=None,
            job_id=None,
            artifact_id=None,
            payload={
                "message": ("Pending approval IDs: 4885d761ae58ee7d, 0e8fd603c8ccd46c"),
                "reason": "duplicate_approval_backlog",
            },
        )

    def test_notify_user_allows_approval_backlog_when_ids_change(self) -> None:
        svc, router, _, observe = _make_service()
        observed_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
        observe.recent_events.return_value = [
            {
                "event_type": "kairos_notify_user",
                "timestamp": observed_at,
                "payload": {
                    "message": ("5 approvals require review: 0e8fd603c8ccd46c, 4885d761ae58ee7d")
                },
            }
        ]
        decision = TickDecision(
            action="notify_user",
            reason="approval backlog",
            detail=(
                "6 approvals are pending; listed IDs: "
                "0e8fd603c8ccd46c, 4885d761ae58ee7d, 6be1a96098b4cfa0"
            ),
        )

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
            payload={
                "message": (
                    "6 approvals are pending; listed IDs: "
                    "0e8fd603c8ccd46c, 4885d761ae58ee7d, 6be1a96098b4cfa0"
                )
            },
        )


class EnhancedContextTests(unittest.TestCase):
    def test_context_includes_urgent_bus_messages(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        msg = _new_message(
            from_agent="rook",
            to_agent="hex",
            intent="escalate",
            topic="fire",
            payload={},
            priority="urgent",
        )
        bus.send(msg)
        svc, _, _, _ = _make_service(bus=bus)
        ctx = svc._gather_context()
        self.assertIn("Urgent bus messages: 1", ctx)
        self.assertIn("fire", ctx)

    def test_context_includes_expired_requests(self) -> None:
        tmpdir = Path(tempfile.mkdtemp())
        bus = AgentBus(bus_root=tmpdir)
        msg = _new_message(
            from_agent="rook",
            to_agent="hex",
            intent="request",
            topic="help",
            payload={},
            ttl_seconds=1,
        )
        msg.created_at = time.time() - 10
        bus.send(msg)
        svc, _, _, _ = _make_service(bus=bus)
        ctx = svc._gather_context()
        self.assertIn("Expired requests: 1", ctx)


class AutoActionApprovalTests(unittest.TestCase):
    """Fase 1: auto_publish_social and auto_deploy default to draft +
    pending approval. Live execution is opt-in via env flag.
    """

    def setUp(self) -> None:
        for var in ("KAIROS_AUTO_PUBLISH_SOCIAL", "KAIROS_AUTO_DEPLOY"):
            os.environ.pop(var, None)

    def _wiki_with_page(self, content: str = "Insight body") -> tuple[MagicMock, Path]:
        tmpdir = Path(tempfile.mkdtemp())
        wiki = MagicMock()
        wiki.wiki_dir = tmpdir
        page = tmpdir / "p.md"
        page.write_text(content, encoding="utf-8")
        return wiki, tmpdir

    def _approvals_mock(self) -> MagicMock:
        approvals = MagicMock()
        approvals.create.return_value = MagicMock(
            approval_id="abc123",
            token="tok",
            action="kairos:auto_publish_social",
            summary="draft",
        )
        return approvals

    def test_auto_publish_social_default_creates_pending_no_publish(self) -> None:
        wiki, _ = self._wiki_with_page()
        approvals = self._approvals_mock()
        svc, router, _, observe = _make_service(wiki=wiki, approvals=approvals)
        router.ask.return_value = MagicMock(content="Tweet draft text")
        decision = TickDecision(action="auto_publish_social")
        with patch("claw_v2.social.x_adapter_from_keychain") as adapter_factory:
            svc._handle_auto_publish_social(decision)
        adapter_factory.assert_not_called()
        approvals.create.assert_called_once()
        kwargs = approvals.create.call_args.kwargs
        self.assertEqual(kwargs.get("action"), "kairos:auto_publish_social")
        self.assertIn("Tweet draft text", kwargs.get("summary", ""))
        emit_kinds = [c.args[0] for c in observe.emit.call_args_list]
        self.assertIn("kairos_auto_publish_social_pending", emit_kinds)
        self.assertNotIn("kairos_auto_publish_social", emit_kinds)

    def test_auto_publish_social_with_env_publishes(self) -> None:
        wiki, _ = self._wiki_with_page()
        approvals = self._approvals_mock()
        svc, router, _, observe = _make_service(wiki=wiki, approvals=approvals)
        router.ask.return_value = MagicMock(content="Tweet draft text")
        adapter = MagicMock()
        adapter.publish.return_value = MagicMock(success=True, post_id="pid")
        decision = TickDecision(action="auto_publish_social")
        with (
            patch.dict(os.environ, {"KAIROS_AUTO_PUBLISH_SOCIAL": "1"}),
            patch("claw_v2.social.x_adapter_from_keychain", return_value=adapter),
        ):
            svc._handle_auto_publish_social(decision)
        adapter.publish.assert_called_once_with("Tweet draft text")
        approvals.create.assert_not_called()
        emit_kinds = [c.args[0] for c in observe.emit.call_args_list]
        self.assertIn("kairos_auto_publish_social", emit_kinds)
        self.assertNotIn("kairos_auto_publish_social_pending", emit_kinds)

    def test_auto_publish_social_no_approvals_and_disabled_errors(self) -> None:
        wiki, _ = self._wiki_with_page()
        svc, router, _, _ = _make_service(wiki=wiki)
        router.ask.return_value = MagicMock(content="Tweet draft text")
        decision = TickDecision(action="auto_publish_social")
        with patch("claw_v2.social.x_adapter_from_keychain") as adapter_factory:
            with self.assertRaises(RuntimeError):
                svc._handle_auto_publish_social(decision)
        adapter_factory.assert_not_called()

    def test_auto_deploy_default_creates_pending_no_push(self) -> None:
        approvals = self._approvals_mock()
        svc, _, _, observe = _make_service(approvals=approvals)
        decision = TickDecision(action="auto_deploy")
        run_results = [
            MagicMock(returncode=0, stdout="deploy_sha\n"),
            MagicMock(returncode=0, stdout="local_sha\n"),
        ]
        with patch("claw_v2.kairos.run_subprocess_bounded", side_effect=run_results) as runner:
            svc._handle_auto_deploy(decision)
        self.assertEqual(runner.call_count, 2)
        approvals.create.assert_called_once()
        kwargs = approvals.create.call_args.kwargs
        self.assertEqual(kwargs.get("action"), "kairos:auto_deploy")
        emit_kinds = [c.args[0] for c in observe.emit.call_args_list]
        self.assertIn("kairos_auto_deploy_pending", emit_kinds)
        self.assertNotIn("kairos_auto_deploy", emit_kinds)

    def test_auto_deploy_with_env_pushes(self) -> None:
        approvals = self._approvals_mock()
        svc, _, _, observe = _make_service(approvals=approvals)
        decision = TickDecision(action="auto_deploy")
        run_results = [
            MagicMock(returncode=0, stdout="deploy_sha\n"),
            MagicMock(returncode=0, stdout="local_sha\n"),
            MagicMock(returncode=0, stdout=""),
        ]
        with (
            patch.dict(os.environ, {"KAIROS_AUTO_DEPLOY": "1"}),
            patch("claw_v2.kairos.run_subprocess_bounded", side_effect=run_results) as runner,
        ):
            svc._handle_auto_deploy(decision)
        self.assertEqual(runner.call_count, 3)
        approvals.create.assert_not_called()
        push_call = runner.call_args_list[2]
        self.assertIn("push", push_call.args[0])
        emit_kinds = [c.args[0] for c in observe.emit.call_args_list]
        self.assertIn("kairos_auto_deploy", emit_kinds)

    def test_auto_deploy_already_up_to_date_skips_pending(self) -> None:
        approvals = self._approvals_mock()
        svc, _, _, _ = _make_service(approvals=approvals)
        decision = TickDecision(action="auto_deploy")
        run_results = [
            MagicMock(returncode=0, stdout="same_sha\n"),
            MagicMock(returncode=0, stdout="same_sha\n"),
        ]
        with patch("claw_v2.kairos.run_subprocess_bounded", side_effect=run_results):
            svc._handle_auto_deploy(decision)
        approvals.create.assert_not_called()

    def test_auto_deploy_no_approvals_and_disabled_errors(self) -> None:
        svc, _, _, _ = _make_service()
        decision = TickDecision(action="auto_deploy")
        run_results = [
            MagicMock(returncode=0, stdout="deploy_sha\n"),
            MagicMock(returncode=0, stdout="local_sha\n"),
        ]
        with patch("claw_v2.kairos.run_subprocess_bounded", side_effect=run_results) as runner:
            with self.assertRaises(RuntimeError):
                svc._handle_auto_deploy(decision)
        self.assertEqual(runner.call_count, 2)


class AutoPublishSocialDedupTests(unittest.TestCase):
    def test_already_published_keyed_on_source_not_text(self) -> None:
        # 2026-05-31 audit (R2): dedup must key on the stable wiki source
        # filename, not the nondeterministic tweet text (the LLM regenerates the
        # wording each tick, so an exact-text match never fires and the daemon
        # re-tweets the same article every tick).
        svc, _, _, observe = _make_service()
        observe.recent_events.return_value = [
            {
                "event_type": "kairos_auto_publish_social",
                "payload": {"success": True, "source": "article.md", "tweet": "First wording"},
            }
        ]
        # Same source article already posted (with different wording) -> duplicate.
        self.assertTrue(svc._already_published_social("article.md"))
        # A different source article -> not a duplicate.
        self.assertFalse(svc._already_published_social("other.md"))


if __name__ == "__main__":
    unittest.main()
