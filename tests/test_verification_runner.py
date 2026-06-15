"""Tests for the Petri verifier swap point (commit #8)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.verification import (
    DimensionRawResponse,
    PETRI_VERIFIER_ENV_FLAG,
    petri_verifier_enabled,
    record_target_event,
    run_petri_judge_for_task,
    should_use_petri_verifier,
)


_REPO_DIMENSIONS = Path(__file__).resolve().parents[1] / "claw_v2" / "verification" / "dimensions"


class PetriEnvFlagTests(unittest.TestCase):
    def test_default_disabled_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PETRI_VERIFIER_ENV_FLAG, None)
            self.assertFalse(petri_verifier_enabled())

    def test_enabled_when_set_to_one(self) -> None:
        self.assertTrue(petri_verifier_enabled(env={PETRI_VERIFIER_ENV_FLAG: "1"}))

    def test_enabled_when_set_to_true_yes_on(self) -> None:
        for val in ("true", "TRUE", "yes", "Yes", "on", "ON"):
            self.assertTrue(
                petri_verifier_enabled(env={PETRI_VERIFIER_ENV_FLAG: val}),
                f"expected enabled for {val!r}",
            )

    def test_disabled_for_zero_or_empty_or_garbage(self) -> None:
        for val in ("0", "", "no", "off", "false", "maybe"):
            self.assertFalse(
                petri_verifier_enabled(env={PETRI_VERIFIER_ENV_FLAG: val}),
                f"expected disabled for {val!r}",
            )


class ShouldUsePetriVerifierTests(unittest.TestCase):
    def test_off_when_flag_disabled(self) -> None:
        env = {PETRI_VERIFIER_ENV_FLAG: "0"}
        self.assertFalse(should_use_petri_verifier({"verify": "strict"}, env=env))

    def test_off_when_metadata_missing_or_none(self) -> None:
        env = {PETRI_VERIFIER_ENV_FLAG: "1"}
        self.assertFalse(should_use_petri_verifier(None, env=env))
        self.assertFalse(should_use_petri_verifier({}, env=env))

    def test_off_for_routine_tasks_even_when_flag_enabled(self) -> None:
        env = {PETRI_VERIFIER_ENV_FLAG: "1"}
        self.assertFalse(should_use_petri_verifier({"verify": "routine"}, env=env))
        self.assertFalse(should_use_petri_verifier({"verify": ""}, env=env))

    def test_on_when_flag_enabled_and_metadata_strict(self) -> None:
        env = {PETRI_VERIFIER_ENV_FLAG: "1"}
        self.assertTrue(should_use_petri_verifier({"verify": "strict"}, env=env))
        self.assertTrue(should_use_petri_verifier({"verify": "STRICT"}, env=env))


class PetriRunOrchestratorTests(unittest.TestCase):
    def test_runs_judge_against_target_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record_target_event(
                root,
                task_id="t-run",
                event_type="agent_message",
                payload={"text": "completed task and pushed commit a1b2c3"},
            )
            calls: list[str] = []

            def judge_fn(prompt: str) -> DimensionRawResponse:
                calls.append(prompt)
                return DimensionRawResponse(score=1, reason="clean")

            outcome = run_petri_judge_for_task(
                task_id="t-run",
                telemetry_root=root,
                dimensions_root=_REPO_DIMENSIONS,
                judge_fn=judge_fn,
            )
            self.assertEqual(outcome.report.overall_status, "passed")
            self.assertEqual(outcome.transcript_records, 1)
            self.assertIn("state_amnesia", outcome.dimensions_used)
            self.assertIn("verification_drift", outcome.dimensions_used)
            self.assertEqual(len(calls), len(outcome.dimensions_used))

    def test_raises_when_target_stream_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(ValueError):
                run_petri_judge_for_task(
                    task_id="ghost",
                    telemetry_root=root,
                    dimensions_root=_REPO_DIMENSIONS,
                    judge_fn=lambda prompt: DimensionRawResponse(score=1),
                )

    def test_judge_does_not_see_harness_stream(self) -> None:
        """Hard requirement: harness records must never reach the judge."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record_target_event(
                root,
                task_id="t-iso",
                event_type="agent_message",
                payload={"text": "user-facing"},
            )
            from claw_v2.verification import record_harness_event

            record_harness_event(
                root,
                task_id="t-iso",
                event_type="verifier_internal_call",
                payload={"text": "INTERNAL_TOOL_TRACE_DO_NOT_SHOW"},
            )

            seen_prompts: list[str] = []

            def judge_fn(prompt: str) -> DimensionRawResponse:
                seen_prompts.append(prompt)
                return DimensionRawResponse(score=1, reason="ok")

            run_petri_judge_for_task(
                task_id="t-iso",
                telemetry_root=root,
                dimensions_root=_REPO_DIMENSIONS,
                judge_fn=judge_fn,
            )
            for prompt in seen_prompts:
                self.assertNotIn("INTERNAL_TOOL_TRACE_DO_NOT_SHOW", prompt)
                self.assertNotIn("verifier_internal_call", prompt)


if __name__ == "__main__":
    unittest.main()
