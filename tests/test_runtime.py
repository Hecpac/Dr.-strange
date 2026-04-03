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
    def test_build_runtime_wires_ollama_transport_for_secondary_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "JUDGE_PROVIDER": "ollama",
            }

            def ollama_transport(request: LLMRequest) -> LLMResponse:
                self.assertEqual(request.lane, "judge")
                self.assertEqual(request.model, "gemma4")
                return LLMResponse(
                    content="ollama:ok",
                    lane=request.lane,
                    provider="ollama",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic, ollama_transport=ollama_transport)
                response = runtime.router.ask("classify", lane="judge", evidence_pack={"data": "x"})
                self.assertEqual(response.provider, "ollama")
                self.assertEqual(response.model, "gemma4")

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

    def test_multimodal_message_is_forwarded_and_memory_stores_text_summary(self) -> None:
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
            seen_prompts: list[object] = []

            def multimodal_anthropic(request: LLMRequest) -> LLMResponse:
                seen_prompts.append(request.prompt)
                return LLMResponse(
                    content="handled:brain",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    confidence=0.9,
                    cost_estimate=0.02,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=multimodal_anthropic)
                response = runtime.bot.handle_multimodal(
                    user_id="123",
                    session_id="session-1",
                    content_blocks=[
                        {"type": "text", "text": "que ves en esta imagen?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "cG5n",
                            },
                        },
                    ],
                    memory_text="[Imagen adjunta]\nque ves en esta imagen?",
                )

                self.assertEqual(response, "handled:brain")
                self.assertEqual(len(seen_prompts), 1)
                prompt = seen_prompts[0]
                self.assertIsInstance(prompt, list)
                prompt_blocks = prompt
                self.assertIn("# Current input", prompt_blocks[0]["text"])
                self.assertEqual(prompt_blocks[1]["text"], "que ves en esta imagen?")
                self.assertEqual(prompt_blocks[2]["type"], "image")

                recent = runtime.memory.get_recent_messages("session-1")
                self.assertEqual(recent[-2]["content"], "[Imagen adjunta]\nque ves en esta imagen?")


    def test_cost_gate_blocks_when_limit_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "DAILY_COST_LIMIT": "0.10",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                # First message should succeed
                response1 = runtime.brain.handle_message("session-1", "hello")
                self.assertEqual(response1.content, "handled:brain")

                # Emit enough cost to exceed the $0.10 limit
                runtime.observe.emit(
                    "llm_response",
                    lane="brain",
                    provider="anthropic",
                    model="claude-opus-4-6",
                    payload={"cost_estimate": 0.10},
                )

                # Second message should be blocked
                response2 = runtime.brain.handle_message("session-1", "world")
                self.assertEqual(response2.artifacts.get("blocked_by"), "daily_cost_gate")
                self.assertEqual(response2.provider, "none")


    def test_computer_service_wired_and_screen_command_works(self) -> None:
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
                self.assertIsNotNone(runtime.bot.computer)
                # Mock the screenshot since we can't run screencapture in tests
                runtime.bot.computer.capture_screenshot = lambda: {"data": "test_data", "media_type": "image/png"}
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/screen")
                self.assertIn("screenshot_data", result)


if __name__ == "__main__":
    unittest.main()
