from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.action_events import ProposedAction
from claw_v2.critic_protocol import (
    CRITIC_SCHEMA_VERSION,
    evaluate_critic_decision,
    load_critic_decisions,
    record_critic_decision,
)
from claw_v2.evidence_ledger import Claim, EvidenceRef
from claw_v2.gdi import GDISnapshot
from claw_v2.goal_contract import GoalContract


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class CriticProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.goal = GoalContract(
            goal_id="g_1",
            objective="Ship telemetry",
            allowed_actions=["write_file", "git_push"],
            disallowed_actions=["force_push"],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_approves_aligned_action_with_verified_evidence(self) -> None:
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(tool="write_file", tier="tier_2", rationale_brief="edit telemetry"),
            evidence_ledger_subset=[
                Claim(
                    claim_id="c_1",
                    goal_id="g_1",
                    claim_text="Tests passed",
                    claim_type="fact",
                    evidence_refs=[EvidenceRef(kind="tool_call", ref="pytest -q")],
                    verification_status="verified",
                )
            ],
            risk_level="medium",
        )

        self.assertEqual(decision.schema_version, CRITIC_SCHEMA_VERSION)
        self.assertEqual(decision.decision, "approve")

    def test_blocks_disallowed_action(self) -> None:
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(tool="force_push", tier="tier_3", rationale_brief="force push"),
            risk_level="critical",
        )

        self.assertEqual(decision.decision, "block")

    def test_revises_tier_two_with_evidence_gap(self) -> None:
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(tool="write_file", tier="tier_2", rationale_brief="edit"),
            evidence_ledger_subset=[
                Claim(
                    claim_id="c_gap",
                    goal_id="g_1",
                    claim_text="Build passed",
                    claim_type="fact",
                    verification_status="unverified",
                )
            ],
            risk_level="medium",
        )

        self.assertEqual(decision.decision, "revise")
        self.assertEqual(decision.evidence_gaps, ["c_gap:unverified"])

    def test_asks_human_for_tier_three_evidence_gap(self) -> None:
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(tool="git_push", tier="tier_3", rationale_brief="publish"),
            evidence_ledger_subset=[
                Claim(
                    claim_id="c_gap",
                    goal_id="g_1",
                    claim_text="Branch is safe",
                    claim_type="fact",
                    verification_status="unverified",
                )
            ],
            risk_level="critical",
        )

        self.assertEqual(decision.decision, "ask_human")

    def test_tier_two_point_five_requires_guards(self) -> None:
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(
                tool="git_push",
                tier="tier_2_5",
                args_redacted={"branch": "main"},
                rationale_brief="push",
            ),
            risk_level="medium",
        )

        self.assertEqual(decision.decision, "revise")
        self.assertTrue(any("protected branch" in item for item in decision.required_fix))

    def test_gdi_stop_blocks(self) -> None:
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(tool="write_file", tier="tier_2", rationale_brief="edit"),
            gdi_snapshot=GDISnapshot(
                snapshot_id="gdi_1",
                goal_id="g_1",
                session_id="tg-1",
                gdi_score=0.9,
                band="stop",
            ),
            risk_level="high",
        )

        self.assertEqual(decision.decision, "block")

    def test_record_and_load_decision(self) -> None:
        observe = FakeObserve()
        decision = evaluate_critic_decision(
            self.goal,
            proposed_next_action=ProposedAction(tool="write_file", tier="tier_2", rationale_brief="edit"),
        )
        record_critic_decision(self.root, decision, observe=observe)

        loaded = load_critic_decisions(self.root)
        self.assertEqual(loaded[0].decision_id, decision.decision_id)
        self.assertEqual(observe.events[0][0], "critic_decision_received")


if __name__ == "__main__":
    unittest.main()

