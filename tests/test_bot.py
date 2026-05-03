from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.approval_gate import ApprovalPending
from claw_v2.agents import AgentDefinition, ExperimentRecord
from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.eval_mocks import scripted_experiment_runner
from claw_v2.github import PullRequestResult
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content="handled",
        lane=request.lane,
        provider="anthropic",
        model=request.model,
    )


class BotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pipeline_state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._pipeline_state_tmp.cleanup)
        patcher = patch.dict(
            os.environ,
            {"PIPELINE_STATE_ROOT": str(Path(self._pipeline_state_tmp.name) / "pipeline")},
            clear=False,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_help_command_surfaces_main_and_topic_specific_commands(self) -> None:
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

                overview = runtime.bot.handle_text(user_id="123", session_id="s1", text="/help")
                self.assertIn("/approvals", overview)
                self.assertIn("/help pipeline", overview)
                self.assertIn("/terminal_list", overview)
                self.assertIn("/spending", overview)

                pipeline_help = runtime.bot.handle_text(user_id="123", session_id="s1", text="/help pipeline")
                self.assertIn("/pipeline_status", pipeline_help)
                self.assertIn("/pipeline_merge <issue_id>", pipeline_help)

                agents_help = runtime.bot.handle_text(user_id="123", session_id="s1", text="/help agents")
                self.assertIn("/agent_create", agents_help)
                self.assertIn("/agent_pr", agents_help)

                traces_help = runtime.bot.handle_text(user_id="123", session_id="s1", text="/help traces")
                self.assertIn("/traces [limit]", traces_help)
                self.assertIn("/trace <trace_id> [limit]", traces_help)

                unknown = runtime.bot.handle_text(user_id="123", session_id="s1", text="/help desconocido")
                self.assertIn("Tema de ayuda no reconocido", unknown)

    def test_spending_command_returns_daily_llm_decision_breakdown(self) -> None:
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
                runtime.observe.emit(
                    "llm_decision",
                    lane="brain",
                    provider="anthropic",
                    model="claude-opus-4-7",
                    payload={"cost_estimate": 0.12},
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/spending")
                payload = json.loads(reply)

                self.assertAlmostEqual(payload["total"], 0.12)
                self.assertEqual(payload["by_lane"], {"brain": 0.12})

    def test_freeze_command_blocks_tool_dispatch_until_unfreeze(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            target = workspace / "note.txt"
            target.write_text("hello", encoding="utf-8")
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(workspace),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/freeze")
                self.assertIn("Freeze activado", reply)
                with self.assertRaises(PermissionError):
                    runtime.tool_registry.execute("Read", {"path": str(target)}, agent_class="researcher")

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/unfreeze")
                self.assertIn("Freeze desactivado", reply)
                result = runtime.tool_registry.execute("Read", {"path": str(target)}, agent_class="researcher")
                self.assertEqual(result["content"], "hello")

    def test_command_router_preserves_terminal_usage_response(self) -> None:
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

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read")

                self.assertEqual(reply, "usage: /terminal_read <session_id> [offset]")

    def test_trace_commands_return_recent_index_and_replay(self) -> None:
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
                runtime.bot.handle_text(user_id="123", session_id="s1", text="hola")

                traces = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/traces 5"))
                self.assertGreaterEqual(len(traces["traces"]), 1)
                trace_id = traces["traces"][0]["trace_id"]

                replay = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/trace {trace_id}"))
                self.assertEqual(replay["trace_id"], trace_id)
                self.assertGreaterEqual(replay["event_count"], 1)
                self.assertTrue(any(event["event_type"] == "llm_response" for event in replay["events"]))

    def test_trace_replay_redacts_sensitive_payloads(self) -> None:
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
                trace_id = "trace-redact-test"
                runtime.observe.emit(
                    "synthetic_secret",
                    lane="brain",
                    provider="anthropic",
                    model="test",
                    trace_id=trace_id,
                    payload={"note": "user typed /approve abc-123 secret-token-xyz to confirm"},
                )
                replay = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/trace {trace_id}")
                self.assertNotIn("secret-token-xyz", replay)
                self.assertIn("[REDACTED]", replay)

    def test_social_publish_requires_approval_before_publishing(self) -> None:
        from unittest.mock import MagicMock
        from claw_v2.content import PostDraft
        from claw_v2.social import PublishResult

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
                draft = PostDraft(account="acme", platform="x", text="hello", hashtags=["#ai"])
                runtime.bot.content_engine = MagicMock()
                runtime.bot.content_engine.generate_batch.return_value = [draft]
                publisher_mock = MagicMock()
                publisher_mock.publish.return_value = PublishResult(
                    platform="x", account="acme", post_id="42",
                    url="https://x.com/acme/42", published_at="2026-04-26",
                )
                runtime.bot.social_publisher = publisher_mock

                first = runtime.bot.handle_text(user_id="123", session_id="s1", text="/social_publish acme")
                payload = json.loads(first)

                self.assertEqual(payload["status"], "approval_required")
                self.assertIn("approval_id", payload)
                self.assertIn("approval_token", payload)
                self.assertEqual(publisher_mock.publish.call_count, 0)

                approval_id = payload["approval_id"]
                token = payload["approval_token"]
                second = runtime.bot.handle_text(
                    user_id="123", session_id="s1",
                    text=f"/social_approve {approval_id} {token}",
                )

                self.assertEqual(publisher_mock.publish.call_count, 1)
                self.assertIn("post_id", second)

    def test_stream_interrupted_marks_running_checkpoint_with_partial(self) -> None:
        from unittest.mock import MagicMock
        from claw_v2.adapters.base import StreamInterruptedError

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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.side_effect = StreamInterruptedError(
                    "OpenAI stream idle timeout - partial response received",
                    partial_output="parcial sintetizado: paso 1",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="corrige el bug del login",
                )
                self.assertIn("Tarea autónoma iniciada", reply)
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))

                record = runtime.task_ledger.get(task_id)
                self.assertEqual(record.status, "running")
                self.assertEqual(record.verification_status, "interrupted")
                self.assertIn("parcial sintetizado", record.artifacts.get("partial_output", ""))

                events = [
                    e for e in runtime.observe.recent_events(limit=50)
                    if e["event_type"] == "stream_interrupted_checkpointed"
                ]
                self.assertGreaterEqual(len(events), 1)

    def test_quality_command_returns_metrics(self) -> None:
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
                runtime.task_ledger.create(
                    task_id="tg-1:done",
                    session_id="tg-1",
                    objective="ship",
                    runtime="coordinator",
                    status="running",
                )
                runtime.task_ledger.mark_terminal(
                    "tg-1:done",
                    status="succeeded",
                    summary="done",
                    verification_status="passed",
                    artifacts={"diff": "+1 -0"},
                )
                response = runtime.bot.handle_text(user_id="123", session_id="tg-1", text="/quality")
                payload = json.loads(response)
                self.assertIn("tasks", payload)
                self.assertIn("quality", payload)
                self.assertIn("top_failure_reasons", payload)
                self.assertIn("provider_health", payload)
                self.assertGreaterEqual(payload["tasks"]["verified_success"], 1)

    def test_diagnose_task_explains_missing_evidence(self) -> None:
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
                runtime.task_ledger.create(
                    task_id="tg-1:nb",
                    session_id="tg-1",
                    objective="crear cuaderno IA",
                    runtime="nlm_natural_language",
                    status="running",
                    metadata={"intent": "create_notebook"},
                )
                runtime.task_ledger.mark_running_checkpoint(
                    "tg-1:nb",
                    summary="empezó pero falta cuaderno",
                    error="missing notebook artifact",
                    verification_status="missing_evidence",
                    artifacts={"handler_result": "started"},
                )
                response = runtime.bot.handle_text(
                    user_id="123", session_id="tg-1", text="/diagnose_task tg-1:nb",
                )
                self.assertIn("tg-1:nb", response)
                self.assertIn("missing_evidence", response)
                self.assertIn("evidencia", response.lower())
                self.assertIn("/task_resume tg-1:nb", response)

    def test_meta_questions_do_not_start_new_task(self) -> None:
        from unittest.mock import MagicMock

        meta_questions = [
            "¿Completaste la tarea?",
            "Porque no pudiste completar la tarea?",
            "Qué pasó con el job?",
            "Crea el cuaderno que te pedí",
            "Continúa con eso",
            "Retoma la anterior",
        ]
        for text in meta_questions:
            with tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                env = {
                    "DB_PATH": str(root / "data" / "claw.db"),
                    "WORKSPACE_ROOT": str(root / "workspace"),
                    "AGENT_STATE_ROOT": str(root / "agents"),
                    "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                    "APPROVALS_ROOT": str(root / "approvals"),
                    "TELEGRAM_ALLOWED_USER_ID": "123",
                    # This test exercises the canned task_intent router
                    # specifically; opt out of the brain-bypass hotfix flag
                    # that disables it by default in production.
                    "CLAW_DISABLE_TASK_INTENT_ROUTER": "0",
                }
                with patch.dict(os.environ, env, clear=False):
                    runtime = build_runtime(anthropic_executor=fake_anthropic)
                    runtime.task_ledger.create(
                        task_id="tg-1:prev",
                        session_id="tg-1",
                        objective="cuaderno previo",
                        runtime="coordinator",
                        status="running",
                        metadata={"autonomous": True},
                    )
                    runtime.bot.coordinator = MagicMock()
                    runtime.bot._nlm_handler = MagicMock()
                    runtime.bot._nlm_handler.natural_language_response.return_value = None
                    runtime.bot._nlm_handler.dispatch.return_value = "nlm dispatch should not run"
                    runtime.memory.update_session_state("tg-1", autonomy_mode="autonomous")

                    before = len(runtime.task_ledger.list(session_id="tg-1"))
                    runtime.bot.handle_text(user_id="123", session_id="tg-1", text=text)
                    after = len(runtime.task_ledger.list(session_id="tg-1"))

                    self.assertEqual(after, before, msg=f"text {text!r} created a new task")
                    runtime.bot.coordinator.run.assert_not_called()
                    runtime.bot._nlm_handler.natural_language_response.assert_not_called()

    def test_failure_diagnostic_response_explains_previous_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "CLAW_DISABLE_TASK_INTENT_ROUTER": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.task_ledger.create(
                    task_id="tg-1:nb-prev",
                    session_id="tg-1",
                    objective="crear cuaderno IA energia",
                    runtime="coordinator",
                    status="running",
                    metadata={"autonomous": True},
                )
                runtime.task_ledger.mark_terminal(
                    "tg-1:nb-prev",
                    status="failed",
                    error="codex_timeout: provider unavailable",
                    verification_status="failed",
                    summary="provider down",
                )
                reply = runtime.bot.handle_text(
                    user_id="123", session_id="tg-1",
                    text="¿Por qué falló la tarea?",
                )
                self.assertIn("tg-1:nb-prev", reply)
                self.assertIn("codex_timeout", reply)
                self.assertIn("/task_resume", reply)

    def test_pipeline_merge_requires_approval_before_merging(self) -> None:
        from unittest.mock import MagicMock
        from claw_v2.pipeline import PipelineRun

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
                pipeline_mock = MagicMock()
                pipeline_mock._load_run.return_value = PipelineRun(
                    issue_id="HEC-9", branch_name="feat/hec-9",
                    repo_root=str(root), status="pr_created",
                    pr_url="https://github.com/owner/repo/pull/9",
                )
                pipeline_mock.merge_and_close.return_value = PipelineRun(
                    issue_id="HEC-9", branch_name="feat/hec-9",
                    repo_root=str(root), status="done",
                    pr_url="https://github.com/owner/repo/pull/9",
                )
                runtime.bot.pipeline = pipeline_mock

                first = runtime.bot.handle_text(user_id="123", session_id="s1", text="/pipeline_merge HEC-9")
                payload = json.loads(first)

                self.assertEqual(payload["status"], "approval_required")
                self.assertEqual(payload["issue"], "HEC-9")
                self.assertEqual(pipeline_mock.merge_and_close.call_count, 0)

                approval_id = payload["approval_id"]
                token = payload["approval_token"]
                second = runtime.bot.handle_text(
                    user_id="123", session_id="s1",
                    text=f"/pipeline_merge_confirm {approval_id} {token}",
                )

                self.assertEqual(pipeline_mock.merge_and_close.call_count, 1)
                self.assertIn("done", second)

    def test_pipeline_merge_blocks_when_pr_not_created(self) -> None:
        from unittest.mock import MagicMock
        from claw_v2.pipeline import PipelineRun

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
                pipeline_mock = MagicMock()
                pipeline_mock._load_run.return_value = PipelineRun(
                    issue_id="HEC-9", branch_name="feat/hec-9",
                    repo_root=str(root), status="awaiting_approval", pr_url=None,
                )
                runtime.bot.pipeline = pipeline_mock

                response = runtime.bot.handle_text(user_id="123", session_id="s1", text="/pipeline_merge HEC-9")
                payload = json.loads(response)

                self.assertEqual(payload["status"], "not_mergeable")
                self.assertEqual(pipeline_mock.merge_and_close.call_count, 0)

    def test_social_publish_rejects_invalid_approval_token(self) -> None:
        from unittest.mock import MagicMock
        from claw_v2.content import PostDraft

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
                draft = PostDraft(account="acme", platform="x", text="hi", hashtags=[])
                runtime.bot.content_engine = MagicMock()
                runtime.bot.content_engine.generate_batch.return_value = [draft]
                publisher_mock = MagicMock()
                runtime.bot.social_publisher = publisher_mock

                first = runtime.bot.handle_text(user_id="123", session_id="s1", text="/social_publish acme")
                approval_id = json.loads(first)["approval_id"]

                response = runtime.bot.handle_text(
                    user_id="123", session_id="s1",
                    text=f"/social_approve {approval_id} wrong-token",
                )
                self.assertEqual(response, "approval rejected")
                self.assertEqual(publisher_mock.publish.call_count, 0)

    def test_trace_command_reports_missing_trace(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/trace missing-trace")
                self.assertEqual(reply, "trace not found: missing-trace")

    def test_bot_persists_visible_fallback_instead_of_no_result(self) -> None:
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

            def no_result_anthropic(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="(no result)",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=no_result_anthropic)
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="hola")

                self.assertEqual(reply, "Recibido. ¿Qué quieres que haga con esto?")
                recent = runtime.memory.get_recent_messages("s1", limit=2)
                self.assertEqual(recent[-1]["content"], "Recibido. ¿Qué quieres que haga con esto?")
                outcomes = runtime.memory.search_past_outcomes("fallback", task_type="telegram_message")
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["outcome"], "failure")
                self.assertIn("clarifying question", outcomes[0]["lesson"])

    def test_bot_records_successful_natural_language_outcome(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="explícame el login")

                self.assertEqual(reply, "handled")
                outcomes = runtime.memory.search_past_outcomes("login", task_type="telegram_message")
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["outcome"], "success")
                self.assertIn("usable reply", outcomes[0]["lesson"])

    def test_chrome_commands_report_degraded_capability_reason(self) -> None:
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
                runtime.bot.set_capability_status(
                    "chrome_cdp",
                    available=False,
                    reason="Chrome no pudo iniciar en el puerto configurado.",
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_pages")

                self.assertIn("módulo de navegación", reply)
                self.assertIn("Chrome no pudo iniciar", reply)

    def test_computer_command_reports_degraded_capability_reason(self) -> None:
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
                runtime.bot.set_capability_status(
                    "computer_use",
                    available=False,
                    reason="Computer Use está desactivado por healthcheck.",
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/computer revisa la pantalla")

                self.assertIn("módulo de control de escritorio", reply)
                self.assertIn("healthcheck", reply)

    def test_autonomy_mode_commands_update_session_state(self) -> None:
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

                initial = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy"))
                self.assertEqual(initial["autonomy_mode"], "assisted")

                updated = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                )
                self.assertEqual(updated["autonomy_mode"], "autonomous")

                policy = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy_policy"))
                self.assertEqual(policy["autonomy_mode"], "autonomous")
                self.assertIn("coding", policy["automatic_coordinator_modes"])
                self.assertIn("edit", policy["allowed_task_actions"])
                self.assertIn("commit", policy["allowed_task_actions"])
                self.assertIn("push", policy["allowed_task_actions"])
                self.assertNotIn("commit", policy["approval_required_actions"])
                self.assertIn("deploy", policy["blocked_actions"])
                self.assertIn("deploy", policy["action_patterns"])
                self.assertIn("commit", policy["task_action_patterns"])
                self.assertIn("push", policy["task_action_patterns"])

                invalid = runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy unsafe")
                self.assertEqual(invalid, "autonomy mode must be one of: manual, assisted, autonomous")

    def test_natural_language_autonomy_grant_sets_autonomous_mode(self) -> None:
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

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Tienes toda la autonomia para terminar el plan no tienes que pedirme autorizacion en cada fase",
                )

                self.assertIn("Autonomía activada", reply)
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["autonomy_mode"], "autonomous")
                self.assertIn("push", state["active_object"]["autonomy_grant"]["allowed_without_phase_approval"])

    def test_telegram_sessions_default_to_assisted_until_explicit_override(self) -> None:
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

                runtime.bot.handle_text(user_id="123", session_id="tg-123", text="hola")
                state = runtime.memory.get_session_state("tg-123")
                self.assertEqual(state["autonomy_mode"], "assisted")
                self.assertEqual(state["active_object"]["autonomy_configured"]["source"], "telegram_default")

                runtime.bot.handle_text(user_id="123", session_id="tg-123", text="/autonomy autonomous")
                runtime.bot.handle_text(user_id="123", session_id="tg-123", text="hola de nuevo")
                state = runtime.memory.get_session_state("tg-123")
                self.assertEqual(state["autonomy_mode"], "autonomous")
                self.assertEqual(state["active_object"]["autonomy_configured"]["source"], "command")

    def test_option_followups_and_proceed_use_session_state(self) -> None:
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
            prompts: list[str] = []

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                prompts.append(prompt_text)
                if len(prompts) == 1:
                    content = "1. Revisar logs\n2. Corregir el bug de browse"
                else:
                    content = "handled"
                return LLMResponse(
                    content=content,
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)

                first = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="dame opciones para arreglar browse",
                )
                self.assertIn("1. Revisar logs", first)
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["last_options"], ["Revisar logs", "Corregir el bug de browse"])

                selected = runtime.bot.handle_text(user_id="123", session_id="s1", text="opción 2")
                self.assertEqual(selected, "handled")
                self.assertIn("El usuario seleccionó la opción 2.", prompts[-1])
                self.assertIn("Opción elegida: Corregir el bug de browse", prompts[-1])

                proceed = runtime.bot.handle_text(user_id="123", session_id="s1", text="procede")
                self.assertEqual(proceed, "handled")
                self.assertIn("Continúa con esta acción pendiente: Corregir el bug de browse", prompts[-1])

    @patch("claw_v2.browse_handler._jina_read")
    def test_browse_updates_active_object_in_session_state(self, mock_jina) -> None:
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
                mock_jina.return_value = "contenido"
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/browse https://example.com/path",
                )
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["mode"], "browse")
                self.assertEqual(state["active_object"]["kind"], "url")
                self.assertEqual(state["active_object"]["url"], "https://example.com/path")

    def test_autonomous_mode_adds_task_loop_contract_to_prompt(self) -> None:
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
            prompts: list[str] = []

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                prompts.append(prompt_text)
                return LLMResponse(
                    content="handled",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.coordinator = None
                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="corrige el bug del login y corre tests",
                )

                self.assertIn("# Autonomy contract", prompts[-1])
                self.assertIn("Mode: autonomous", prompts[-1])
                self.assertIn("Follow a short task loop internally", prompts[-1])
                self.assertIn("Batch multiple safe intermediate steps", prompts[-1])

    def test_pending_action_is_extracted_from_assistant_reply(self) -> None:
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
            prompts: list[str] = []

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                prompts.append(prompt_text)
                if len(prompts) == 1:
                    content = "Hecho.\nSiguiente paso: correr pytest -q"
                else:
                    content = "handled"
                return LLMResponse(
                    content=content,
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["pending_action"], "correr pytest -q")
                self.assertEqual(state["task_queue"][0]["summary"], "correr pytest -q")
                self.assertEqual(state["task_queue"][0]["status"], "pending")

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="procede")
                self.assertEqual(reply, "handled")
                self.assertIn("Continúa con esta acción pendiente: correr pytest -q", prompts[-1])
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["task_queue"][0]["status"], "in_progress")

    def test_task_loop_tracks_budget_and_checkpoint(self) -> None:
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

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="Hecho.\nVerificado: pending\nSiguiente paso: correr pytest -q",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy assisted")
                runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")

                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["step_budget"], 2)
                self.assertEqual(state["steps_taken"], 1)
                self.assertEqual(state["verification_status"], "pending")
                self.assertEqual(state["last_checkpoint"]["pending_action"], "correr pytest -q")

                task_loop = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_loop"))
                self.assertEqual(task_loop["steps_taken"], 1)
                self.assertEqual(task_loop["verification_status"], "pending")

                queue = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_queue"))
                self.assertEqual(queue[0]["summary"], "correr pytest -q")
                self.assertEqual(queue[0]["priority"], 1)

    def test_task_queue_can_be_filtered_by_mode(self) -> None:
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
                runtime.memory.update_session_state(
                    "s1",
                    task_queue=[
                        {"task_id": "coding:assistant:correr-pytest-q", "summary": "correr pytest -q", "mode": "coding", "status": "pending", "source": "assistant", "priority": 1},
                        {"task_id": "research:assistant:revisar-fuentes", "summary": "revisar fuentes", "mode": "research", "status": "pending", "source": "assistant", "priority": 1},
                    ],
                )

                queue = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_queue coding"))
                self.assertEqual(len(queue), 1)
                self.assertEqual(queue[0]["mode"], "coding")

    def test_proceed_uses_task_queue_when_pending_action_is_empty(self) -> None:
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
            prompts: list[str] = []

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                prompts.append(prompt_text)
                if len(prompts) == 1:
                    content = "Hecho.\nSiguiente paso: correr pytest -q"
                else:
                    content = "handled"
                return LLMResponse(
                    content=content,
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")
                state = runtime.memory.get_session_state("s1")
                runtime.memory.update_session_state(
                    "s1",
                    pending_action="",
                    task_queue=state["task_queue"],
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="procede")
                self.assertEqual(reply, "handled")
                self.assertIn("Continúa con este siguiente paso de la cola: correr pytest -q", prompts[-1])
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["task_queue"][0]["status"], "in_progress")

    def test_proceed_prefers_task_queue_item_matching_current_mode(self) -> None:
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
            prompts: list[str] = []

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                prompts.append(prompt_text)
                return LLMResponse(
                    content="handled",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.memory.update_session_state(
                    "s1",
                    mode="coding",
                    pending_action="",
                    task_queue=[
                        {"task_id": "research:assistant:revisar-fuentes", "summary": "revisar fuentes", "mode": "research", "status": "pending", "source": "assistant", "priority": 0},
                        {"task_id": "coding:assistant:correr-pytest-q", "summary": "correr pytest -q", "mode": "coding", "status": "pending", "source": "assistant", "priority": 1},
                    ],
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="procede")
                self.assertEqual(reply, "handled")
                self.assertIn("Continúa con este siguiente paso de la cola: correr pytest -q", prompts[-1])

    def test_proceed_stops_when_step_budget_is_exhausted(self) -> None:
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

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="Hecho.\nVerificado: pending\nSiguiente paso: correr pytest -q",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy manual")
                runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="procede")
                self.assertIn("step budget agotado", reply)
                self.assertIn("Resumen actual:", reply)

    def test_task_done_marks_queue_entry_done(self) -> None:
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

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                return LLMResponse(
                    content="Hecho.\nSiguiente paso: correr pytest -q",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")
                task_id = runtime.memory.get_session_state("s1")["task_queue"][0]["task_id"]

                result = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text=f"/task_done {task_id}",
                    )
                )

                self.assertEqual(result[0]["status"], "done")
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["pending_action"], "")

    def test_task_defer_marks_queue_entry_deferred(self) -> None:
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

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                if "corrige el bug del login" in prompt_text:
                    return LLMResponse(
                        content="Hecho.\nSiguiente paso: correr pytest -q",
                        lane=request.lane,
                        provider="anthropic",
                        model=request.model,
                    )
                return LLMResponse(
                    content="Hecho.\nSiguiente paso: revisar coverage",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")
                runtime.memory.update_session_state(
                    "s1",
                    task_queue=[
                        {
                            "task_id": "coding:assistant:correr-pytest-q",
                            "summary": "correr pytest -q",
                            "mode": "coding",
                            "status": "pending",
                            "source": "assistant",
                            "priority": 1,
                        },
                        {
                            "task_id": "coding:assistant:revisar-coverage",
                            "summary": "revisar coverage",
                            "mode": "coding",
                            "status": "pending",
                            "source": "assistant",
                            "priority": 2,
                        },
                    ],
                    pending_action="correr pytest -q",
                )

                result = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/task_defer coding:assistant:correr-pytest-q",
                    )
                )

                self.assertEqual(result[0]["status"], "deferred")
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["pending_action"], "revisar coverage")

    def test_proceed_skips_pending_tasks_with_unmet_dependencies(self) -> None:
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
            prompts: list[str] = []

            def scripted_executor(request: LLMRequest) -> LLMResponse:
                prompt_text = request.prompt if isinstance(request.prompt, str) else json.dumps(request.prompt)
                prompts.append(prompt_text)
                return LLMResponse(
                    content="handled",
                    lane=request.lane,
                    provider="anthropic",
                    model=request.model,
                )

            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=scripted_executor)
                runtime.memory.update_session_state(
                    "s1",
                    mode="coding",
                    pending_action="",
                    task_queue=[
                        {
                            "task_id": "coding:assistant:run-tests",
                            "summary": "run tests",
                            "mode": "coding",
                            "status": "pending",
                            "source": "assistant",
                            "priority": 0,
                            "depends_on": ["coding:assistant:fix-bug"],
                        },
                        {
                            "task_id": "coding:assistant:fix-bug",
                            "summary": "fix bug",
                            "mode": "coding",
                            "status": "pending",
                            "source": "assistant",
                            "priority": 1,
                            "depends_on": [],
                        },
                    ],
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="procede")
                self.assertEqual(reply, "handled")
                self.assertIn("Continúa con este siguiente paso de la cola: fix bug", prompts[-1])

    def test_task_done_unblocks_dependent_pending_task(self) -> None:
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
                runtime.memory.update_session_state(
                    "s1",
                    mode="coding",
                    pending_action="fix bug",
                    task_queue=[
                        {
                            "task_id": "coding:assistant:fix-bug",
                            "summary": "fix bug",
                            "mode": "coding",
                            "status": "in_progress",
                            "source": "assistant",
                            "priority": 0,
                            "depends_on": [],
                        },
                        {
                            "task_id": "coding:assistant:run-tests",
                            "summary": "run tests",
                            "mode": "coding",
                            "status": "pending",
                            "source": "assistant",
                            "priority": 1,
                            "depends_on": ["coding:assistant:fix-bug"],
                        },
                    ],
                )

                result = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/task_done coding:assistant:fix-bug",
                    )
                )

                self.assertEqual(result[0]["status"], "done")
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["pending_action"], "run tests")

    def test_task_run_uses_coordinator_and_updates_checkpoint(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:123",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="implement_change", content="edited files", duration_seconds=0.2)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="1. Update the failing path",
                )

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run corrige el bug del login",
                )

                self.assertIn("todavía necesita un paso más", reply)
                runtime.bot.coordinator.run.assert_called_once()
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["verification_status"], "pending")
                self.assertEqual(state["last_checkpoint"]["summary"], "1. Update the failing path")
                self.assertEqual(state["task_queue"][0]["priority"], 0)

    def test_autonomous_coding_message_uses_coordinator(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:autonomous",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: pending\nSiguiente paso: correr pytest -q", duration_seconds=0.1)],
                    },
                    synthesis="1. Inspect the login path",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="corrige el bug del login",
                )

                self.assertIn("Tarea autónoma iniciada", reply)
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))
                runtime.bot.coordinator.run.assert_called_once()
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["pending_action"], "correr pytest -q")
                self.assertEqual(state["verification_status"], "pending")
                self.assertEqual(state["task_queue"][0]["priority"], 0)
                record = runtime.task_ledger.get(task_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.session_id, "s1")
                self.assertEqual(record.objective, "corrige el bug del login")
                self.assertEqual(record.status, "running")
                self.assertEqual(record.verification_status, "pending")
                generic_job_id = record.metadata["generic_job_id"]
                generic_job = runtime.job_service.get(generic_job_id)
                self.assertIsNotNone(generic_job)
                self.assertEqual(generic_job.kind, "coordinator.autonomous_task")
                self.assertEqual(generic_job.status, "retrying")
                self.assertEqual(generic_job.checkpoint["verification_status"], "pending")
                lifecycle = record.artifacts["lifecycle"]
                self.assertEqual(lifecycle["plan"]["objective"], "corrige el bug del login")
                self.assertEqual(lifecycle["plan"]["planned_phases"], ["research", "synthesis", "implementation", "verification"])
                self.assertEqual(lifecycle["verification"]["status"], "pending")
                self.assertNotIn("outcome", lifecycle)
                self.assertEqual(lifecycle["job"]["lifecycle_status"], "pending")
                tasks_payload = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/tasks"))
                self.assertEqual(tasks_payload["summary"], {"running": 1})
                self.assertEqual(tasks_payload["tasks"][0]["task_id"], task_id)
                jobs_payload = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/jobs"))
                self.assertEqual(jobs_payload["system_summary"], {"retrying": 1})
                self.assertEqual(jobs_payload["system_jobs"][0]["job_id"], generic_job_id)
                job_trace = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/job_trace {task_id}"))
                self.assertEqual(job_trace["job_id"], task_id)
                self.assertTrue(
                    any(event["event_type"] == "task_ledger_checkpoint" for event in job_trace["events"])
                )
                self.assertTrue(all(event["artifact_id"] for event in job_trace["events"] if event["event_type"].startswith("task_ledger_")))

                runtime.bot.coordinator.run.reset_mock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id=task_id,
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="apply_patch", content="patched files: login.py", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="verified after resume",
                )
                self.assertEqual(runtime.bot.resume_interrupted_tasks(), 1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))
                resumed_record = runtime.task_ledger.get(task_id)
                self.assertEqual(resumed_record.status, "succeeded")
                self.assertEqual(resumed_record.verification_status, "passed")
                resumed_job = runtime.job_service.get(generic_job_id)
                self.assertEqual(resumed_job.status, "completed")
                self.assertEqual(resumed_job.attempts, 2)

    def test_autonomous_task_waiting_for_user_input_closes_blocked(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="task-1",
                    phase_results={
                        "research": [WorkerResult(task_name="gather_findings", content="no evidence", duration_seconds=0.1)],
                        "verification": [
                            WorkerResult(
                                task_name="verify_findings",
                                content=(
                                    "Verification Status: pending\n"
                                    "Siguiente paso: solicitar al usuario el enlace o contenido verificable"
                                ),
                                duration_seconds=0.1,
                            )
                        ],
                    },
                    synthesis="No hay enlace ni contenido para revisar.",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="analiza el ultimo cuaderno",
                )

                self.assertIn("Tarea autónoma iniciada", reply)
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))
                record = runtime.task_ledger.get(task_id)
                self.assertEqual(record.status, "failed")
                self.assertEqual(record.verification_status, "blocked")
                self.assertIn("waiting_for_user_input", record.error)
                generic_job = runtime.job_service.get(record.metadata["generic_job_id"])
                self.assertEqual(generic_job.status, "failed")
                self.assertIn("waiting_for_user_input", generic_job.error)
                tasks_payload = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/tasks"))
                self.assertEqual(tasks_payload["summary"], {"failed": 1})

    def test_task_completion_question_reports_status_without_starting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
                "CLAW_DISABLE_TASK_INTENT_ROUTER": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.task_ledger.create(
                    task_id="s1:running-task",
                    session_id="s1",
                    objective="revisar notebook",
                    mode="research",
                    runtime="coordinator",
                    status="running",
                    metadata={"autonomous": True},
                )
                runtime.bot.coordinator = MagicMock()
                runtime.memory.update_session_state("s1", autonomy_mode="autonomous")

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Completaste la tarea ?",
                )

                self.assertIn("s1:running-task", reply)
                self.assertIn("running", reply.lower())
                runtime.bot.coordinator.run.assert_not_called()
                self.assertEqual(runtime.task_ledger.summary(session_id="s1"), {"running": 1})

    def test_operational_alert_message_does_not_start_autonomous_task(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.memory.update_session_state("s1", autonomy_mode="autonomous")

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=(
                        "Alerta operacional: Auto-research provider failure\n"
                        "Severidad: critical\n"
                        "Agent: perf-optimizer\n"
                        "Reason: codex_timeout\n"
                        "Failures: 1\n"
                        "Error: Codex CLI timed out after 300.0s"
                    ),
                )

                self.assertIn("Alerta operacional registrada", reply)
                self.assertIn("no la voy a convertir en tarea autónoma", reply)
                runtime.bot.coordinator.run.assert_not_called()
                self.assertEqual(runtime.task_ledger.summary(session_id="s1"), {})
                self.assertTrue(
                    any(
                        event["event_type"] == "operational_alert_input_handled"
                        for event in runtime.observe.recent_events(limit=5)
                    )
                )

    def test_coding_task_autostashes_dirty_worktree(self) -> None:
        import subprocess as _sub
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            _sub.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
            _sub.run(["git", "-C", str(workspace), "config", "user.email", "t@t"], check=True)
            _sub.run(["git", "-C", str(workspace), "config", "user.name", "t"], check=True)
            (workspace / "README.md").write_text("hello\n")
            _sub.run(["git", "-C", str(workspace), "add", "."], check=True)
            _sub.run(["git", "-C", str(workspace), "commit", "-q", "-m", "init"], check=True)
            (workspace / "README.md").write_text("hello dirty\n")
            (workspace / "untracked.txt").write_text("noise\n")
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(workspace),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:autonomous",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="implement_change", content="## Edits\n- foo: x", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="1. Implement",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))

                stash_list = _sub.run(
                    ["git", "-C", str(workspace), "stash", "list"],
                    capture_output=True, text=True, check=True,
                )
                self.assertIn(f"claw:autostash:{task_id}", stash_list.stdout)
                clean = _sub.run(
                    ["git", "-C", str(workspace), "status", "--porcelain"],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(clean.stdout.strip(), "")

    def test_resumed_coding_task_keeps_dirty_worktree_context(self) -> None:
        import subprocess as _sub
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            _sub.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
            _sub.run(["git", "-C", str(workspace), "config", "user.email", "t@t"], check=True)
            _sub.run(["git", "-C", str(workspace), "config", "user.name", "t"], check=True)
            (workspace / "README.md").write_text("hello\n")
            _sub.run(["git", "-C", str(workspace), "add", "."], check=True)
            _sub.run(["git", "-C", str(workspace), "commit", "-q", "-m", "init"], check=True)
            (workspace / "README.md").write_text("partial task work\n")
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(workspace),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
                "TELEGRAM_ALLOWED_USER_ID": "123",
            }
            with patch.dict(os.environ, env, clear=False):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.task_ledger.create(
                    task_id="s1:lost-task",
                    session_id="s1",
                    objective="continua el cambio parcial",
                    mode="coding",
                    runtime="coordinator",
                    status="lost",
                    metadata={"autonomous": True},
                )
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:lost-task",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="kept dirty context",
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_resume s1:lost-task")
                self.assertIn("Tarea reanudada", reply)
                self.assertTrue(runtime.bot._task_handler.wait_for_task("s1:lost-task", timeout=2))

                stash_list = _sub.run(
                    ["git", "-C", str(workspace), "stash", "list"],
                    capture_output=True, text=True, check=True,
                )
                self.assertNotIn("claw:autostash:s1:lost-task", stash_list.stdout)
                self.assertEqual((workspace / "README.md").read_text(), "partial task work\n")

    def test_implementation_worker_error_marks_task_failed(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:autonomous",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(
                            task_name="implement_change",
                            content="",
                            duration_seconds=0.1,
                            error="Codex CLI timed out after 120.0s",
                        )],
                        "verification": [WorkerResult(
                            task_name="verify_change",
                            content="No hay evidencia de implementación.",
                            duration_seconds=0.1,
                        )],
                    },
                    synthesis="1. Inspect the login path",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="corrige el bug del login",
                )

                self.assertIn("Tarea autónoma iniciada", reply)
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))

                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["verification_status"], "failed")

                record = runtime.task_ledger.get(task_id)
                self.assertIsNotNone(record)
                self.assertEqual(record.status, "failed")
                self.assertEqual(record.verification_status, "failed")
                self.assertIn("Codex CLI timed out", record.error)

                lifecycle = record.artifacts["lifecycle"]
                self.assertEqual(lifecycle["outcome"]["status"], "failed")
                self.assertEqual(lifecycle["job"]["lifecycle_status"], "failed")

    def test_task_resume_command_restarts_lost_autonomous_task(self) -> None:
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
                runtime.task_ledger.create(
                    task_id="s1:lost-task",
                    session_id="s1",
                    objective="corrige el bug del login",
                    mode="coding",
                    runtime="coordinator",
                    status="lost",
                    metadata={"autonomous": True},
                )
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:lost-task",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="apply_patch", content="patched files: login.py", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="resumed and verified",
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_resume s1:lost-task")
                self.assertIn("Tarea reanudada", reply)
                self.assertTrue(runtime.bot._task_handler.wait_for_task("s1:lost-task", timeout=2))

                runtime.bot.coordinator.run.assert_called_once()
                record = runtime.task_ledger.get("s1:lost-task")
                self.assertEqual(record.status, "succeeded")
                self.assertEqual(record.metadata["resume_reason"], "manual_resume")
                self.assertEqual(record.metadata["resume_count"], 1)
                generic_job = runtime.job_service.get(record.metadata["generic_job_id"])
                self.assertEqual(generic_job.status, "completed")
                self.assertEqual(generic_job.metadata["reason"], "manual_resume")
                lifecycle = record.artifacts["lifecycle"]
                self.assertEqual(lifecycle["plan"]["objective"], "corrige el bug del login")
                self.assertEqual(lifecycle["execution"]["status"], "resumed")
                self.assertEqual(lifecycle["outcome"]["status"], "succeeded")

    def test_task_resume_reopens_false_success_autonomous_task(self) -> None:
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
                old_job = runtime.job_service.enqueue(
                    kind="coordinator.autonomous_task",
                    payload={
                        "task_id": "s1:false-success",
                        "session_id": "s1",
                        "objective": "termina el hero del prototipo",
                        "mode": "coding",
                    },
                    resume_key="coordinator:s1:false-success",
                    metadata={"reason": "initial_run"},
                )
                runtime.job_service.complete(old_job.job_id, result={"verification_status": "pending"})
                runtime.task_ledger.create(
                    task_id="s1:false-success",
                    session_id="s1",
                    objective="termina el hero del prototipo",
                    mode="coding",
                    runtime="coordinator",
                    status="running",
                    metadata={"autonomous": True, "generic_job_id": old_job.job_id},
                )
                # Sprint 1: writing succeeded+pending must be redirected to a running checkpoint
                # by the completion validator (false_success_prevented).
                runtime.task_ledger.mark_terminal(
                    "s1:false-success",
                    status="succeeded",
                    summary="falta evidencia de screenshots",
                    verification_status="pending",
                    artifacts={"response_preview": "Verification Status: pending"},
                )
                redirected = runtime.task_ledger.get("s1:false-success")
                self.assertEqual(redirected.status, "running")
                self.assertNotEqual(redirected.verification_status, "passed")

                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:false-success",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="apply_patch", content="patched files: hero.html", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="resumed and verified",
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_resume s1:false-success")
                self.assertIn("Tarea reanudada", reply)
                self.assertTrue(runtime.bot._task_handler.wait_for_task("s1:false-success", timeout=2))

                runtime.bot.coordinator.run.assert_called_once()
                record = runtime.task_ledger.get("s1:false-success")
                self.assertEqual(record.status, "succeeded")
                self.assertEqual(record.verification_status, "passed")
                self.assertEqual(record.metadata["resume_reason"], "manual_resume")
                self.assertNotEqual(record.metadata["generic_job_id"], old_job.job_id)
                resumed_job = runtime.job_service.get(record.metadata["generic_job_id"])
                self.assertEqual(resumed_job.status, "completed")
                self.assertEqual(resumed_job.resume_key, "coordinator:s1:false-success")
                self.assertEqual(runtime.job_service.get(old_job.job_id).status, "completed")

    def test_task_resume_keeps_verified_success_terminal(self) -> None:
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
                runtime.task_ledger.create(
                    task_id="s1:verified-success",
                    session_id="s1",
                    objective="corrige el bug del login",
                    mode="coding",
                    runtime="coordinator",
                    status="running",
                    metadata={"autonomous": True},
                )
                runtime.task_ledger.mark_terminal(
                    "s1:verified-success",
                    status="succeeded",
                    summary="verificado",
                    verification_status="passed",
                    artifacts={"test_output": "5 passed"},
                )
                runtime.bot.coordinator = MagicMock()

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_resume s1:verified-success")

                self.assertIn("already succeeded", reply)
                runtime.bot.coordinator.run.assert_not_called()

    def test_task_cancel_command_marks_running_task_cancelled(self) -> None:
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
                runtime.task_ledger.create(
                    task_id="s1:running-task",
                    session_id="s1",
                    objective="corrige el bug del login",
                    mode="coding",
                    runtime="coordinator",
                    status="running",
                    metadata={"autonomous": True},
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/task_cancel s1:running-task")
                self.assertIn("Tarea cancelada", reply)

                record = runtime.task_ledger.get("s1:running-task")
                self.assertEqual(record.status, "cancelled")
                self.assertEqual(record.verification_status, "cancelled")
                self.assertEqual(record.artifacts["lifecycle"]["plan"]["objective"], "corrige el bug del login")
                self.assertEqual(record.artifacts["lifecycle"]["outcome"]["status"], "cancelled")
                status = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/job_status s1:running-task"))
                self.assertEqual(status["status"], "cancelled")
                jobs = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/jobs"))
                self.assertEqual(jobs["summary"], {"cancelled": 1})
                self.assertEqual(jobs["jobs"][0]["task_id"], "s1:running-task")

    def test_job_cancel_on_generic_coordinator_job_cancels_linked_task(self) -> None:
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
                runtime.task_ledger.create(
                    task_id="s1:running-task",
                    session_id="s1",
                    objective="corrige el bug del login",
                    mode="coding",
                    runtime="coordinator",
                    status="running",
                    metadata={"autonomous": True},
                )
                job = runtime.job_service.enqueue(
                    kind="coordinator.autonomous_task",
                    payload={
                        "task_id": "s1:running-task",
                        "session_id": "s1",
                        "objective": "corrige el bug del login",
                        "mode": "coding",
                    },
                    resume_key="coordinator:s1:running-task",
                )

                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/job_cancel {job.job_id}")

                self.assertIn("Tarea cancelada", reply)
                self.assertEqual(runtime.task_ledger.get("s1:running-task").status, "cancelled")
                self.assertEqual(runtime.job_service.get(job.job_id).status, "cancelled")

    def test_jobs_command_includes_generic_job_service_records(self) -> None:
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
                job = runtime.job_service.enqueue(
                    kind="notebooklm.research",
                    payload={"notebook_id": "nb1"},
                    resume_key="nlm:nb1",
                )

                jobs = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/jobs"))
                self.assertEqual(jobs["system_summary"], {"queued": 1})
                self.assertEqual(jobs["system_jobs"][0]["job_id"], job.job_id)

                status = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/job_status {job.job_id}"))
                self.assertEqual(status["source"], "job_service")
                self.assertEqual(status["kind"], "notebooklm.research")

                cancel = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/job_cancel {job.job_id}")
                self.assertIn("Job cancelado", cancel)
                self.assertEqual(runtime.job_service.get(job.job_id).status, "cancelled")

    def test_autonomous_policy_blocks_sensitive_automatic_task(self) -> None:
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
                runtime.bot.coordinator = MagicMock()

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="haz git push y despliega a prod",
                )

                self.assertIn("autonomy policy blocked coordinated execution", reply)
                runtime.bot.coordinator.run.assert_not_called()
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["verification_status"], "blocked")

    def test_autonomous_policy_allows_requested_git_push_without_phase_approval(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:push",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="implement_change", content="pushed branch", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="git push completed",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="verifica y haz git push de la rama",
                )

                self.assertIn("Tarea autónoma iniciada", reply)
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))
                runtime.bot.coordinator.run.assert_called_once()
                self.assertEqual(runtime.approvals.list_pending(), [])

    def test_task_run_blocks_sensitive_scope_even_when_explicit(self) -> None:
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
                runtime.bot.coordinator = MagicMock()

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run deploy to prod and git push",
                )

                self.assertIn("autonomy policy blocked coordinated execution", reply)
                runtime.bot.coordinator.run.assert_not_called()

    def test_task_run_blocks_commit_until_approved(self) -> None:
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
                runtime.bot.coordinator = MagicMock()

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run commit los cambios del bug del login",
                )

                self.assertIn("autonomy policy requires approval", reply)
                self.assertIn("/task_approve", reply)
                self.assertIn("/task_abort", reply)
                runtime.bot.coordinator.run.assert_not_called()
                pending = runtime.approvals.list_pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["metadata"]["kind"], "coordinated_task")
                self.assertEqual(pending[0]["metadata"]["session_id"], "s1")
                self.assertEqual(pending[0]["metadata"]["approved_actions"], ["commit"])
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["verification_status"], "awaiting_approval")

    def test_task_run_allows_commit_when_session_is_autonomous(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:commit",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="implement_change", content="created commit", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="commit completed",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run commit los cambios del bug del login",
                )

                self.assertIn("todavía necesita un paso más", reply)
                runtime.bot.coordinator.run.assert_called_once()
                self.assertEqual(runtime.approvals.list_pending(), [])

    def test_task_approve_runs_coordinator_after_commit_approval(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:approved",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="scope ok", duration_seconds=0.1)],
                        "implementation": [WorkerResult(task_name="implement_change", content="created commit", duration_seconds=0.2)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="1. Commit created and verified",
                )

                first = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run commit los cambios del bug del login",
                )
                match = re.search(r"/task_approve ([^ ]+) ([^`\n ]+)", first)
                self.assertIsNotNone(match)
                approval_id, token = match.group(1), match.group(2)

                second = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=f"/task_approve {approval_id} {token}",
                )

                self.assertIn("todavía necesita un paso más", second)
                runtime.bot.coordinator.run.assert_called_once()
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["verification_status"], "pending")

    def test_proceed_during_task_approval_wait_returns_explicit_instruction(self) -> None:
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
                runtime.bot.coordinator = MagicMock()

                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run commit los cambios del bug del login",
                )

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="procede",
                )

                self.assertIn("Hay una aprobación pendiente", reply)
                self.assertIn("/task_pending", reply)
                self.assertIn("/task_abort", reply)
                runtime.bot.coordinator.run.assert_not_called()

    def test_task_pending_lists_session_approval_queue(self) -> None:
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
                runtime.bot.coordinator = MagicMock()

                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run commit los cambios del bug del login",
                )

                pending = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/task_pending",
                    )
                )

                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["action"], "coordinated_task")
                self.assertIn("/task_approve", pending[0]["approve_command"])
                self.assertIn("/task_abort", pending[0]["abort_command"])

    def test_task_abort_rejects_pending_coordinated_task(self) -> None:
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
                runtime.bot.coordinator = MagicMock()

                first = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/task_run commit los cambios del bug del login",
                )
                match = re.search(r"/task_abort ([^`\n ]+)", first)
                self.assertIsNotNone(match)
                approval_id = match.group(1)

                second = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=f"/task_abort {approval_id}",
                )

                self.assertEqual(second, "coordinated task rejected")
                runtime.bot.coordinator.run.assert_not_called()
                self.assertEqual(runtime.bot.approvals.status(approval_id), "rejected")
                state = runtime.memory.get_session_state("s1")
                self.assertEqual(state["verification_status"], "blocked")
                self.assertEqual(state["pending_approvals"], [])

    @patch("claw_v2.browse_handler._jina_read")
    def test_browse_failure_records_learning_outcome(self, mock_jina) -> None:
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
                mock_jina.return_value = ""
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = None
                runtime.bot.managed_chrome = None

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/browse https://example.com/fail",
                )

                self.assertIn("browse error", reply)
                outcomes = runtime.memory.search_past_outcomes("example.com/fail", task_type="browse")
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["outcome"], "failure")
                self.assertIn("backend path", outcomes[0]["lesson"])

    def test_terminal_commands_delegate_to_bridge(self) -> None:
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
                bridge = MagicMock()
                bridge.list_sessions.return_value = [{"session_id": "sess-1", "status": "running"}]
                bridge.open.return_value = {"session_id": "sess-1", "tool": "codex"}
                bridge.status.return_value = {"session_id": "sess-1", "status": "running"}
                bridge.read.return_value = {"session_id": "sess-1", "offset": 12, "next_offset": 16, "output": "pong"}
                bridge.send.return_value = {"session_id": "sess-1", "bytes_written": 12}
                bridge.close.return_value = {"session_id": "sess-1", "status": "closing"}
                runtime.bot.terminal_bridge = bridge

                sessions = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_list"))
                self.assertEqual(sessions["sessions"][0]["session_id"], "sess-1")

                opened = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/terminal_open codex /tmp/work dir",
                    )
                )
                self.assertEqual(opened["tool"], "codex")
                bridge.open.assert_called_once_with("codex", cwd="/tmp/work dir")

                status = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_status sess-1")
                )
                self.assertEqual(status["status"], "running")
                bridge.status.assert_called_once_with("sess-1")

                read = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read sess-1 12")
                )
                self.assertEqual(read["output"], "pong")
                bridge.read.assert_called_once_with("sess-1", offset=12, limit=3000)

                sent = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/terminal_send sess-1 hola codex",
                    )
                )
                self.assertEqual(sent["bytes_written"], 12)
                bridge.send.assert_called_once_with("sess-1", "hola codex")

                closed = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_close sess-1")
                )
                self.assertEqual(closed["status"], "closing")
                bridge.close.assert_called_once_with("sess-1")

    def test_terminal_command_usage_and_validation(self) -> None:
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
                runtime.bot.terminal_bridge = MagicMock()

                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_open"),
                    "usage: /terminal_open <claude|codex> [cwd]",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_status"),
                    "usage: /terminal_status <session_id>",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read"),
                    "usage: /terminal_read <session_id> [offset]",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_send"),
                    "usage: /terminal_send <session_id> <text>",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_close"),
                    "usage: /terminal_close <session_id>",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/terminal_read sess-1 -1"),
                    "offset must be greater than or equal to 0",
                )

    def test_agent_create_command(self) -> None:
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
                created = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_create researcher-2 researcher Investigate regressions carefully.",
                    )
                )
                self.assertEqual(created["name"], "researcher-2")
                self.assertEqual(created["agent_class"], "researcher")
                self.assertIn("WebSearch", created["allowed_tools"])
                self.assertNotIn("Write", created["allowed_tools"])

    def test_approvals_commands(self) -> None:
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
                pending = runtime.approvals.create("deploy_prod", "high risk deploy")
                approvals_payload = runtime.bot.handle_text(user_id="123", session_id="s1", text="/approvals")
                parsed = json.loads(approvals_payload)
                self.assertEqual(parsed[0]["approval_id"], pending.approval_id)
                status = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=f"/approval_status {pending.approval_id}",
                )
                self.assertEqual(status, "pending")

    def test_natural_language_tool_approval_retries_original_request(self) -> None:
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
                pending = runtime.approvals.create("tool:GPTImage", "GPTImage(prompt)")
                mock_handle_message = MagicMock(
                    side_effect=[
                        ApprovalPending(
                            approval_id=pending.approval_id,
                            token=pending.token,
                            tool="GPTImage",
                            summary="GPTImage(prompt)",
                        ),
                        LLMResponse(
                            content="imagen creada",
                            lane="brain",
                            provider="openai",
                            model="gpt-5.4-mini",
                        ),
                    ]
                )

                with patch.object(type(runtime.brain), "handle_message", mock_handle_message):
                    first = runtime.bot.handle_text(
                        user_id="123",
                        session_id="tg-123",
                        text="Ejecuta la herramienta protegida de prueba",
                    )
                    self.assertIn("/approve", first)
                    state = runtime.brain.memory.get_session_state("tg-123")
                    self.assertEqual(
                        state["active_object"]["pending_tool_approval"]["approval_id"],
                        pending.approval_id,
                    )
                    messages = runtime.brain.memory.get_recent_messages("tg-123", limit=2)
                    self.assertEqual(messages[0]["content"], "Ejecuta la herramienta protegida de prueba")
                    self.assertNotIn(pending.token, messages[1]["content"])

                    second = runtime.bot.handle_text(user_id="123", session_id="tg-123", text="Aprobada")

                self.assertEqual(runtime.approvals.status(pending.approval_id), "approved")
                self.assertIn("Reintenté la acción original", second)
                self.assertIn("imagen creada", second)
                retried_message = mock_handle_message.call_args_list[1].args[1]
                self.assertIn("Ejecuta la herramienta protegida de prueba", retried_message)
                self.assertEqual(
                    mock_handle_message.call_args_list[1].kwargs["memory_text"],
                    "Ejecuta la herramienta protegida de prueba",
                )

    def test_agent_control_commands(self) -> None:
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
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-1",
                        agent_class="operator",
                        instruction="Ship carefully.",
                    )
                )

                listing = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/agents"))
                self.assertEqual(listing[0]["name"], "operator-1")
                self.assertFalse(listing[0]["commit_on_promotion"])

                promote = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_promote operator-1 on")
                )
                self.assertTrue(promote["promote_on_improvement"])

                branch = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_branch operator-1 on")
                )
                self.assertTrue(branch["branch_on_promotion"])

                commit = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit operator-1 on")
                )
                self.assertTrue(commit["commit_on_promotion"])

                message = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_commit_message operator-1 chore(claw): publish operator-1",
                    )
                )
                self.assertEqual(message["promotion_commit_message"], "chore(claw): publish operator-1")

                branch_name = json.loads(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_branch_name operator-1 claw/operator-1/review",
                    )
                )
                self.assertEqual(branch_name["promotion_branch_name"], "claw/operator-1/review")

                detail = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_status operator-1")
                )
                self.assertEqual(detail["instruction"], "Ship carefully.")
                self.assertTrue(detail["commit_on_promotion"])
                self.assertTrue(detail["branch_on_promotion"])

                cleared = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit_message operator-1 clear")
                )
                self.assertIsNone(cleared["promotion_commit_message"])
                cleared_branch = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_branch_name operator-1 clear")
                )
                self.assertIsNone(cleared_branch["promotion_branch_name"])
                duplicate = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/agent_create operator-1 operator Duplicate attempt",
                )
                self.assertEqual(duplicate, "agent already exists: operator-1")

    def test_agent_control_errors_are_user_facing(self) -> None:
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
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_status missing"),
                    "agent not found: missing",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_create ../bad operator Nope"),
                    "agent_name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_create bad-judge judge Nope"),
                    "agent_class must be one of: researcher, operator, deployer",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_branch_name missing claw/foo"),
                    "agent not found: missing",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit missing on"),
                    "agent not found: missing",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_commit foo maybe"),
                    "toggle must be one of: on, off",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run foo 0"),
                    "max_experiments must be greater than 0",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_publish foo 0"),
                    "max_experiments must be greater than 0",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run_until foo nope 3"),
                    "target_metric must be a number",
                )
                self.assertEqual(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_history foo nope"),
                    "limit must be an integer",
                )
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-err",
                        agent_class="operator",
                        instruction="Check errors.",
                    )
                )
                self.assertEqual(
                    runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/agent_branch_name operator-err bad..branch",
                    ),
                    "promotion_branch_name is not a valid git branch name",
                )

    def test_agent_run_commands_execute_and_resume_agents(self) -> None:
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
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-2",
                        agent_class="operator",
                        instruction="Improve carefully.",
                    ),
                    state={"paused": True, "last_verified_state": {"metric": 0.1}},
                )
                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [ExperimentRecord(1, 0.2, 0.1, "improved")]
                )

                run_payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run operator-2 1")
                )
                self.assertEqual(run_payload["run_reason"], "completed")
                self.assertEqual(run_payload["experiments_run"], 1)
                self.assertEqual(run_payload["run_last_metric"], 0.2)
                self.assertFalse(run_payload["paused"])

                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [
                        ExperimentRecord(1, 0.25, 0.2, "improved"),
                        ExperimentRecord(2, 0.35, 0.25, "improved"),
                    ]
                )
                run_until_payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_run_until operator-2 0.3 3")
                )
                self.assertEqual(run_until_payload["run_reason"], "target_reached")
                self.assertEqual(run_until_payload["experiments_run"], 2)
                self.assertEqual(run_until_payload["run_last_metric"], 0.35)

    def test_agent_pause_resume_and_history_commands(self) -> None:
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
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-4",
                        agent_class="operator",
                        instruction="Inspect history.",
                    )
                )
                runtime.auto_research.store.append_result("operator-4", ExperimentRecord(1, 0.2, 0.1, "improved", 0.01))
                runtime.auto_research.store.append_result(
                    "operator-4",
                    ExperimentRecord(2, 0.25, 0.2, "improved", 0.02, "abc1234", "claw/operator-4/abc1234"),
                )

                paused = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_pause operator-4")
                )
                self.assertTrue(paused["paused"])

                resumed = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_resume operator-4")
                )
                self.assertFalse(resumed["paused"])

                history = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_history operator-4 1")
                )
                self.assertEqual(history["history_limit"], 1)
                self.assertEqual(history["history_count"], 1)
                self.assertEqual(history["history"][0]["experiment_number"], 2)
                self.assertEqual(history["history"][0]["promotion_commit_sha"], "abc1234")
                self.assertEqual(history["history"][0]["promotion_branch_name"], "claw/operator-4/abc1234")

    def test_agent_publish_command_returns_publication_metadata(self) -> None:
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
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-publish",
                        agent_class="operator",
                        instruction="Publish carefully.",
                    ),
                    state={"last_verified_state": {"metric": 0.1}},
                )
                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [
                        ExperimentRecord(
                            1,
                            0.2,
                            0.1,
                            "executed",
                            0.03,
                            "deadbeef",
                            "claw/operator-publish/deadbee",
                        )
                    ]
                )

                payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_publish operator-publish 1")
                )
                self.assertEqual(payload["run_reason"], "completed")
                self.assertTrue(payload["publish_mode_updated"])
                self.assertTrue(payload["published"])
                self.assertEqual(payload["published_commit_sha"], "deadbeef")
                self.assertEqual(payload["published_branch_name"], "claw/operator-publish/deadbee")

    def test_agent_pr_command_returns_pull_request_metadata(self) -> None:
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
                runtime.auto_research.create_agent(
                    definition=AgentDefinition(
                        name="operator-pr",
                        agent_class="operator",
                        instruction="Open a pull request.",
                    ),
                    state={"last_verified_state": {"metric": 0.1}},
                )
                runtime.auto_research.experiment_runner = scripted_experiment_runner(
                    [
                        ExperimentRecord(
                            1,
                            0.2,
                            0.1,
                            "executed",
                            0.03,
                            "beadfeed",
                            "claw/operator-pr/beadfee",
                        )
                    ]
                )

                class FakePullRequests:
                    def create_pull_request(self, **kwargs) -> PullRequestResult:
                        self.kwargs = kwargs
                        return PullRequestResult(
                            url="https://github.com/acme/repo/pull/7",
                            branch_name=kwargs["branch_name"],
                            title=kwargs["title"],
                            number=7,
                            draft=kwargs["draft"],
                        )

                runtime.bot.pull_requests = FakePullRequests()
                payload = json.loads(
                    runtime.bot.handle_text(user_id="123", session_id="s1", text="/agent_pr operator-pr 1")
                )
                self.assertTrue(payload["published"])
                self.assertTrue(payload["pull_request_created"])
                self.assertEqual(payload["pull_request_number"], 7)
                self.assertEqual(payload["pull_request_url"], "https://github.com/acme/repo/pull/7")
                self.assertTrue(payload["pull_request_draft"])


    def test_chrome_pages_command(self) -> None:
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
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.connect_to_chrome.return_value = [
                    {"url": "https://ads.google.com", "title": "Google Ads", "index": 0},
                ]
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_pages")
                parsed = json.loads(result)
                self.assertEqual(parsed["pages"][0]["url"], "https://ads.google.com")

    def test_chrome_browse_command(self) -> None:
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
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://ads.google.com/campaigns",
                    title="Google Ads",
                    content="campaign data...",
                )
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/chrome_browse https://ads.google.com")
                self.assertIn("Google Ads", result)
                self.assertIn("campaign data", result)

    def test_natural_language_google_ads_shortcut_uses_chrome_browse(self) -> None:
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
                from claw_v2.browser import BrowseResult
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://ads.google.com/campaigns",
                    title="Google Ads",
                    content="campaign data...",
                )
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Ya chrome esta abierto en Google Ads revisalo",
                )
                self.assertIn("Google Ads", result)
                runtime.bot.browser.chrome_navigate.assert_called_once_with(
                    "https://ads.google.com",
                    cdp_url="http://localhost:9250",
                    page_url_pattern="ads.google.com",
                )

    def test_natural_language_chatgpt_new_chat_uses_chrome_cdp(self) -> None:
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
                from claw_v2.browser import BrowseResult

                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://chatgpt.com/",
                    title="ChatGPT",
                    content="Message ChatGPT",
                )
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Abre chrome y inicia un nuevo chat en ChatGPT",
                )
                self.assertIn("ChatGPT abierto en Chrome", result)
                runtime.bot.browser.chrome_navigate.assert_called_once_with(
                    "https://chatgpt.com/",
                    cdp_url="http://localhost:9250",
                    page_url_pattern="chatgpt.com",
                )

    def test_natural_language_chatgpt_image_request_uses_browser_automation(self) -> None:
        class StubBrowserUse:
            def __init__(self) -> None:
                self.instruction = ""

            async def run_task(self, instruction: str) -> str:
                self.instruction = instruction
                return "imagen solicitada en ChatGPT"

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
                browser_use = StubBrowserUse()
                runtime.bot.browser_use = browser_use
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Abre chrome y pídele a ChatGPT una imagen que represente mi marca",
                )
                self.assertEqual(result, "imagen solicitada en ChatGPT")
                self.assertIn("https://chatgpt.com/", browser_use.instruction)
                self.assertIn("imagen que represente mi marca", browser_use.instruction)

    def test_brain_prompt_includes_runtime_capability_context(self) -> None:
        captured: dict[str, str] = {}

        def capture_prompt(request: LLMRequest) -> LLMResponse:
            captured["prompt"] = str(request.prompt)
            return LLMResponse(
                content="<response>ok</response>",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

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
                runtime = build_runtime(anthropic_executor=capture_prompt)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser_use = MagicMock()

                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="responde prueba")

                self.assertEqual(result, "ok")
                self.assertIn("# Runtime capability context", captured["prompt"])
                self.assertIn("Chrome CDP: available (http://localhost:9250)", captured["prompt"])
                self.assertIn("Browser automation: available", captured["prompt"])
                self.assertIn("do not say 'no tengo acceso", captured["prompt"])

    def test_blocks_false_capability_denial_when_browser_available(self) -> None:
        def false_denial(request: LLMRequest) -> LLMResponse:
            return LLMResponse(
                content="<response>No puedo hacerlo porque no tengo acceso al navegador. Habilita el browser bridge.</response>",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

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
                runtime = build_runtime(anthropic_executor=false_denial)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser_use = MagicMock()

                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="responde prueba")

                self.assertIn("No voy a asumir falta de acceso", result)
                self.assertIn("Chrome/CDP", result)
                self.assertNotIn("Habilita el browser bridge", result)

    @patch("claw_v2.browse_handler._jina_read")
    def test_natural_language_url_uses_isolated_browse(self, mock_jina) -> None:
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
                mock_jina.return_value = "# Pricing\n\nOpenAI API Pricing — pay per token for GPT models. " + "x" * 200
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content="handled",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )
                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="Revisa https://openai.com/pricing",
                    )
                self.assertIn("## Fuente", result)
                self.assertIn("## Aplicación sugerida", result)
                self.assertIn("handled", result)
                mock_jina.assert_called_once_with("https://openai.com/pricing")
                args, kwargs = mock_handle_message.call_args
                self.assertIn("## Fuente", args[1])
                self.assertIn("## Aplicación sugerida", args[1])
                self.assertIn("[Contenido del enlace pre-cargado]", args[1])
                self.assertEqual(kwargs["memory_text"], "Revisa https://openai.com/pricing")

    @patch("claw_v2.browse_handler._jina_read")
    def test_natural_language_bare_domain_is_normalized_for_browse(self, mock_jina) -> None:
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
                mock_jina.return_value = "# Docs\n\ndocumentation content here " + "x" * 200
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content="handled",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )
                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="revisa example.com/docs",
                    )
                self.assertIn("## Fuente", result)
                self.assertIn("## Aplicación sugerida", result)
                self.assertIn("handled", result)
                mock_jina.assert_called_once_with("https://example.com/docs")
                args, kwargs = mock_handle_message.call_args
                self.assertIn("[URL analizada]: https://example.com/docs", args[1])
                self.assertEqual(kwargs["memory_text"], "revisa example.com/docs")

    @patch("claw_v2.browse_handler._jina_read")
    def test_standalone_url_uses_brain_link_analysis_prompt(self, mock_jina) -> None:
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
                url = "https://example.com/post"
                mock_jina.return_value = "# Post\n\ncontenido del post " + "x" * 200
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content="handled",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )
                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text=url,
                    )
                self.assertIn("## Fuente", result)
                self.assertIn("## Aplicación sugerida", result)
                self.assertIn("handled", result)
                mock_jina.assert_called_once_with(url)
                args, kwargs = mock_handle_message.call_args
                self.assertIn("## Fuente", args[1])
                self.assertIn("## Aplicación sugerida", args[1])
                self.assertIn("[URL analizada]: https://example.com/post", args[1])
                self.assertEqual(kwargs["memory_text"], url)

    @patch("claw_v2.browse_handler._jina_read")
    def test_standalone_url_echo_is_rewritten_into_structured_analysis(self, mock_jina) -> None:
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
                url = "https://example.com/post"
                mock_jina.return_value = "# Post\n\ncontenido del post"
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content=url,
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )
                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text=url,
                    )
                self.assertIn("## Fuente", result)
                self.assertIn("## Aplicación sugerida", result)
                self.assertIn("contenido del post", result)
                self.assertNotEqual(result.strip(), url)

    @patch("claw_v2.browse_handler._tweet_fxtwitter_read")
    def test_natural_language_review_tweet_reuses_recent_tweet_url(self, mock_tweet_read) -> None:
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
                from claw_v2.browser import BrowseResult

                tweet_url = "https://x.com/tendenciatuits/status/2039116558836936982?s=46"
                mock_tweet_read.return_value = f"**Tendencias y Tuits Borrados on X** ({tweet_url})\n\nTexto limpio del tweet."
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url=tweet_url,
                    title="X",
                    content="Don't miss what's happening\nLog in\nSign up\nSee new posts " + "x" * 200,
                )

                first = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=tweet_url,
                )
                second = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Revisa el último tweet",
                )

                self.assertIn("## Fuente", first)
                self.assertIn("## Aplicación sugerida", first)
                self.assertIn("handled", first)
                self.assertEqual(second, "handled")
                self.assertEqual(runtime.bot.browser.chrome_navigate.call_count, 1)

    @patch("claw_v2.browse_handler._tweet_fxtwitter_read")
    def test_ambiguous_x_tweet_request_does_not_reuse_previous_tweet(self, mock_tweet_read) -> None:
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
                from claw_v2.browser import BrowseResult

                tweet_url = "https://x.com/tendenciatuits/status/2039116558836936982?s=46"
                mock_tweet_read.return_value = f"**Tendencias y Tuits Borrados on X** ({tweet_url})\n\nTexto limpio del tweet."
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url=tweet_url,
                    title="X",
                    content="Don't miss what's happening\nLog in\nSign up\nSee new posts " + "x" * 200,
                )

                first = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=tweet_url,
                )
                second = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Revisa el tweet de X",
                )

                self.assertIn("## Fuente", first)
                self.assertIn("## Aplicación sugerida", first)
                self.assertIn("handled", first)
                self.assertEqual(second, "handled")
                self.assertEqual(runtime.bot.browser.chrome_navigate.call_count, 1)

    @patch("claw_v2.bot_helpers._tweet_fxtwitter_read")
    def test_natural_language_review_tweet_url_uses_brain_analysis(self, mock_tweet_read) -> None:
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
                tweet_url = "https://x.com/karpathy/status/2044708010506541998?s=46"
                mock_tweet_read.return_value = f"**Andrej Karpathy (@karpathy) on X** ({tweet_url})\n\nTexto limpio del tweet."
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content="handled",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )

                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text=f"Revisa este tweet {tweet_url}",
                    )

                self.assertEqual(result, "handled")
                runtime.bot.browser.chrome_navigate.assert_not_called()
                mock_tweet_read.assert_called_once_with(tweet_url)
                args, kwargs = mock_handle_message.call_args
                self.assertEqual(args[0], "s1")
                self.assertIn("## Fuente", args[1])
                self.assertIn("## Aplicación sugerida", args[1])
                self.assertEqual(kwargs["memory_text"], f"Revisa este tweet {tweet_url}")

    def test_runtime_capability_question_uses_conservative_prompt(self) -> None:
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
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content="handled",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )

                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="Que aportará al bot ?",
                    )

                self.assertIn("## Implementado hoy", result)
                self.assertIn("## Parcial", result)
                self.assertIn("## Sugerencia", result)
                self.assertIn("handled", result)
                args, kwargs = mock_handle_message.call_args
                self.assertIn("[Instrucción de rigor sobre el sistema]", args[1])
                self.assertIn("## Implementado hoy", args[1])
                self.assertIn("## Parcial", args[1])
                self.assertIn("## Sugerencia", args[1])
                self.assertEqual(kwargs["memory_text"], "Que aportará al bot ?")

    def test_runtime_capability_question_enforces_response_sections(self) -> None:
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
                with patch.object(type(runtime.bot.brain), "handle_message") as mock_handle_message:
                    mock_handle_message.return_value = LLMResponse(
                        content="Fallas críticas\nLa búsqueda semántica está limitada.\nSugiero embeddings reales.",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    )

                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="Que aportará al bot ?",
                    )

                self.assertIn("## Implementado hoy", result)
                self.assertIn("## Parcial", result)
                self.assertIn("## Sugerencia", result)
                self.assertIn("Fallas críticas", result)

    def test_natural_language_review_request_uses_computer_read(self) -> None:
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
                runtime.bot.computer = MagicMock()
                runtime.bot.computer.capture_screenshot.return_value = {
                    "data": "abc123",
                    "media_type": "image/png",
                }
                with patch.object(
                    type(runtime.bot.brain),
                    "handle_message",
                    return_value=LLMResponse(
                        content="Veo Google Ads en la pantalla.",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    ),
                ) as mock_handle_message:
                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="Revisa la pagina actual y dime que ves",
                    )

                self.assertEqual(result, "Veo Google Ads en la pantalla.")
                runtime.bot.computer.capture_screenshot.assert_called_once_with()
                mock_handle_message.assert_called_once()

    def test_natural_language_click_request_uses_computer_action(self) -> None:
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
                runtime.bot.computer = MagicMock()
                runtime.bot.browser_use = None  # prevent real OpenAI calls
                runtime.bot.computer_client_factory = lambda: object()
                runtime.bot.computer_gate = MagicMock()

                def fake_run_agent_loop(*, session, **kwargs):
                    session.status = "awaiting_approval"
                    session.pending_action = {
                        "tool_use_id": "tool-1",
                        "action": "left_click",
                        "coordinate": [500, 300],
                    }
                    return "Action needs approval: left_click — waiting for /action_approve"

                runtime.bot.computer.run_agent_loop.side_effect = fake_run_agent_loop

                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Haz clic en Revisar tu configuración",
                )

                self.assertIn("/action_approve", result)
                runtime.bot.computer.run_agent_loop.assert_called_once()

    @patch("claw_v2.browse_handler._jina_read")
    def test_browse_command_normalizes_bare_domain(self, mock_jina) -> None:
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
                mock_jina.return_value = "# Local App\n\nlocal preview content " + "x" * 200
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/browse localhost:3000",
                )
                self.assertIn("Local App", result)
                mock_jina.assert_called_once_with("https://localhost:3000")

    @patch("claw_v2.browse_handler._jina_read")
    def test_browse_emits_observe_event_for_public_strategy(self, mock_jina) -> None:
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
                mock_jina.return_value = "# Public\n\ncontenido publico " + "x" * 100
                runtime = build_runtime(anthropic_executor=fake_anthropic)

                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/browse https://example.com/public",
                )

                event = runtime.observe.recent_events(1)[0]
                self.assertEqual(event["event_type"], "browse_result")
                self.assertEqual(event["payload"]["strategy"], "public")
                self.assertEqual(event["payload"]["selected_backend"], "jina")
                self.assertEqual(event["payload"]["status"], "success")

    @patch("claw_v2.browse_handler._tweet_fxtwitter_read")
    def test_browse_emits_observe_event_for_authenticated_strategy(self, mock_tweet_read) -> None:
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
                from claw_v2.browser import BrowseResult

                mock_tweet_read.return_value = ""
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.return_value = BrowseResult(
                    url="https://x.com/acme/status/1",
                    title="Tweet",
                    content="tweet content " + "x" * 200,
                )

                runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/browse https://x.com/acme/status/1",
                )

                event = runtime.observe.recent_events(1)[0]
                self.assertEqual(event["event_type"], "browse_result")
                self.assertEqual(event["payload"]["strategy"], "authenticated")
                self.assertEqual(event["payload"]["selected_backend"], "chrome_cdp")
                self.assertEqual(event["payload"]["status"], "success")

    def test_natural_language_terminal_shortcut_opens_claude(self) -> None:
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
                runtime.bot.terminal_bridge = MagicMock()
                runtime.bot.terminal_bridge.open.return_value = {"session_id": "sess-1", "tool": "claude"}
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="Abre Claude code en la terminal",
                )
                parsed = json.loads(result)
                self.assertEqual(parsed["tool"], "claude")
                runtime.bot.terminal_bridge.open.assert_called_once_with("claude", cwd=None)

    def test_chrome_command_returns_actionable_cdp_message_when_port_is_down(self) -> None:
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
                runtime.bot.browser = MagicMock()
                runtime.bot.managed_chrome = MagicMock()
                runtime.bot.managed_chrome.cdp_url = "http://localhost:9250"
                runtime.bot.browser.chrome_navigate.side_effect = RuntimeError("connect ECONNREFUSED 127.0.0.1:9250")
                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/chrome_browse https://ads.google.com",
                )
                self.assertIn("Chrome del bot no responde", result)
                self.assertIn("Reinicia el bot", result)

    def test_screen_command(self) -> None:
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
                runtime.bot.computer = MagicMock()
                runtime.bot.computer.capture_screenshot.return_value = {"data": "abc123base64data", "media_type": "image/png"}
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text="/screen")
                parsed = json.loads(result)
                self.assertIn("screenshot_data", parsed)

    def test_computer_command_uses_screenshot_and_multimodal_brain(self) -> None:
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
                runtime.bot.computer = MagicMock()
                runtime.bot.computer.capture_screenshot.return_value = {
                    "data": "abc123",
                    "media_type": "image/png",
                }
                with patch.object(
                    type(runtime.bot.brain),
                    "handle_message",
                    return_value=LLMResponse(
                        content="Veo una pagina con metricas.",
                        lane="brain",
                        provider="anthropic",
                        model="claude-opus-4-7",
                    ),
                ) as mock_handle_message:
                    result = runtime.bot.handle_text(
                        user_id="123",
                        session_id="s1",
                        text="/computer revisa la pagina actual y dime que ves",
                    )

                self.assertEqual(result, "Veo una pagina con metricas.")
                runtime.bot.computer.capture_screenshot.assert_called_once_with()
                mock_handle_message.assert_called_once()
                args, kwargs = mock_handle_message.call_args
                self.assertEqual(args[0], "s1")
                self.assertEqual(args[1][0]["type"], "text")
                self.assertEqual(args[1][1]["type"], "image")
                self.assertEqual(args[1][1]["source"]["media_type"], "image/png")
                self.assertEqual(kwargs["memory_text"], "[Screenshot de escritorio]\nrevisa la pagina actual y dime que ves")

    def test_computer_action_command_creates_pending_approval(self) -> None:
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
                runtime.bot.computer = MagicMock()
                runtime.bot.browser_use = None  # prevent real OpenAI calls
                runtime.bot.computer_client_factory = lambda: object()
                runtime.bot.computer_gate = MagicMock()

                def fake_run_agent_loop(*, session, **kwargs):
                    session.status = "awaiting_approval"
                    session.pending_action = {
                        "tool_use_id": "tool-1",
                        "action": "left_click",
                        "coordinate": [500, 300],
                    }
                    return "Action needs approval: left_click — waiting for /action_approve"

                runtime.bot.computer.run_agent_loop.side_effect = fake_run_agent_loop

                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/computer haz click en Revisar tu configuracion",
                )

                self.assertIn("/action_approve", result)
                self.assertIn("/action_abort", result)
                pending = runtime.approvals.list_pending()
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]["metadata"]["kind"], "computer_use")
                self.assertEqual(pending[0]["metadata"]["session_id"], "s1")

    def test_action_approve_resumes_pending_computer_session(self) -> None:
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
                runtime.bot.computer = MagicMock()
                runtime.bot.browser_use = None  # prevent real OpenAI calls
                runtime.bot.computer_client_factory = lambda: object()
                runtime.bot.computer_gate = MagicMock()
                call_count = {"value": 0}

                def fake_run_agent_loop(*, session, **kwargs):
                    call_count["value"] += 1
                    if call_count["value"] == 1:
                        session.status = "awaiting_approval"
                        session.pending_action = {
                            "tool_use_id": "tool-1",
                            "action": "left_click",
                            "coordinate": [500, 300],
                        }
                        return "Action needs approval: left_click — waiting for /action_approve"
                    self.assertEqual(session.pending_action["tool_use_id"], "tool-1")
                    session.status = "done"
                    return "Hecho. Ya hice click y revise la nueva pantalla."

                runtime.bot.computer.run_agent_loop.side_effect = fake_run_agent_loop

                first = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/computer haz click en Revisar tu configuracion",
                )
                match = re.search(r"/action_approve ([^ ]+) ([^`\n ]+)", first)
                self.assertIsNotNone(match)
                approval_id, token = match.group(1), match.group(2)

                second = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text=f"/action_approve {approval_id} {token}",
                )

                self.assertEqual(second, "Hecho. Ya hice click y revise la nueva pantalla.")
                self.assertEqual(call_count["value"], 2)

    def test_computer_abort_cancels_active_session(self) -> None:
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
                runtime.bot._computer_handler._sessions["s1"] = MagicMock(status="running")

                result = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/computer_abort",
                )

                self.assertEqual(result, "computer session aborted")

    def test_get_computer_client_uses_openai(self) -> None:
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
                "OPENAI_API_KEY": "sk-proj-test-key",
            }
            with patch.dict(os.environ, env, clear=True):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                with patch("openai.OpenAI") as mock_openai:
                    client = runtime.bot._computer_handler._get_client()

                mock_openai.assert_called_once_with(api_key="sk-proj-test-key")
                self.assertIs(client, mock_openai.return_value)

    def test_get_computer_client_fails_without_openai_key(self) -> None:
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
            with patch.dict(os.environ, env, clear=True):
                runtime = build_runtime(anthropic_executor=fake_anthropic)
                with self.assertRaises(RuntimeError, msg="OPENAI_API_KEY"):
                    runtime.bot._computer_handler._get_client()

    def test_action_approve_command(self) -> None:
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
                pending = runtime.bot.approvals.create(action="click", summary="test")
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/action_approve {pending.approval_id} {pending.token}")
                self.assertEqual(result, "approved")

    def test_action_abort_command(self) -> None:
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
                pending = runtime.bot.approvals.create(action="click", summary="test")
                result = runtime.bot.handle_text(user_id="123", session_id="s1", text=f"/action_abort {pending.approval_id}")
                self.assertEqual(result, "action rejected")
                self.assertEqual(runtime.bot.approvals.status(pending.approval_id), "rejected")


    def test_playbooks_command_lists_available_playbooks(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/playbooks")
                self.assertIn("Playbooks disponibles", reply)
                self.assertIn("QTS Backtesting", reply)

    def test_playbook_detail_shows_content(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/playbook backtesting")
                self.assertIn("QTS Backtesting", reply)
                self.assertIn("backtest_multi.py", reply)

    def test_backtest_command_without_args_shows_usage(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/backtest")
                self.assertIn("QTS Backtesting", reply)
                self.assertIn("/backtest", reply)

    def test_backtest_command_with_instruction_calls_brain(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/backtest corre ICT para BTC")
                self.assertIsInstance(reply, str)
                self.assertTrue(len(reply) > 0)


    def test_grill_command_without_args_shows_usage(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/grill")
                self.assertIn("/grill", reply)

    def test_grill_command_with_plan_calls_brain(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/grill migrar auth a OAuth2")
                self.assertIsInstance(reply, str)
                self.assertTrue(len(reply) > 0)

    def test_tdd_command_without_args_shows_usage(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/tdd")
                self.assertIn("/tdd", reply)

    def test_improve_arch_command_without_args_calls_brain(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/improve_arch")
                self.assertIsInstance(reply, str)
                self.assertTrue(len(reply) > 0)


    def test_effort_command_shows_current_levels(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/effort")
                self.assertIn("brain:", reply)
                self.assertIn("worker:", reply)

    def test_effort_command_sets_level(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/effort xhigh brain")
                self.assertIn("xhigh", reply)
                self.assertEqual(runtime.bot.config.brain_effort, "xhigh")

    def test_effort_command_sets_all_lanes(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/effort max")
                self.assertIn("max", reply)
                self.assertEqual(runtime.bot.config.brain_effort, "max")
                self.assertEqual(runtime.bot.config.worker_effort, "max")
                self.assertEqual(runtime.bot.config.judge_effort, "max")

    def test_models_command_lists_subscription_and_api_models(self) -> None:
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

                payload = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/models"))

                by_key = {item["key"]: item for item in payload}
                self.assertEqual(by_key["codex:gpt-5.5"]["billing"], "chatgpt_subscription")
                self.assertEqual(by_key["openai:gpt-5.5"]["billing"], "api")

    def test_model_set_persists_session_override_and_warns_for_api_billing(self) -> None:
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

                reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/model set coding codex:gpt-5.5 effort=xhigh",
                )
                api_reply = runtime.bot.handle_text(
                    user_id="123",
                    session_id="s1",
                    text="/model set research openai:gpt-5.5",
                )
                status = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/model status"))
                config = json.loads(runtime.bot.handle_text(user_id="123", session_id="s1", text="/config"))

                self.assertIn("Modelo para worker", reply)
                self.assertIn("chatgpt_subscription", reply)
                self.assertIn("API billing", api_reply)
                self.assertEqual(status["lanes"]["worker"]["provider"], "codex")
                self.assertEqual(status["lanes"]["worker"]["model"], "gpt-5.5")
                self.assertEqual(status["lanes"]["worker"]["effort"], "xhigh")
                self.assertEqual(status["lanes"]["research"]["billing"], "api")
                self.assertEqual(config["lanes"]["worker"]["billing"], "chatgpt_subscription")
                self.assertEqual(config["lanes"]["brain"]["billing"], "claude_subscription_or_api")

    def test_autonomous_task_uses_session_model_override_for_coordinator_and_ledger(self) -> None:
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
                runtime.bot.coordinator = MagicMock()
                runtime.bot.coordinator.run.return_value = CoordinatorResult(
                    task_id="s1:override",
                    phase_results={
                        "research": [WorkerResult(task_name="scope_and_risks", content="ok", duration_seconds=0.1)],
                        "verification": [WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1)],
                    },
                    synthesis="done",
                )

                runtime.bot.handle_text(user_id="123", session_id="s1", text="/autonomy autonomous")
                runtime.bot.handle_text(user_id="123", session_id="s1", text="/model set coding codex:gpt-5.5 effort=xhigh")
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="corrige el bug del login")
                task_id = re.search(r"`([^`]+)`", reply).group(1)
                self.assertTrue(runtime.bot._task_handler.wait_for_task(task_id, timeout=2))

                lane_overrides = runtime.bot.coordinator.run.call_args.kwargs["lane_overrides"]
                self.assertEqual(lane_overrides["worker"]["provider"], "codex")
                self.assertEqual(lane_overrides["worker"]["model"], "gpt-5.5")
                self.assertEqual(lane_overrides["worker"]["effort"], "xhigh")
                record = runtime.task_ledger.get(task_id)
                self.assertEqual(record.provider, "codex")
                self.assertEqual(record.model, "gpt-5.5")

    def test_verify_command_calls_brain(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/verify")
                self.assertIsInstance(reply, str)
                self.assertTrue(len(reply) > 0)

    def test_focus_command_toggles_mode(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/focus")
                self.assertIn("activado", reply)
                reply2 = runtime.bot.handle_text(user_id="123", session_id="s1", text="/focus")
                self.assertIn("desactivado", reply2)

    def test_voice_command_toggles_and_selects(self) -> None:
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
                reply = runtime.bot.handle_text(user_id="123", session_id="s1", text="/voice")
                self.assertIn("activado", reply)
                self.assertIn("nova", reply)
                self.assertEqual(runtime.bot.is_voice_mode("s1"), "nova")
                reply2 = runtime.bot.handle_text(user_id="123", session_id="s1", text="/voice echo")
                self.assertIn("echo", reply2)
                self.assertEqual(runtime.bot.is_voice_mode("s1"), "echo")
                reply3 = runtime.bot.handle_text(user_id="123", session_id="s1", text="/voice off")
                self.assertIn("desactivado", reply3)
                self.assertIsNone(runtime.bot.is_voice_mode("s1"))
                reply4 = runtime.bot.handle_text(user_id="123", session_id="s1", text="/voice invalid")
                self.assertIn("inválida", reply4)


if __name__ == "__main__":
    unittest.main()
