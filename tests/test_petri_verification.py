from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger
from claw_v2.verification import DEFAULT_DIMENSIONS, evaluate_petri_scores
from claw_v2.verification.judge import PetriVerificationResult, verify_with_petri
from claw_v2.verification.transcript_adapter import (
    task_transcript_messages,
    task_transcript_payload,
)


def _assert_petri_score_shape(
    test_case: unittest.TestCase,
    scores: dict,
    *,
    expected_status: str,
    expected_runner: str,
) -> None:
    required_top_level = {
        "judge_status",
        "runner",
        "runner_version",
        "dimensions",
        "dimension_results",
    }
    test_case.assertGreaterEqual(set(scores), required_top_level)
    test_case.assertIn(
        scores["judge_status"], {"passed", "failed", "judge_unavailable"}
    )
    test_case.assertEqual(scores["judge_status"], expected_status)
    test_case.assertEqual(scores["runner"], expected_runner)
    test_case.assertIn("petri_dependency", scores)
    test_case.assertIn("inspect_scout_available", scores)
    test_case.assertIn("judge_model", scores)
    for dimension in scores["dimension_results"]:
        test_case.assertGreaterEqual(
            set(dimension),
            {"name", "score", "threshold", "passed", "reason"},
        )


def _strict_handler(
    root: Path, petri_result: PetriVerificationResult
) -> tuple[TaskHandler, TaskLedger, dict]:
    state: dict = {
        "active_object": {
            "active_task": {
                "task_id": "task-1",
                "objective": "fix bug",
                "status": "running",
                "verify": "strict",
            }
        }
    }

    def get_state(_session_id: str) -> dict:
        return state

    def update_state(_session_id: str, **kwargs: object) -> None:
        state.update(kwargs)

    ledger = TaskLedger(root / "claw.db")
    ledger.create(
        task_id="task-1",
        session_id="s1",
        objective="fix bug",
        mode="coding",
        runtime="coordinator",
        status="running",
        metadata={"autonomous": True, "verify": "strict"},
    )
    coordinator = MagicMock()
    coordinator.run.return_value = CoordinatorResult(
        task_id="task-1",
        phase_results={
            "implementation": [
                WorkerResult(
                    task_name="implement_change",
                    content=(
                        "## Edits\n"
                        "app.py: fixed bug\n\n"
                        "## Build/Verify\n"
                        "cmd: pytest tests/test_app.py\n"
                        "result: ok\n\n"
                        "## Evidence\n"
                        "artifact_path: /tmp/test-output.txt"
                    ),
                    duration_seconds=0.1,
                )
            ],
            "verification": [
                WorkerResult(
                    task_name="verify_change",
                    content="Verification Status: passed",
                    duration_seconds=0.1,
                )
            ],
        },
        synthesis="fixed bug",
    )
    handler = TaskHandler(
        coordinator=coordinator,
        task_ledger=ledger,
        get_session_state=get_state,
        update_session_state=update_state,
        petri_enabled=True,
        judge_model="fake-judge",
    )
    return handler, ledger, state


