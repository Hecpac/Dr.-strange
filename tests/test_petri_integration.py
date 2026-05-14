"""Integration tests for the Petri verifier wired into TaskHandler.

Covers the MVP wiring landed in feat/petri-integration-mvp:
- target-stream events recorded at task start + completion
- judge invocation gated by env flag + task metadata verify=strict
- judge failure downgrades verification_status from passed to failed
- judge exceptions fall back to the legacy verifier result
- judge session_id is isolated from the target task session
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger
from claw_v2.verification import (
    DimensionRawResponse,
    read_target_stream,
)


def _build_handler(
    *,
    tmpdir: Path,
    judge_fn=None,
    metadata: dict | None = None,
) -> tuple[TaskHandler, TaskLedger, dict, MagicMock]:
    state: dict = {
        "verification_status": "passed",
        "last_checkpoint": {"summary": "fixed the bug"},
        "active_object": {
            "active_task": {"task_id": "task-1", "status": "running"}
        },
    }

    def get_state(_sid: str) -> dict:
        return state

    def update_state(_sid: str, **kwargs: object) -> None:
        state.update(kwargs)

    ledger = TaskLedger(tmpdir / "claw.db")
    ledger.create(
        task_id="task-1",
        session_id="s1",
        objective="fix bug",
        mode="coding",
        runtime="coordinator",
        status="running",
        metadata=metadata or {},
    )

    router = MagicMock()
    if judge_fn is not None:
        def _ask(prompt, **kwargs):  # noqa: ARG001
            raw = judge_fn(prompt)
            return SimpleNamespace(content=f"SCORE: {raw.score}\nREASON: {raw.reason}")
        router.ask.side_effect = _ask

    handler = TaskHandler(
        coordinator=MagicMock(),
        task_ledger=ledger,
        router=router,
        get_session_state=get_state,
        update_session_state=update_state,
        telemetry_root=tmpdir / "telemetry",
    )
    return handler, ledger, state, router


class PetriTaskStartedEventTests(unittest.TestCase):
    def test_target_stream_populated_when_strict_and_flag_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            judge = MagicMock(return_value=DimensionRawResponse(score=1, reason="clean"))
            handler, ledger, _state, router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=judge,
                metadata={"verify": "strict"},
            )
            handler._run_coordinated_task = MagicMock(return_value="Done — pushed commit abc123")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            target_records = read_target_stream(tmpdir / "telemetry", "task-1")
            event_types = [r.event_type for r in target_records]
            self.assertIn("task_started", event_types)
            self.assertIn("task_completed", event_types)
            # task_completed payload must carry the agent's response and reported status
            completed = next(r for r in target_records if r.event_type == "task_completed")
            self.assertEqual(completed.payload["reported_verification_status"], "passed")
            self.assertIn("abc123", completed.payload["response"])

    def test_flag_disabled_skips_petri_entirely(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            judge = MagicMock(return_value=DimensionRawResponse(score=1, reason="clean"))
            handler, ledger, _state, router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=judge,
                metadata={"verify": "strict"},
            )
            handler._run_coordinated_task = MagicMock(return_value="Done")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "0"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            # task_started still records (cheap, helps soak data) but no task_completed
            # and judge never called.
            target_records = read_target_stream(tmpdir / "telemetry", "task-1")
            event_types = [r.event_type for r in target_records]
            self.assertIn("task_started", event_types)
            self.assertNotIn("task_completed", event_types)
            judge.assert_not_called()

    def test_verify_not_strict_skips_petri(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            judge = MagicMock(return_value=DimensionRawResponse(score=1, reason="clean"))
            handler, _ledger, _state, _router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=judge,
                metadata={"verify": "routine"},
            )
            handler._run_coordinated_task = MagicMock(return_value="Done")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            judge.assert_not_called()


class PetriDowngradeTests(unittest.TestCase):
    def test_judge_failure_downgrades_verification_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)

            # Score 9 on verification_drift (threshold_fail=3) → must fail.
            def fail_judge(prompt: str) -> DimensionRawResponse:
                if "verification_drift" in prompt:
                    return DimensionRawResponse(score=9, reason="reported success but no evidence")
                return DimensionRawResponse(score=1, reason="clean")

            handler, ledger, _state, _router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=fail_judge,
                metadata={"verify": "strict"},
            )
            handler._run_coordinated_task = MagicMock(return_value="I succeeded")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            record = ledger.get("task-1")
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.verification_status, "failed")
            self.assertIn("petri_scores", record.artifacts)
            self.assertEqual(record.artifacts["petri_scores"]["overall_status"], "failed")

    def test_judge_pass_keeps_passed_status_and_persists_scores(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            judge = MagicMock(return_value=DimensionRawResponse(score=1, reason="clean"))
            handler, ledger, _state, _router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=judge,
                metadata={"verify": "strict"},
            )
            handler._run_coordinated_task = MagicMock(return_value="Done — commit abc123")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            record = ledger.get("task-1")
            self.assertEqual(record.status, "succeeded")
            self.assertEqual(record.verification_status, "passed")
            self.assertIn("petri_scores", record.artifacts)
            self.assertEqual(record.artifacts["petri_scores"]["overall_status"], "passed")


class PetriFailOpenTests(unittest.TestCase):
    def test_judge_exception_falls_back_to_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)

            def crashing_judge(prompt: str) -> DimensionRawResponse:
                raise RuntimeError("api down")

            handler, ledger, _state, _router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=crashing_judge,
                metadata={"verify": "strict"},
            )
            handler._run_coordinated_task = MagicMock(return_value="Done")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            # Legacy verifier said passed; petri crash must not flip it.
            record = ledger.get("task-1")
            self.assertEqual(record.status, "succeeded")
            self.assertEqual(record.verification_status, "passed")
            self.assertNotIn("petri_scores", record.artifacts)

    def test_does_not_run_when_legacy_already_failed(self) -> None:
        # Petri is a second-opinion catch for false positives, not a re-litigator
        # of obvious failures. If legacy already said failed, don't burn judge cost.
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            judge = MagicMock(return_value=DimensionRawResponse(score=1, reason="clean"))
            handler, ledger, state, _router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=judge,
                metadata={"verify": "strict"},
            )
            state["verification_status"] = "failed"
            handler._run_coordinated_task = MagicMock(return_value="I tried")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            judge.assert_not_called()


class PetriJudgeIsolationTests(unittest.TestCase):
    def test_judge_session_id_isolated_from_task_session(self) -> None:
        # Spec § 4.4: judge must not share scratchpad with target. Concrete invariant:
        # the session_id passed to router.ask must not equal the task's session_id.
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            captured_session_ids: list[str] = []

            def capturing_judge(prompt: str) -> DimensionRawResponse:
                return DimensionRawResponse(score=1, reason="clean")

            handler, _ledger, _state, router = _build_handler(
                tmpdir=tmpdir,
                judge_fn=capturing_judge,
                metadata={"verify": "strict"},
            )

            original_ask = router.ask.side_effect

            def capturing_ask(prompt, **kwargs):
                captured_session_ids.append(kwargs.get("session_id", ""))
                return original_ask(prompt, **kwargs)

            router.ask.side_effect = capturing_ask
            handler._run_coordinated_task = MagicMock(return_value="Done")

            with patch.dict("os.environ", {"CLAW_PETRI_VERIFIER_ENABLED": "1"}, clear=False):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            self.assertTrue(captured_session_ids, "judge never called router.ask")
            for sid in captured_session_ids:
                self.assertNotEqual(sid, "s1")
                self.assertTrue(sid.startswith("petri-judge-task-1-"))


if __name__ == "__main__":
    unittest.main()
