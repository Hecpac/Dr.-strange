from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.action_events import load_events
from claw_v2.adapters.base import LLMRequest
from claw_v2.evidence_ledger import load_claims
from claw_v2.goal_contract import load_goals
from claw_v2.main import build_runtime
from claw_v2.tools import TIER_LOCAL_MUTATION, ToolDefinition, ToolRegistry
from claw_v2.types import LLMResponse


def fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content=f"<response>handled:{request.lane}</response>",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


class P0RuntimeWiringTests(unittest.TestCase):
    def test_tool_registry_populates_p0_jsonl_without_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = ToolRegistry(
                workspace_root=root / "workspace",
                telemetry_root=root / "telemetry",
            )
            registry.register(
                ToolDefinition(
                    name="TestWrite",
                    description="fake write",
                    allowed_agent_classes=("operator",),
                    handler=lambda args: {"ok": True, "path": args["path"]},
                    mutates_state=True,
                    tier=TIER_LOCAL_MUTATION,
                )
            )

            result = registry.execute("TestWrite", {"path": "out.txt"}, agent_class="operator")

            self.assertEqual(result["ok"], True)
            self.assertEqual(len(load_goals(root / "telemetry")), 1)
            self.assertEqual(len(load_claims(root / "telemetry")), 1)
            events = load_events(root / "telemetry")
            self.assertEqual(
                [event.event_type for event in events],
                ["action_proposed", "claim_recorded", "action_executed"],
            )
            self.assertEqual(events[2].originating_event_id, events[0].event_id)

    def test_tool_registry_records_failed_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = ToolRegistry(
                workspace_root=root / "workspace",
                telemetry_root=root / "telemetry",
            )

            def fail(_: dict) -> dict:
                raise RuntimeError("boom")

            registry.register(
                ToolDefinition(
                    name="FailingTool",
                    description="fake failure",
                    allowed_agent_classes=("operator",),
                    handler=fail,
                    tier=TIER_LOCAL_MUTATION,
                )
            )

            with self.assertRaises(RuntimeError):
                registry.execute("FailingTool", {}, agent_class="operator")

            events = load_events(root / "telemetry")
            self.assertEqual(
                [event.event_type for event in events],
                ["action_proposed", "claim_recorded", "action_failed"],
            )
            self.assertEqual(events[2].originating_event_id, events[0].event_id)
            self.assertEqual(len(load_claims(root / "telemetry")), 1)

    def test_autonomous_task_populates_p0_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "TELEMETRY_ROOT": str(root / "telemetry"),
                "WORKER_PROVIDER": "anthropic",
                "VERIFIER_PROVIDER": "anthropic",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                response = runtime.bot._task_handler.start_autonomous_task(
                    "tg-1",
                    "corrige el bug del login",
                    mode="coding",
                )
                self.assertIn("Tarea autónoma iniciada", response)
                task_id = response.split("`", maxsplit=2)[1]
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))

                goals = load_goals(root / "telemetry")
                claims = load_claims(root / "telemetry")
                events = load_events(root / "telemetry")

            self.assertGreaterEqual(len(goals), 1)
            self.assertGreaterEqual(len(claims), 2)
            self.assertIn("action_proposed", {event.event_type for event in events})
            self.assertIn("action_executed", {event.event_type for event in events})


if __name__ == "__main__":
    unittest.main()
