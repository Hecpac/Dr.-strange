from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content=f"handled:{request.lane}",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
        confidence=0.9,
        cost_estimate=0.02,
    )


class RuntimeTests(unittest.TestCase):
    def test_build_runtime_and_status_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.brain.handle_message("session-1", "hello")
                payload = runtime.bot.handle_text(user_id="123", session_id="session-1", text="/status")
                parsed = json.loads(payload)
                self.assertIn("brain:anthropic:claude-opus-4-6", parsed["lane_metrics"])

    def test_daemon_tick_runs_scheduled_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                tick = runtime.daemon.tick(now=1000)
                self.assertIn("heartbeat", tick.executed_jobs)
                self.assertIn("morning_brief", tick.executed_jobs)
                self.assertIn("daily_metrics", tick.executed_jobs)

    def test_brain_persists_anthropic_provider_session_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            seen_session_ids: list[str | None] = []

            def sessionful_anthropic(request: LLMRequest) -> LLMResponse:
                seen_session_ids.append(request.session_id)
                provider_session_id = request.session_id or "sdk-session-1"
                return LLMResponse(
                    content=f"handled:{request.lane}",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    confidence=0.9,
                    cost_estimate=0.02,
                    artifacts={"session_id": provider_session_id},
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=sessionful_anthropic)
                runtime.brain.handle_message("session-1", "hello")
                runtime.brain.handle_message("session-1", "hello again")
                self.assertEqual(seen_session_ids, [None, "sdk-session-1"])
                self.assertEqual(runtime.memory.get_provider_session("session-1", "anthropic"), "sdk-session-1")


if __name__ == "__main__":
    unittest.main()
