from __future__ import annotations

import unittest

from claw_v2.task_completion import (
    COMPLETION_CANDIDATES,
    CompletionDecision,
    validate_completion,
)


class ValidateCompletionTests(unittest.TestCase):
    def test_cannot_succeed_without_passed_verification(self) -> None:
        record = {
            "status": "succeeded",
            "verification_status": "pending",
            "summary": "Notebook created.",
            "artifacts": {"notebook_title": "X"},
            "evidence": {},
        }
        decision = validate_completion(record)
        self.assertNotEqual(decision.final_status, "succeeded")
        self.assertEqual(decision.reason, "success_without_passed_verification")

    def test_cannot_succeed_without_evidence(self) -> None:
        record = {
            "status": "succeeded",
            "verification_status": "passed",
            "summary": "All good.",
            "artifacts": {},
            "evidence": {},
        }
        decision = validate_completion(record)
        self.assertNotEqual(decision.final_status, "succeeded")
        self.assertEqual(decision.reason, "success_without_evidence")
        self.assertIn("tool_result_or_artifact", decision.missing_evidence)

    def test_plan_only_summary_blocks_succeeded(self) -> None:
        record = {
            "status": "succeeded",
            "verification_status": "passed",
            "summary": "Step 1: plan things.\nStep 2: execute things.",
            "artifacts": {},
            "evidence": {},
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "pending")
        self.assertEqual(decision.reason, "plan_only_no_execution_evidence")

    def test_plan_only_with_evidence_does_not_trigger_plan_branch(self) -> None:
        record = {
            "status": "succeeded",
            "verification_status": "passed",
            "summary": "Step 1: plan things.\nStep 2: done.",
            "artifacts": {"notebook_title": "Notebook X"},
            "evidence": {"handler_result": "created"},
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "succeeded")

    def test_can_succeed_with_passed_verification_and_evidence(self) -> None:
        record = {
            "status": "succeeded",
            "verification_status": "passed",
            "summary": "Notebook created.",
            "artifacts": {"notebook_title": "Research Notebook"},
            "evidence": {"handler_result": "created"},
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "succeeded")
        self.assertEqual(decision.verification_status, "passed")
        self.assertEqual(decision.reason, "verified_with_evidence")

    def test_running_task_can_persist_without_evidence(self) -> None:
        record = {
            "status": "running",
            "verification_status": "pending",
            "summary": "Inspecting...",
            "artifacts": {},
            "evidence": {},
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "running")
        self.assertEqual(decision.reason, "not_terminal_or_not_ready")

    def test_failed_verification_yields_failed(self) -> None:
        record = {
            "status": "running",
            "verification_status": "failed",
            "summary": "ran but failed",
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "failed")
        self.assertEqual(decision.verification_status, "failed")

    def test_blocked_verification_yields_blocked(self) -> None:
        record = {
            "status": "running",
            "verification_status": "blocked",
            "summary": "needs approval",
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "blocked")

    def test_done_with_evidence_passes(self) -> None:
        record = {
            "status": "done",
            "verification_status": "verified",
            "summary": "PR merged.",
            "artifacts": {"pr_url": "https://example.com/pr/1"},
            "evidence": {},
        }
        decision = validate_completion(record)
        self.assertEqual(decision.final_status, "succeeded")

    def test_completion_candidates_match_success_statuses(self) -> None:
        self.assertIn("succeeded", COMPLETION_CANDIDATES)
        self.assertIn("completed", COMPLETION_CANDIDATES)
        self.assertIn("done", COMPLETION_CANDIDATES)
        self.assertIn("closed", COMPLETION_CANDIDATES)

    def test_returns_completion_decision_dataclass(self) -> None:
        decision = validate_completion({"status": "queued"})
        self.assertIsInstance(decision, CompletionDecision)


if __name__ == "__main__":
    unittest.main()
