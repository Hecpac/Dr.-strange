"""Wave 2.6 wiring test: the factory built in main.py wires Kairos to
a real AgentLoop driven by sub_agents.dispatch_typed and observe spending.

Verifies the integration end-to-end without booting the full runtime:
- factory(goal_id, project_id, milestone_id) returns an AgentLoop
- Loop's executor calls sub_agents.dispatch_typed with worker lane
- Loop's cost_tracker reads from observe.spending_today
- Loop's max_cost_usd / max_iterations are populated
- KairosService._handle_run_agent_loop drives factory + emits outcome event
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock

from claw_v2.agent_loop import AgentLoop
from claw_v2.agents import SubAgentResult
from claw_v2.kairos import KairosService, TickDecision
from claw_v2.main import _build_kairos_agent_loop_factory


def _stub_subagent_result(status: str = "succeeded", summary: str = "done") -> SubAgentResult:
    return SubAgentResult(status=status, summary=summary)


class KairosAgentLoopFactoryTests(unittest.TestCase):
    def test_factory_returns_an_agent_loop_with_budget_and_iteration_caps(self) -> None:
        sub_agents = MagicMock()
        observe = MagicMock()
        observe.spending_today.return_value = {"total": 0.5}

        factory = _build_kairos_agent_loop_factory(sub_agents=sub_agents, observe=observe)
        loop = factory("g1", "p1", "m1")

        self.assertIsInstance(loop, AgentLoop)
        self.assertEqual(loop.max_iterations, 3)
        self.assertEqual(loop.max_cost_usd, 10.0)
        self.assertIsNotNone(loop.cost_tracker)
        # cost_tracker reads observe.spending_today["total"]
        self.assertAlmostEqual(loop.cost_tracker(), 0.5)

    def test_loop_executor_dispatches_to_worker_subagent_with_plan(self) -> None:
        sub_agents = MagicMock()
        sub_agents.list_agents = MagicMock(return_value=[])  # no overrides — keep default
        sub_agents.dispatch_typed.return_value = _stub_subagent_result()
        observe = MagicMock()
        observe.spending_today.return_value = {"total": 0.0}

        factory = _build_kairos_agent_loop_factory(sub_agents=sub_agents, observe=observe)
        loop = factory("g_landing", None, None)

        outcome = loop.run("Ship hero copy")

        self.assertEqual(outcome.status, "passed")
        sub_agents.dispatch_typed.assert_called_once_with("rook", "Ship hero copy", lane="worker")

    def test_loop_retries_with_critique_when_executor_returns_failure(self) -> None:
        sub_agents = MagicMock()
        sub_agents.list_agents = MagicMock(return_value=[])
        sub_agents.dispatch_typed.side_effect = [
            _stub_subagent_result(status="failed", summary="missing fixture"),
            _stub_subagent_result(status="succeeded", summary="ok now"),
        ]
        observe = MagicMock()
        observe.spending_today.return_value = {"total": 0.0}

        factory = _build_kairos_agent_loop_factory(sub_agents=sub_agents, observe=observe)
        loop = factory("g", None, None)

        outcome = loop.run("first attempt")

        self.assertEqual(outcome.status, "passed")
        self.assertEqual(sub_agents.dispatch_typed.call_count, 2)
        # Second call should pass the critique-derived plan, NOT the original goal,
        # because critic is wired and history[-1].critique is non-empty.
        second_args = sub_agents.dispatch_typed.call_args_list[1]
        plan_arg = second_args.args[1] if len(second_args.args) > 1 else second_args.kwargs.get("instruction")
        self.assertIn("Iter 1", plan_arg)
        self.assertIn("Try a different angle", plan_arg)

    def test_factory_falls_back_to_first_worker_when_default_agent_missing(self) -> None:
        # default is "rook"; if list_agents reports no rook but reports a worker
        # lane agent, the factory uses that instead.
        sub_agents = MagicMock()
        worker_stub = MagicMock(name="ix", lane="worker")
        worker_stub.name = "ix"
        worker_stub.lane = "worker"
        sub_agents.list_agents = MagicMock(return_value=[worker_stub])
        sub_agents.dispatch_typed.return_value = _stub_subagent_result()
        observe = MagicMock()
        observe.spending_today.return_value = {"total": 0.0}

        factory = _build_kairos_agent_loop_factory(sub_agents=sub_agents, observe=observe)
        loop = factory("g", None, None)
        loop.run("plan")

        sub_agents.dispatch_typed.assert_called_once()
        self.assertEqual(sub_agents.dispatch_typed.call_args.args[0], "ix")

    def test_kairos_run_agent_loop_uses_factory_end_to_end(self) -> None:
        # Wire factory + KairosService and trigger _handle_run_agent_loop.
        sub_agents = MagicMock()
        sub_agents.list_agents = MagicMock(return_value=[])
        sub_agents.dispatch_typed.return_value = _stub_subagent_result()
        observe = MagicMock()
        observe.spending_today.return_value = {"total": 0.0}

        factory = _build_kairos_agent_loop_factory(sub_agents=sub_agents, observe=observe)

        heartbeat = MagicMock()
        heartbeat.collect.return_value = MagicMock(
            pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={}
        )
        svc = KairosService(
            router=MagicMock(),
            heartbeat=heartbeat,
            observe=observe,
            sub_agents=sub_agents,
            agent_loop_factory=factory,
        )

        decision = TickDecision(
            action="run_agent_loop",
            reason="milestone overdue",
            detail=json.dumps(
                {"goal_id": "g_landing", "project_id": "p_site", "goal_text": "Ship hero copy"}
            ),
        )
        svc._handle_run_agent_loop(decision)

        sub_agents.dispatch_typed.assert_called_once_with("rook", "Ship hero copy", lane="worker")
        emit_calls = [
            call for call in observe.emit.call_args_list
            if call.args[0] == "kairos_agent_loop_complete"
        ]
        self.assertEqual(len(emit_calls), 1)
        payload = emit_calls[0].kwargs["payload"]
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["goal_id"], "g_landing")
        self.assertEqual(payload["project_id"], "p_site")


if __name__ == "__main__":
    unittest.main()
