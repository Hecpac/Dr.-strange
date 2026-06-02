from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.evidence_ledger import load_claims
from claw_v2.verification_profiles import (
    PROFILES,
    ProfileVerificationDecision,
    VerificationProfile,
    get_profile,
    record_verification_coordinates,
    verify_profile_evidence,
    verification_coordinates_for,
)


class VerifyProfileEvidenceTests(unittest.TestCase):
    def test_nlm_create_passes_with_handler_evidence(self) -> None:
        decision = verify_profile_evidence(
            task_kind="notebooklm_create",
            evidence={"handler_result": "created", "notebook_title": "Research Notebook"},
        )
        self.assertEqual(decision.status, "passed")
        self.assertEqual(decision.reason, "profile_evidence_satisfied")
        self.assertEqual(decision.missing_evidence, [])
        self.assertEqual(decision.risk_level, "low")
        self.assertFalse(decision.requires_human_approval)

    def test_nlm_create_passes_with_notebook_id(self) -> None:
        decision = verify_profile_evidence(
            task_kind="notebooklm_create",
            evidence={"handler_result": "created", "notebook_id": "nb-123"},
        )
        self.assertEqual(decision.status, "passed")

    def test_nlm_create_pending_without_notebook_artifact(self) -> None:
        decision = verify_profile_evidence(
            task_kind="notebooklm_create",
            evidence={"handler_result": "created"},
        )
        self.assertEqual(decision.status, "pending")
        self.assertIn("notebook_id_or_title", decision.missing_evidence)

    def test_nlm_review_requires_review_summary(self) -> None:
        decision = verify_profile_evidence(
            task_kind="notebooklm_review",
            evidence={"handler_result": "ok", "notebook_title": "X"},
        )
        self.assertEqual(decision.status, "pending")
        self.assertIn("review_summary", decision.missing_evidence)

    def test_research_requires_sources_and_synthesis(self) -> None:
        decision = verify_profile_evidence(
            task_kind="research",
            evidence={"sources": ["https://example.com"]},
        )
        self.assertEqual(decision.status, "pending")
        self.assertIn("synthesis", decision.missing_evidence)

    def test_coding_inspection_does_not_require_test_output(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_inspection",
            evidence={"files_read": ["a.py"], "findings": "no issues"},
        )
        self.assertEqual(decision.status, "passed")

    def test_coding_patch_requires_diff_and_check(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_patch",
            evidence={"changed_files": ["a.py"]},
        )
        self.assertEqual(decision.status, "pending")
        self.assertIn("diff", decision.missing_evidence)
        self.assertIn("verification_check", decision.missing_evidence)

    def test_coding_patch_passes_with_diff_and_test_output(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_patch",
            evidence={"changed_files": ["a.py"], "diff": "..", "test_output": "5 passed"},
        )
        self.assertEqual(decision.status, "passed")
        self.assertIn("no_fudge_factors", {coord["dimension"] for coord in decision.coordinates})

    def test_coding_patch_blocks_unjustified_numeric_fudge_factor(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_patch",
            evidence={
                "changed_files": ["claw_v2/risk.py"],
                "diff": (
                    "diff --git a/claw_v2/risk.py b/claw_v2/risk.py\n"
                    "+++ b/claw_v2/risk.py\n"
                    "@@ -1,1 +1,1 @@\n"
                    "+score *= 1.37\n"
                ),
                "test_output": "5 passed",
            },
        )
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.reason, "no_fudge_factors")
        self.assertTrue(decision.requires_human_approval)
        self.assertIn("no_fudge_justification", decision.missing_evidence)

    def test_coding_patch_requires_randomized_or_parametrized_check_for_critical_logic(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_patch",
            evidence={
                "changed_files": ["claw_v2/scoring.py"],
                "diff": "diff --git a/claw_v2/scoring.py b/claw_v2/scoring.py\n+++ b/claw_v2/scoring.py\n+return value + 1",
                "test_output": "5 passed",
                "critical_logic": True,
            },
        )
        self.assertEqual(decision.status, "pending")
        self.assertEqual(decision.reason, "missing_verification_coordinates")
        self.assertIn("randomized_or_parametrized_check", decision.missing_evidence)

        parametrized = verify_profile_evidence(
            task_kind="coding_patch",
            evidence={
                "changed_files": ["claw_v2/scoring.py"],
                "diff": "diff --git a/claw_v2/scoring.py b/claw_v2/scoring.py\n+++ b/claw_v2/scoring.py\n+return value + 1",
                "test_output": "5 passed",
                "critical_logic": True,
                "parametrized_check": "pytest tests/test_scoring.py::test_boundaries -q",
            },
        )
        self.assertEqual(parametrized.status, "passed")

    def test_success_contract_requires_invariant_coordinate(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_bugfix",
            evidence={
                "changed_files": ["claw_v2/cache.py"],
                "diff": "diff --git a/claw_v2/cache.py b/claw_v2/cache.py\n+++ b/claw_v2/cache.py\n+return cached or value",
                "repro_check": "repro passed",
                "success_contract": {"invariant": "cache never returns stale writes"},
            },
        )
        self.assertEqual(decision.status, "pending")
        self.assertIn("success_contract_invariants", decision.missing_evidence)

        invariant = verify_profile_evidence(
            task_kind="coding_bugfix",
            evidence={
                "changed_files": ["claw_v2/cache.py"],
                "diff": "diff --git a/claw_v2/cache.py b/claw_v2/cache.py\n+++ b/claw_v2/cache.py\n+return cached or value",
                "repro_check": "repro passed",
                "success_contract": {"invariant": "cache never returns stale writes"},
                "invariant_check": "property check passed",
            },
        )
        self.assertEqual(invariant.status, "passed")

    def test_pipeline_merge_blocked_for_human_approval(self) -> None:
        decision = verify_profile_evidence(
            task_kind="pipeline_merge",
            evidence={"pr_url": "https://example.com/pr/1", "approval_id": "abc"},
        )
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.reason, "human_approval_required")
        self.assertTrue(decision.requires_human_approval)
        self.assertEqual(decision.risk_level, "high")

    def test_social_publish_blocked_critical(self) -> None:
        decision = verify_profile_evidence(
            task_kind="social_publish",
            evidence={"drafts_preview": [{"text": "hi"}], "approval_id": "abc"},
        )
        self.assertEqual(decision.status, "blocked")
        self.assertEqual(decision.risk_level, "critical")

    def test_unknown_task_kind_pending(self) -> None:
        decision = verify_profile_evidence(task_kind="unknown_kind", evidence={"x": "y"})
        self.assertEqual(decision.status, "pending")
        self.assertEqual(decision.reason, "unknown_task_kind")
        self.assertIn("task_kind_profile", decision.missing_evidence)

    def test_get_profile_returns_dataclass(self) -> None:
        profile = get_profile("notebooklm_create")
        self.assertIsInstance(profile, VerificationProfile)
        self.assertEqual(profile.task_kind, "notebooklm_create")

    def test_decision_is_dataclass(self) -> None:
        decision = verify_profile_evidence(task_kind="notebooklm_create", evidence={})
        self.assertIsInstance(decision, ProfileVerificationDecision)

    def test_all_critical_profiles_require_human_approval(self) -> None:
        for kind in ("social_publish", "pipeline_merge"):
            self.assertTrue(PROFILES[kind].human_approval_required, msg=kind)

    def test_verification_coordinates_can_be_recorded_as_separate_evidence_claims(self) -> None:
        decision = verify_profile_evidence(
            task_kind="coding_patch",
            evidence={
                "changed_files": ["a.py"],
                "diff": "diff --git a/a.py b/a.py\n+++ b/a.py\n+return value + 1",
                "test_output": "5 passed",
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            claims = record_verification_coordinates(
                root,
                goal_id="goal-1",
                decision=decision,
                source_ref="pytest",
            )

            loaded = load_claims(root)
            dimensions = [claim.claim_text for claim in loaded]
            self.assertEqual(len(claims), len(decision.coordinates))
            self.assertEqual(len(loaded), len(decision.coordinates))
            self.assertTrue(any("required_evidence" in text for text in dimensions))
            self.assertTrue(any("no_fudge_factors" in text for text in dimensions))

    def test_verification_coordinates_for_returns_each_required_coordinate(self) -> None:
        coordinates = verification_coordinates_for(
            task_kind="coding_patch",
            evidence={
                "changed_files": ["a.py"],
                "diff": "diff --git a/a.py b/a.py\n+++ b/a.py\n+return value + 1",
                "test_output": "5 passed",
            },
        )
        self.assertEqual(
            {coordinate["dimension"] for coordinate in coordinates},
            {"required_evidence", "fast_tests", "no_fudge_factors"},
        )


if __name__ == "__main__":
    unittest.main()
