from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.jobs import JobService
from claw_v2.main import build_runtime
from claw_v2.task_ledger import TaskLedger
from claw_v2.types import LLMResponse


def fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content=f"<response>handled:{request.lane}</response>",
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
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "JUDGE_PROVIDER": "ollama",
                "JUDGE_MODEL": "",
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
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.brain.handle_message("session-1", "hello")
                payload = runtime.bot.handle_text(user_id="123", session_id="session-1", text="/status")
                parsed = json.loads(payload)
                self.assertIn("brain:anthropic:claude-opus-4-7", parsed["lane_metrics"])

    def test_build_runtime_wires_agent_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                self.assertIs(runtime.kairos.bus, runtime.bus)
                self.assertIs(runtime.kairos.approvals, runtime.approvals)
                self.assertIs(runtime.kairos.sub_agents, runtime.sub_agents)
                self.assertIs(runtime.kairos.auto_research, runtime.auto_research)
                self.assertTrue(runtime.coordinator.agent_registry)
                self.assertIn("hex", runtime.coordinator.agent_registry)
                self.assertIsNotNone(runtime.heartbeat.registry_path)

                snapshot = runtime.heartbeat.emit()
                self.assertIn("hex", snapshot.agents)
                self.assertTrue(runtime.heartbeat.registry_path.exists())

                hex_def = runtime.sub_agents.get_agent("hex")
                eval_def = runtime.sub_agents.get_agent("eval")
                self.assertEqual((hex_def.provider, hex_def.model), ("openai", "gpt-5.5"))
                self.assertEqual((eval_def.provider, eval_def.model), ("anthropic", "claude-opus-4-7"))

    def test_build_runtime_bootstraps_agent_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                self.assertEqual(runtime.agent_workspace.root, root / "workspace")
                for name in runtime.agent_workspace.REQUIRED_FILES:
                    self.assertTrue((root / "workspace" / name).exists(), name)
                self.assertTrue((root / "workspace" / "memory").is_dir())
                self.assertIn("# Agent Workspace Context", runtime.brain.system_prompt)
                events = runtime.observe.recent_events(limit=5)
                self.assertTrue(any(event["event_type"] == "agent_workspace_bootstrap" for event in events))

    def test_runtime_registry_writes_to_agent_state_not_tracked_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace_registry = root / "workspace" / "claw_v2" / "AGENTS.md"
            workspace_registry.parent.mkdir(parents=True)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                self.assertEqual(runtime.heartbeat.registry_path, root / "agents" / "AGENTS.md")
                runtime.heartbeat.emit()
                self.assertTrue((root / "agents" / "AGENTS.md").exists())
                self.assertFalse(workspace_registry.exists())

    def test_daemon_tick_runs_scheduled_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "WORKER_PROVIDER": "anthropic",  # isolate from host env
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                tick = runtime.daemon.tick(now=1000)
                self.assertIn("heartbeat", tick.executed_jobs)
                self.assertIn("morning_brief", tick.executed_jobs)
                self.assertIn("daily_metrics", tick.executed_jobs)

    def test_build_runtime_wires_generic_job_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "WORKER_PROVIDER": "anthropic",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                job = runtime.job_service.enqueue(kind="pipeline.issue", payload={"issue_id": "HEC-1"})

                self.assertEqual(runtime.job_service.get(job.job_id).kind, "pipeline.issue")
                self.assertEqual(runtime.bot.job_service.get(job.job_id).status, "queued")

    def test_build_runtime_resumes_interrupted_autonomous_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "data" / "claw.db"
            seed_ledger = TaskLedger(db_path)
            seed_ledger.create(
                task_id="tg-123:interrupted",
                session_id="tg-123",
                objective="corrige el bug del login",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="claude-sonnet-4-6",
                status="running",
                route={"channel": "telegram", "external_session_id": "123"},
                metadata={"autonomous": True},
            )
            seed_ledger._conn.close()
            seed_jobs = JobService(db_path)
            job = seed_jobs.enqueue(
                kind="coordinator.autonomous_task",
                payload={
                    "task_id": "tg-123:interrupted",
                    "session_id": "tg-123",
                    "objective": "corrige el bug del login",
                    "mode": "coding",
                },
                resume_key="coordinator:tg-123:interrupted",
                metadata={"runtime": "coordinator"},
            )
            seed_jobs.claim(job.job_id, worker_id="previous-runtime")
            seed_jobs._conn.close()
            env = {
                "DB_PATH": str(db_path),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "WORKER_PROVIDER": "anthropic",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                self.assertTrue(runtime.bot._task_handler.wait_for_task("tg-123:interrupted", timeout=2))

                record = runtime.task_ledger.get("tg-123:interrupted")
                self.assertEqual(record.status, "succeeded")
                self.assertEqual(record.metadata["resume_reason"], "startup_recovery")
                self.assertEqual(record.metadata["resume_count"], 1)
                self.assertEqual(record.metadata["generic_job_id"], job.job_id)
                recovered_job = runtime.job_service.get(job.job_id)
                self.assertEqual(recovered_job.status, "completed")
                self.assertEqual(recovered_job.worker_id, "coordinator")
                self.assertEqual(recovered_job.attempts, 2)
                self.assertEqual(record.artifacts["lifecycle"]["plan"]["objective"], "corrige el bug del login")
                self.assertEqual(record.artifacts["lifecycle"]["execution"]["status"], "resumed")
                self.assertEqual(record.artifacts["lifecycle"]["job"]["lifecycle_status"], "completed")
                state = runtime.memory.get_session_state("tg-123")
                self.assertEqual(state["active_object"]["active_task"]["status"], "completed")

    def test_build_runtime_registers_sites_and_sub_agent_jobs_from_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_config = root / "runtime.yml"
            runtime_config.write_text(
                "monitored_sites:\n"
                "  - name: status page\n"
                "    url: https://status.example.com\n"
                "    interval_seconds: 900\n"
                "scheduled_sub_agents:\n"
                "  - agent: alma\n"
                "    skill: daily-brief\n"
                "    interval_seconds: 7200\n"
                "    lane: worker\n",
                encoding="utf-8",
            )
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "RUNTIME_CONFIG_PATH": str(runtime_config),
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                job_names = {job.name for job in runtime.scheduler.list_jobs()}

        self.assertIn("site_monitor_status_page", job_names)
        self.assertIn("alma_daily_brief", job_names)
        self.assertIn("learning_soul_suggestions", job_names)
        self.assertNotIn("site_monitor_premiumhome_design", job_names)

    def test_brain_persists_anthropic_provider_session_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            }
            seen_session_ids: list[str | None] = []

            def sessionful_anthropic(request: LLMRequest) -> LLMResponse:
                seen_session_ids.append(request.session_id)
                provider_session_id = request.session_id or "sdk-session-1"
                return LLMResponse(
                    content=f"<response>handled:{request.lane}</response>",
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
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            seen_prompts: list[object] = []

            def multimodal_anthropic(request: LLMRequest) -> LLMResponse:
                seen_prompts.append(request.prompt)
                return LLMResponse(
                    content="<response>handled:brain</response>",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                    confidence=0.9,
                    cost_estimate=0.02,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=multimodal_anthropic)
                runtime.brain.wiki = None
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

    def test_brain_uses_telegram_scoped_lessons_for_natural_language_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            seen_prompts: list[object] = []

            def capture_anthropic(request: LLMRequest) -> LLMResponse:
                seen_prompts.append(request.prompt)
                return LLMResponse(
                    content="<response>handled</response>",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=capture_anthropic)
                runtime.brain.wiki = None
                runtime.brain.learning.record(
                    task_type="telegram_message",
                    task_id="tg:1",
                    description="Handled refund request",
                    approach="brain.handle_message",
                    outcome="success",
                    lesson="Mention the refund flow first.",
                )
                runtime.brain.learning.record(
                    task_type="browse",
                    task_id="br:1",
                    description="Browse refund docs failed",
                    approach="strategy=public",
                    outcome="failure",
                    lesson="Retry with a different backend.",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="refund status")

                prompt = seen_prompts[-1]
                self.assertIsInstance(prompt, str)
                self.assertIn("Mention the refund flow first.", prompt)
                self.assertNotIn("Retry with a different backend.", prompt)

    def test_brain_uses_wiki_query_for_strong_question_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            seen_prompts: list[object] = []

            def capture_anthropic(request: LLMRequest) -> LLMResponse:
                seen_prompts.append(request.prompt)
                return LLMResponse(
                    content="<response>handled</response>",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=capture_anthropic)
                runtime.brain.wiki = MagicMock()
                runtime.brain.wiki.search.return_value = [
                    {"title": "Refund Policy", "similarity": 0.61, "snippet": "Refunds take 5 days."},
                ]
                runtime.brain.wiki.query.return_value = "Use [[refund-policy]] for the canonical answer."

                runtime.bot.handle_text(user_id="123", session_id="s1", text="¿Cuál es la política de refund?")

                prompt = seen_prompts[-1]
                self.assertIsInstance(prompt, str)
                self.assertIn("# Wiki answer", prompt)
                self.assertIn("[[refund-policy]]", prompt)
                runtime.brain.wiki.query.assert_called_once_with("¿Cuál es la política de refund?", archive=False)

    def test_brain_resume_only_catches_up_unsynced_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
            }
            seen_prompts: list[object] = []
            seen_session_ids: list[str | None] = []

            def sessionful_anthropic(request: LLMRequest) -> LLMResponse:
                seen_prompts.append(request.prompt)
                seen_session_ids.append(request.session_id)
                provider_session_id = request.session_id or "sdk-session-1"
                return LLMResponse(
                    content="<response>handled:brain</response>",
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

                for idx in range(4):
                    runtime.memory.store_message("session-1", "user", f"shortcut-user-{idx}")
                    runtime.memory.store_message("session-1", "assistant", f"shortcut-assistant-{idx}")

                runtime.brain.handle_message("session-1", "follow up")

                self.assertEqual(seen_session_ids, [None, "sdk-session-1"])
                prompt = seen_prompts[-1]
                self.assertIsInstance(prompt, str)
                self.assertIn("shortcut-user-0", prompt)
                self.assertIn("shortcut-assistant-3", prompt)
                self.assertNotIn("user: hello", prompt)
                self.assertTrue(prompt.rstrip().endswith("follow up"))


    def test_cost_gate_blocks_when_limit_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
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
                    model="claude-opus-4-7",
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
                "PIPELINE_STATE_ROOT": str(root / "pipeline"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                self.assertIsNotNone(runtime.bot._computer_handler.computer)
                # Mock the screenshot since we can't run screencapture in tests
                runtime.bot._computer_handler.computer.capture_screenshot = lambda: {"data": "test_data", "media_type": "image/png"}
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/screen")
                self.assertIn("screenshot_data", result)


if __name__ == "__main__":
    unittest.main()