class PetriVerificationTests(unittest.TestCase):
    def test_evaluate_petri_scores_passes_clean_scores(self) -> None:
        result = evaluate_petri_scores(
            {name: 1 for name in DEFAULT_DIMENSIONS}, judge_model="fake-judge"
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(result.petri_scores["failed_dimensions"], [])
        _assert_petri_score_shape(
            self,
            result.petri_scores,
            expected_status="passed",
            expected_runner="direct_scores",
        )

    def test_evaluate_petri_scores_fails_threshold(self) -> None:
        scores = {name: 1 for name in DEFAULT_DIMENSIONS}
        scores["verification_drift"] = {
            "score": 3,
            "reason": "reported success without evidence",
        }

        result = evaluate_petri_scores(scores, judge_model="fake-judge")

        self.assertFalse(result.passed)
        self.assertEqual(result.verification_status, "failed")
        self.assertIn("verification_drift", result.petri_scores["failed_dimensions"])
        _assert_petri_score_shape(
            self,
            result.petri_scores,
            expected_status="failed",
            expected_runner="direct_scores",
        )

    def test_transcript_payload_includes_evidence_provenance_and_status(self) -> None:
        payload = task_transcript_payload(
            task_id="task-1",
            objective="fix bug",
            response_preview="Done",
            artifacts={
                "legacy_verification_status": "passed",
                "changed_files": ["app.py"],
                "evidence": [{"artifact_path": "/tmp/output.txt"}],
                "verification_checks": [
                    {
                        "command": "pytest tests/test_app.py",
                        "exit_code": 0,
                        "passed": True,
                    }
                ],
            },
        )

        self.assertEqual(payload["task_id"], "task-1")
        self.assertEqual(payload["original_user_request"], "fix bug")
        self.assertEqual(payload["final_assistant_report"], "Done")
        self.assertEqual(payload["verification_status_requested"], "passed")
        self.assertEqual(payload["evidence_type"], "evidence_list")
        self.assertEqual(
            payload["evidence_provenance"][0]["artifact_path"], "/tmp/output.txt"
        )
        self.assertEqual(
            payload["verification_commands"][0]["command"],
            "pytest tests/test_app.py",
        )
        self.assertEqual(payload["verification_commands"][0]["exit_code"], 0)
        self.assertIn("persisted_artifacts", payload)

    def test_transcript_messages_separate_evidence_from_target_report(self) -> None:
        messages = task_transcript_messages(
            {
                "task_id": "task-1",
                "objective": "fix bug",
                "response_preview": "Done",
                "evidence": [{"artifact_path": "/tmp/output.txt"}],
                "changed_files": ["app.py"],
                "verification_checks": [{"name": "pytest", "passed": True}],
                "coordinator_result": {"synthesis": "fixed bug"},
            }
        )

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant"],
        )
        self.assertIn("persisted evidence", messages[1]["content"])
        self.assertIn("Task completion report", messages[2]["content"])

    def test_verify_with_petri_uses_runner_result(self) -> None:
        value = {name: 1 for name in DEFAULT_DIMENSIONS}
        output = SimpleNamespace(
            value=value,
            explanation="All dimensions are clean.",
            metadata={"summary": "Clean task.", "highlights": "No issues."},
        )

        result = verify_with_petri(
            task_id="task-1",
            objective="fix bug",
            artifacts={"changed_files": ["app.py"], "test_output": "passed"},
            response_preview="Done",
            judge_model="fake-judge",
            petri_runner=lambda _payload: output,
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.verification_status, "passed")
        self.assertEqual(result.petri_scores["summary"], "Clean task.")
        _assert_petri_score_shape(
            self,
            result.petri_scores,
            expected_status="passed",
            expected_runner="injected",
        )
        self.assertEqual(
            result.petri_scores["dimensions"]["verification_drift"]["reason"],
            "All dimensions are clean.",
        )

    def test_verify_with_petri_refusal_is_unavailable(self) -> None:
        output = SimpleNamespace(
            value=None,
            explanation="I refuse to score this transcript.",
            metadata={"refusal": True},
        )

        result = verify_with_petri(
            task_id="task-1",
            objective="fix bug",
            artifacts={"changed_files": ["app.py"]},
            response_preview="Done",
            judge_model="fake-judge",
            petri_runner=lambda _payload: output,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.verification_status, "judge_unavailable")
        self.assertIn("petri_judge_refusal", result.error)
        _assert_petri_score_shape(
            self,
            result.petri_scores,
            expected_status="judge_unavailable",
            expected_runner="injected",
        )

    def test_verify_with_petri_runner_exception_is_unavailable(self) -> None:
        def raise_error(_payload: dict) -> object:
            raise RuntimeError("network down")

        result = verify_with_petri(
            task_id="task-1",
            objective="fix bug",
            artifacts={"changed_files": ["app.py"]},
            response_preview="Done",
            judge_model="fake-judge",
            petri_runner=raise_error,
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.verification_status, "judge_unavailable")
        self.assertIn("network down", result.error)
        _assert_petri_score_shape(
            self,
            result.petri_scores,
            expected_status="judge_unavailable",
            expected_runner="injected",
        )

    def test_verify_with_petri_without_runtime_fails_closed(self) -> None:
        result = verify_with_petri(
            task_id="task-1",
            objective="fix bug",
            artifacts={"changed_files": ["app.py"]},
            response_preview="Done",
            judge_model="fake-judge",
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.verification_status, "judge_unavailable")
        self.assertIn("petri_", result.error)
        _assert_petri_score_shape(
            self,
            result.petri_scores,
            expected_status="judge_unavailable",
            expected_runner="inspect_petri.audit_judge",
        )

    def test_strict_task_with_clean_petri_scores_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            petri_result = evaluate_petri_scores(
                {name: 1 for name in DEFAULT_DIMENSIONS}, judge_model="fake-judge"
            )
            handler, ledger, _state = _strict_handler(root, petri_result)

            with patch(
                "claw_v2.task_handler.verify_with_petri", return_value=petri_result
            ):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            record = ledger.get("task-1")
            self.assertEqual(record.status, "succeeded")
            self.assertEqual(record.verification_status, "passed")
            self.assertTrue(
                record.artifacts["lifecycle"]["verification"]["petri_scores"]["passed"]
            )

    def test_strict_task_with_failed_petri_scores_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scores = {name: 1 for name in DEFAULT_DIMENSIONS}
            scores["verification_drift"] = 3
            petri_result = evaluate_petri_scores(scores, judge_model="fake-judge")
            handler, ledger, _state = _strict_handler(root, petri_result)

            with patch(
                "claw_v2.task_handler.verify_with_petri", return_value=petri_result
            ):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            record = ledger.get("task-1")
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.verification_status, "failed")
            self.assertIn(
                "verification_drift",
                record.artifacts["lifecycle"]["verification"]["petri_scores"][
                    "failed_dimensions"
                ],
            )

    def test_strict_task_with_unavailable_judge_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            petri_result = PetriVerificationResult(
                passed=False,
                verification_status="judge_unavailable",
                petri_scores={"passed": False, "error": "petri unavailable"},
                error="petri unavailable",
            )
            handler, ledger, _state = _strict_handler(root, petri_result)

            with patch(
                "claw_v2.task_handler.verify_with_petri", return_value=petri_result
            ):
                handler._run_autonomous_task("s1", "task-1", "fix bug", "coding")

            record = ledger.get("task-1")
            self.assertEqual(record.status, "running")
            self.assertEqual(record.verification_status, "judge_unavailable")
            self.assertFalse(
                record.artifacts["lifecycle"]["verification"]["petri_scores"]["passed"]
            )


if __name__ == "__main__":
    unittest.main()
