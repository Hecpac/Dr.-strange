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


class PetriWorkerResultEventTests(unittest.TestCase):
    """Coordinator phase results land in the target stream so the judge sees
    concrete evidence per phase (file paths, commit hashes, exit codes), not
    just the agent's final claim."""

    def test_worker_results_recorded_to_target_stream(self) -> None:
        from claw_v2.coordinator import CoordinatorResult, WorkerResult

        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            state: dict = {
                "verification_status": "passed",
                "last_checkpoint": {"summary": "done"},
                "active_object": {"active_task": {"task_id": "task-1"}},
                "task_queue": [],
            }

            def get_state(_sid: str) -> dict:
                return state

            def update_state(_sid: str, **kwargs: object) -> None:
                state.update(kwargs)

            ledger = TaskLedger(tmpdir / "claw.db")
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="apply redaction fix",
                mode="coding",
                runtime="coordinator",
                status="running",
            )

            coordinator = MagicMock()
            coordinator.run.return_value = CoordinatorResult(
                task_id="task-1",
                phase_results={
                    "implementation": [
                        WorkerResult(
                            task_name="apply_fix",
                            content="Edited claw_v2/redaction.py at line 14. Commit abc123.",
                            duration_seconds=2.5,
                        )
                    ],
                    "verification": [
                        WorkerResult(
                            task_name="run_tests",
                            content="Ran pytest tests/test_redaction.py — 43 passed",
                            duration_seconds=1.0,
                        )
                    ],
                },
                synthesis="Redaction fix applied and tests pass.",
            )

            handler = TaskHandler(
                coordinator=coordinator,
                task_ledger=ledger,
                get_session_state=get_state,
                update_session_state=update_state,
                telemetry_root=tmpdir / "telemetry",
            )

            handler._run_coordinated_task(
                "s1", "apply redaction fix", mode="coding", forced=False, task_id="task-1"
            )

            target = read_target_stream(tmpdir / "telemetry", "task-1")
            event_types = [r.event_type for r in target]
            self.assertIn("worker_result", event_types)
            self.assertIn("coordinator_synthesis", event_types)

            # Concrete invariant: implementation worker_result must carry the
            # commit hash that the judge will look for under verification_drift.
            impl_events = [
                r for r in target
                if r.event_type == "worker_result" and r.payload.get("phase") == "implementation"
            ]
            self.assertEqual(len(impl_events), 1)
            self.assertIn("abc123", impl_events[0].payload["content"])

    def test_worker_results_skipped_when_telemetry_root_missing(self) -> None:
        # Defensive: handlers built without a telemetry_root (e.g. unit tests
        # that don't care about petri) must not raise when the coordinator
        # returns results.
        from claw_v2.coordinator import CoordinatorResult, WorkerResult

        state: dict = {
            "verification_status": "passed",
            "last_checkpoint": {"summary": "done"},
            "active_object": {"active_task": {"task_id": "task-1"}},
            "task_queue": [],
        }

        def get_state(_sid: str) -> dict:
            return state

        def update_state(_sid: str, **kwargs: object) -> None:
            state.update(kwargs)

        coordinator = MagicMock()
        coordinator.run.return_value = CoordinatorResult(
            task_id="task-1",
            phase_results={"implementation": [WorkerResult("t", "c", 0.1)]},
            synthesis="done",
        )

        handler = TaskHandler(
            coordinator=coordinator,
            task_ledger=None,
            get_session_state=get_state,
            update_session_state=update_state,
            telemetry_root=None,
        )

        # Should not raise.
        handler._run_coordinated_task(
            "s1", "x", mode="coding", forced=False, task_id="task-1"
        )


class PetriVerifyKwargTests(unittest.TestCase):
    """Path B production opt-in: start_autonomous_task accepts verify="strict"
    and threads it into task_ledger metadata where _maybe_run_petri_verifier
    reads it. Tests stub _run_autonomous_task to avoid spawning the worker
    thread (it would race the tmpdir cleanup)."""

    def _make_handler(self, ledger: TaskLedger, tmpdir: Path) -> TaskHandler:
        state: dict = {"task_queue": [], "active_object": {}}

        def get_state(_sid: str) -> dict:
            return state

        def update_state(_sid: str, **kwargs: object) -> None:
            state.update(kwargs)

        handler = TaskHandler(
            coordinator=MagicMock(),
            task_ledger=ledger,
            get_session_state=get_state,
            update_session_state=update_state,
            telemetry_root=tmpdir / "telemetry",
        )
        # Stub the worker so tests assert only on metadata creation,
        # not on coordinator execution.
        handler._run_autonomous_task = MagicMock()
        return handler

    def test_start_autonomous_task_propagates_verify_to_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            ledger = TaskLedger(tmpdir / "claw.db")
            handler = self._make_handler(ledger, tmpdir)

            handler.start_autonomous_task(
                "sess-1",
                "Refactor brain.py",
                mode="coding",
                verify="strict",
            )

            records = list(ledger.list(statuses=("running",), limit=5))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].metadata.get("verify"), "strict")

    def test_start_autonomous_task_without_verify_omits_field(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmpdir = Path(raw)
            ledger = TaskLedger(tmpdir / "claw.db")
            handler = self._make_handler(ledger, tmpdir)

            handler.start_autonomous_task("sess-1", "Quick lookup", mode="research")

            records = list(ledger.list(statuses=("running",), limit=5))
            self.assertEqual(len(records), 1)
            self.assertNotIn("verify", records[0].metadata)


if __name__ == "__main__":
    unittest.main()
