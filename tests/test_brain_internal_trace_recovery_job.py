"""Tests for the provider_repeated_internal_trace recovery-job wiring.

Closes risk #3 from the P0 hotfix audit: the bot.py
internal_trace_repeated branch silently fell back to the generic apology
even though brain hotfix B already shipped a recovery_jobs table. This
hooks that branch into the same machinery so actionable requests are
preserved.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.brain import (
    BrainService,
    INTERNAL_TOOL_TRACE_FALLBACK,
    _format_recovery_message_body,
)
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(tempfile.mkdtemp()) / "test.db"
        self.memory = MemoryStore(self.db_path)
        self.observe = ObserveStream(self.db_path)
        self.brain = BrainService(
            router=MagicMock(),
            memory=self.memory,
            system_prompt="You are Claw.",
            observe=self.observe,
        )


class QueueInternalTraceRecoveryJobTests(_Harness):
    def test_creates_job_with_provider_repeated_internal_trace_when_actionable(self) -> None:
        result = self.brain.queue_internal_trace_recovery_job(
            "s1", source_text="arregla el deployment de producción ahora"
        )

        self.assertIsNotNone(result)
        job_id, message = result
        jobs = self.memory.list_pending_recovery_jobs("s1")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["failure_reason"], "provider_repeated_internal_trace")
        self.assertIn("arregla el deployment", job["original_request_sanitized"])
        self.assertIn(f"#{job_id}", message)
        # bot.py feeds the message into _sanitize_visible_chat_response, so the
        # body must be plain text (no <response> tags) ready for Telegram.
        self.assertNotIn("<response>", message)
        self.assertNotIn(INTERNAL_TOOL_TRACE_FALLBACK, message)

    def test_returns_none_when_request_is_not_actionable(self) -> None:
        result = self.brain.queue_internal_trace_recovery_job("s1", source_text="hola")
        self.assertIsNone(result)
        self.assertEqual(self.memory.list_pending_recovery_jobs("s1"), [])

    def test_emits_recovery_job_created_event(self) -> None:
        self.brain.queue_internal_trace_recovery_job(
            "s1", source_text="arregla el bug del login ya"
        )
        events = self.observe.recent_events(limit=100)
        types = [row["event_type"] for row in events]
        self.assertIn("recovery_job_created", types)


class FormatRecoveryMessageBodyTests(unittest.TestCase):
    def test_body_has_no_response_tags(self) -> None:
        body = _format_recovery_message_body(
            "provider_repeated_internal_trace", "arregla X", 42
        )
        self.assertNotIn("<response>", body)
        self.assertNotIn("</response>", body)
        self.assertIn("#42", body)
        self.assertIn("trace interno", body)


class BotInternalTraceRepeatedBranchTests(unittest.TestCase):
    """End-to-end: a bot turn where the clean-retry STILL produces an
    internal-trace-suppressed response (the actual failure mode from the
    2026-05-24 incident) must now persist a recovery_jobs entry rather
    than silently returning the generic apology.
    """

    def setUp(self) -> None:
        import os
        from pathlib import Path as _Path
        from unittest.mock import patch as _patch

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._pipeline_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._pipeline_tmp.cleanup)
        self._telemetry_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._telemetry_tmp.cleanup)
        root = _Path(self._tmpdir.name)
        env = {
            "DB_PATH": str(root / "data" / "claw.db"),
            "WORKSPACE_ROOT": str(root / "workspace"),
            "AGENT_STATE_ROOT": str(root / "agents"),
            "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
            "APPROVALS_ROOT": str(root / "approvals"),
            "TELEGRAM_ALLOWED_USER_ID": "123",
            "PIPELINE_STATE_ROOT": str(_Path(self._pipeline_tmp.name) / "pipeline"),
            "TELEMETRY_ROOT": str(_Path(self._telemetry_tmp.name) / "telemetry"),
        }
        env_patch = _patch.dict(os.environ, env, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        from claw_v2.main import build_runtime
        from claw_v2.types import LLMResponse as _LLMResponse

        # Executor returns content that the brain's _extract step flags
        # as internal-tool-trace, so the retry inside
        # _recover_internal_trace_suppression naturally trips the
        # internal_trace_repeated branch.
        def _fake_exec(request):
            return _LLMResponse(
                content="to=functions.deploy({\"env\": \"prod\"})",
                lane=request.lane,
                provider="anthropic",
                model=request.model,
            )

        self.runtime = build_runtime(anthropic_executor=_fake_exec)

    def test_internal_trace_repeated_branch_creates_recovery_job(self) -> None:
        from claw_v2.types import LLMResponse

        session_id = "s-recovery-test"

        # Initial suppressed response (the one the bot already received
        # before invoking _recover_internal_trace_suppression).
        suppressed = LLMResponse(
            content="anything",
            lane="brain",
            provider="anthropic",
            model="claude-opus-4-7",
            artifacts={"internal_tool_trace_suppressed": True},
        )

        content = self.runtime.bot._recover_internal_trace_suppression(
            session_id=session_id,
            source_text="arregla el deployment de producción ahora",
            response=suppressed,
            runtime_capability_question=False,
            link_analysis_context=None,
            runtime_channel="telegram",
            pre_turn_message_id=0,
        )

        # A recovery_jobs row must exist with the right failure_reason.
        jobs = self.runtime.bot.brain.memory.list_pending_recovery_jobs(session_id)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job["failure_reason"], "provider_repeated_internal_trace")
        self.assertIn("arregla el deployment", job["original_request_sanitized"])

        # The user-facing message must reference the queued job number
        # instead of the bare INTERNAL_TOOL_TRACE_FALLBACK.
        self.assertNotIn(INTERNAL_TOOL_TRACE_FALLBACK, content)
        self.assertIn(f"#{job['id']}", content)


if __name__ == "__main__":
    unittest.main()
