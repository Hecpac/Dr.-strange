from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.evidence_ledger import Claim, EvidenceRef
from claw_v2.far import FAR_SCHEMA_VERSION, assess_far, load_far_assessments, record_far_assessment


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class FaRTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_verified_tool_evidence_has_high_confidence(self) -> None:
        assessment = assess_far(
            goal_id="g_1",
            claims=[
                Claim(
                    claim_id="c_1",
                    goal_id="g_1",
                    claim_text="Tests passed",
                    claim_type="fact",
                    evidence_refs=[EvidenceRef(kind="tool_call", ref="pytest -q")],
                    verification_status="verified",
                )
            ],
        )

        self.assertEqual(assessment.schema_version, FAR_SCHEMA_VERSION)
        self.assertGreater(assessment.confidence, 0.7)
        self.assertEqual(assessment.recommended_decision, "continue")

    def test_fact_without_tool_evidence_sets_doubt_flag(self) -> None:
        assessment = assess_far(
            goal_id="g_1",
            claims=[
                Claim(
                    claim_id="c_1",
                    goal_id="g_1",
                    claim_text="It worked",
                    claim_type="fact",
                    verification_status="unverified",
                )
            ],
        )

        self.assertIn("missing_tool_evidence", {flag.flag for flag in assessment.doubt_flags})
        self.assertEqual(assessment.recommended_decision, "revise")

    def test_contradicted_claim_blocks(self) -> None:
        assessment = assess_far(
            goal_id="g_1",
            claims=[
                Claim(
                    claim_id="c_1",
                    goal_id="g_1",
                    claim_text="Build passed",
                    claim_type="fact",
                    verification_status="contradicted",
                )
            ],
        )

        self.assertEqual(assessment.recommended_decision, "block")

    def test_tier_three_requires_user_confirmation(self) -> None:
        assessment = assess_far(
            goal_id="g_1",
            claims=[],
            action_tier="tier_3",
            external_state_verified=True,
            user_confirmation_present=False,
        )

        self.assertEqual(assessment.recommended_decision, "ask_human")
        self.assertIn("user_confirmation_needed", {flag.flag for flag in assessment.doubt_flags})

    def test_record_emits_assessment_and_risk_escalation_for_critical_flag(self) -> None:
        observe = FakeObserve()
        assessment = assess_far(goal_id="g_1", claims=[], action_tier="tier_3")
        record_far_assessment(self.root, assessment, observe=observe)

        loaded = load_far_assessments(self.root)
        self.assertEqual(loaded[0].assessment_id, assessment.assessment_id)
        self.assertEqual([event[0] for event in observe.events], ["far_assessment", "risk_escalated"])


if __name__ == "__main__":
    unittest.main()

