from __future__ import annotations

import unittest

from claw_v2.coordinator_schema import (
    COORDINATOR_RESULT_SCHEMA,
    CoordinatorValidation,
    coerce_unstructured_coordinator_output,
    validate_coordinator_result,
    validate_coordinator_semantics,
)


def _valid_payload() -> dict:
    return {
        "status": "executed",
        "task_kind": "notebooklm_create",
        "actions_taken": [
            {
                "agent": "nlm",
                "action": "create_notebook",
                "tool": "notebooklm.create",
                "result": "ok",
            }
        ],
        "evidence": [
            {
                "type": "handler_result",
                "name": "notebook_title",
                "value": "Research Notebook",
            }
        ],
        "changed_files": [],
        "verification": {
            "status": "passed",
            "checks": [
                {
                    "name": "handler_result_present",
                    "status": "passed",
                    "evidence": "notebook title returned",
                }
            ],
        },
        "blockers": [],
        "next_user_action": None,
        "summary_for_user": "Cuaderno creado y verificado.",
    }


class SchemaShapeTests(unittest.TestCase):
    def test_valid_payload_passes(self) -> None:
        result = validate_coordinator_result(_valid_payload())
        self.assertIsInstance(result, CoordinatorValidation)
        self.assertTrue(result.valid)
        self.assertEqual(result.errors, [])

    def test_missing_required_keys(self) -> None:
        payload = _valid_payload()
        del payload["evidence"]
        result = validate_coordinator_result(payload)
        self.assertFalse(result.valid)
        self.assertIn("missing:evidence", result.errors)

    def test_invalid_status(self) -> None:
        payload = _valid_payload()
        payload["status"] = "succeeded"
        result = validate_coordinator_result(payload)
        self.assertFalse(result.valid)
        self.assertIn("invalid_status", result.errors)

    def test_extras_blocked(self) -> None:
        payload = _valid_payload()
        payload["smuggled"] = "extra"
        result = validate_coordinator_result(payload)
        self.assertFalse(result.valid)
        self.assertTrue(any("additional_properties" in err for err in result.errors))

    def test_actions_must_have_required_keys(self) -> None:
        payload = _valid_payload()
        payload["actions_taken"] = [{"agent": "x", "action": "y"}]
        result = validate_coordinator_result(payload)
        self.assertFalse(result.valid)
        self.assertTrue(any("actions_taken[0].missing" in err for err in result.errors))

    def test_schema_has_strict_root(self) -> None:
        self.assertEqual(COORDINATOR_RESULT_SCHEMA.get("additionalProperties"), False)
        for key in ("actions_taken", "evidence", "verification"):
            section = COORDINATOR_RESULT_SCHEMA["properties"][key]
            inner = section.get("items") or section
            self.assertEqual(
                inner.get("additionalProperties", False), False,
                msg=key,
            )


class SemanticsTests(unittest.TestCase):
    def test_executed_requires_actions_taken(self) -> None:
        payload = _valid_payload()
        payload["actions_taken"] = []
        errors = validate_coordinator_semantics(payload)
        self.assertIn("executed_requires_actions_taken", errors)

    def test_passed_verification_requires_evidence(self) -> None:
        payload = _valid_payload()
        payload["evidence"] = []
        payload["verification"]["checks"] = []
        errors = validate_coordinator_semantics(payload)
        self.assertIn("passed_verification_requires_evidence", errors)

    def test_passed_check_evidence_satisfies_verification_evidence(self) -> None:
        payload = _valid_payload()
        payload["evidence"] = []
        errors = validate_coordinator_semantics(payload)
        self.assertNotIn("passed_verification_requires_evidence", errors)

    def test_blockers_force_blocked_or_pending(self) -> None:
        payload = _valid_payload()
        payload["blockers"] = ["missing approval"]
        errors = validate_coordinator_semantics(payload)
        self.assertIn("blockers_require_blocked_or_pending_status", errors)

    def test_executed_requires_passed_verification(self) -> None:
        payload = _valid_payload()
        payload["verification"]["status"] = "pending"
        errors = validate_coordinator_semantics(payload)
        self.assertIn("executed_requires_passed_verification", errors)

    def test_summary_for_user_does_not_count_as_evidence(self) -> None:
        payload = {
            "status": "executed",
            "task_kind": "coding_patch",
            "actions_taken": [{"agent": "hex", "action": "patch", "tool": "none", "result": "claimed"}],
            "evidence": [],
            "changed_files": [],
            "verification": {"status": "passed", "checks": []},
            "blockers": [],
            "next_user_action": None,
            "summary_for_user": "I fixed it.",
        }
        errors = validate_coordinator_semantics(payload)
        self.assertIn("passed_verification_requires_evidence", errors)

    def test_valid_payload_has_no_semantic_errors(self) -> None:
        self.assertEqual(validate_coordinator_semantics(_valid_payload()), [])

    def test_blocked_with_blockers_is_valid(self) -> None:
        payload = _valid_payload()
        payload["status"] = "blocked"
        payload["blockers"] = ["awaiting human approval"]
        payload["actions_taken"] = []
        payload["verification"]["status"] = "blocked"
        errors = validate_coordinator_semantics(payload)
        self.assertEqual(errors, [])


class CoerceUnstructuredTests(unittest.TestCase):
    def test_unstructured_output_becomes_pending(self) -> None:
        result = coerce_unstructured_coordinator_output("Step 1: inspect\nStep 2: execute")
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["verification"]["status"], "pending")
        self.assertEqual(result["actions_taken"], [])
        self.assertEqual(result["evidence"], [])
        self.assertTrue(result["blockers"])
        self.assertIn("Step 1", result["summary_for_user"])

    def test_unstructured_passes_schema_and_semantics(self) -> None:
        result = coerce_unstructured_coordinator_output("freeform plan")
        validation = validate_coordinator_result(result)
        self.assertTrue(validation.valid, msg=validation.errors)
        self.assertEqual(validate_coordinator_semantics(result), [])

    def test_none_input_handled(self) -> None:
        result = coerce_unstructured_coordinator_output(None)
        self.assertEqual(result["summary_for_user"], "")
        self.assertEqual(result["status"], "pending")

    def test_summary_truncated(self) -> None:
        long_text = "x" * 5000
        result = coerce_unstructured_coordinator_output(long_text)
        self.assertLessEqual(len(result["summary_for_user"]), 1000)


class CheckpointIntegrationTests(unittest.TestCase):
    def test_passed_without_evidence_downgrades_to_pending(self) -> None:
        from claw_v2.bot_helpers import _coordinator_checkpoint
        from claw_v2.coordinator import CoordinatorResult, WorkerResult

        result = CoordinatorResult(
            task_id="task-x",
            phase_results={
                "verification": [
                    WorkerResult(
                        task_name="verify_change",
                        content="Verification Status: passed",
                        duration_seconds=0.1,
                    )
                ],
            },
            synthesis="Step 1: plan...\nStep 2: execute...",
        )

        checkpoint = _coordinator_checkpoint(result, objective="implement feature")

        # Implementation phase has no entries → no real evidence,
        # so structured payload coerces to unstructured + pending.
        self.assertIn("coordinator_result", checkpoint)
        self.assertIn("coordinator_semantic_errors", checkpoint)
        self.assertEqual(checkpoint["verification_status"], "pending")

    def test_passed_with_implementation_evidence_stays_passed(self) -> None:
        from claw_v2.bot_helpers import _coordinator_checkpoint
        from claw_v2.coordinator import CoordinatorResult, WorkerResult

        result = CoordinatorResult(
            task_id="task-y",
            phase_results={
                "implementation": [
                    WorkerResult(task_name="apply_patch", content="patched files: a.py", duration_seconds=0.1),
                ],
                "verification": [
                    WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1),
                ],
            },
            synthesis="Done",
        )

        checkpoint = _coordinator_checkpoint(result, objective="implement feature")

        self.assertEqual(checkpoint["verification_status"], "passed")
        structured = checkpoint["coordinator_result"]
        self.assertEqual(structured["status"], "executed")
        self.assertTrue(structured["evidence"])

    def test_explicit_evidence_none_degrades_passed_to_pending(self) -> None:
        from claw_v2.bot_helpers import _coordinator_checkpoint
        from claw_v2.coordinator import CoordinatorResult, WorkerResult

        result = CoordinatorResult(
            task_id="task-none",
            phase_results={
                "implementation": [
                    WorkerResult(
                        task_name="implement_change",
                        content="## Edits\napp.py: changed\n\n## Build/Verify\ncmd: pytest\nresult: ok\n\n## Evidence\nnone",
                        duration_seconds=0.1,
                    )
                ],
                "verification": [
                    WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1),
                ],
            },
            synthesis="Done",
        )

        checkpoint = _coordinator_checkpoint(result, objective="implement feature")

        self.assertEqual(checkpoint["verification_status"], "pending")
        self.assertIn("implementation_declared_no_evidence", checkpoint["coordinator_semantic_errors"])

    def test_sectioned_implementation_evidence_stays_passed(self) -> None:
        from claw_v2.bot_helpers import _coordinator_checkpoint
        from claw_v2.coordinator import CoordinatorResult, WorkerResult

        result = CoordinatorResult(
            task_id="task-sectioned",
            phase_results={
                "implementation": [
                    WorkerResult(
                        task_name="implement_change",
                        content=(
                            "## Edits\napp.py: changed login path\n\n"
                            "## Build/Verify\ncmd: pytest tests/test_login.py\nresult: ok\n\n"
                            "## Evidence\nartifact_path: /tmp/pytest-login.txt"
                        ),
                        duration_seconds=0.1,
                    )
                ],
                "verification": [
                    WorkerResult(task_name="verify_change", content="Verification Status: passed", duration_seconds=0.1),
                ],
            },
            synthesis="Done",
        )

        checkpoint = _coordinator_checkpoint(result, objective="implement feature")

        self.assertEqual(checkpoint["verification_status"], "passed")
        structured = checkpoint["coordinator_result"]
        self.assertEqual(structured["changed_files"], ["app.py"])
        self.assertTrue(structured["verification"]["checks"])
        self.assertTrue(structured["evidence"])


if __name__ == "__main__":
    unittest.main()
