from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.action_events import ActionEvent, ActionResult, ProposedAction
from claw_v2.evidence_ledger import Claim
from claw_v2.gdi import (
    GDI_SCHEMA_VERSION,
    GDISnapshot,
    calculate_gdi_snapshot,
    gate_gdi_action,
    load_gdi_snapshots,
    record_gdi_snapshot,
)
from claw_v2.goal_contract import GoalContract
from claw_v2.telemetry import now_iso


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class GDITests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.goal = GoalContract(
            goal_id="g_1",
            objective="Ship telemetry",
            allowed_actions=["git_status", "write_file"],
            disallowed_actions=["force_push"],
            constraints=["no force-push"],
            success_criteria=["pytest passes"],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_calculates_signals_against_goal_contract(self) -> None:
        snapshot = calculate_gdi_snapshot(
            self.goal,
            session_id="tg-1",
            proposed_next_action=ProposedAction(
                tool="force_push",
                tier="tier_3",
                rationale_brief="force push to prod",
            ),
        )

        self.assertEqual(snapshot.schema_version, GDI_SCHEMA_VERSION)
        self.assertGreaterEqual(snapshot.gdi_score, 0.5)
        self.assertIn(snapshot.band, {"critic_required", "stop"})
        self.assertIn("tool_in_disallowed", {signal.name for signal in snapshot.signals})

    def test_includes_consecutive_failure_and_claim_signals(self) -> None:
        events = [
            ActionEvent(
                event_id="e_1",
                event_type="action_failed",
                actor="claw",
                goal_id="g_1",
                session_id="tg-1",
                result=ActionResult(status="failure"),
            ),
            ActionEvent(
                event_id="e_2",
                event_type="action_failed",
                actor="claw",
                goal_id="g_1",
                session_id="tg-1",
                result=ActionResult(status="failure"),
            ),
        ]
        claims = [
            Claim(
                claim_id="c_1",
                goal_id="g_1",
                claim_text="Likely state is stale",
                claim_type="inference",
                verification_status="unverified",
            )
        ]

        snapshot = calculate_gdi_snapshot(self.goal, session_id="tg-1", recent_events=events, claims=claims)

        names = {signal.name for signal in snapshot.signals}
        self.assertIn("consecutive_failures", names)
        self.assertIn("unverified_claim_ratio", names)

    def test_workspace_escape_is_detected(self) -> None:
        snapshot = calculate_gdi_snapshot(
            self.goal,
            session_id="tg-1",
            workspace_root=self.root / "workspace",
            proposed_next_action=ProposedAction(
                tool="write_file",
                tier="tier_2",
                args_redacted={"path": "/etc/passwd"},
                rationale_brief="write outside workspace",
            ),
        )

        self.assertIn("workspace_escape", {signal.name for signal in snapshot.signals})

    def test_record_and_load_snapshot(self) -> None:
        observe = FakeObserve()
        snapshot = calculate_gdi_snapshot(self.goal, session_id="tg-1")
        record_gdi_snapshot(self.root, snapshot, observe=observe)

        loaded = load_gdi_snapshots(self.root)
        self.assertEqual(loaded[0].snapshot_id, snapshot.snapshot_id)
        self.assertEqual(observe.events[0][0], "gdi_snapshot")

    def test_log_only_gate_never_blocks(self) -> None:
        snapshot = GDISnapshot(
            snapshot_id="gdi_1",
            goal_id="g_1",
            session_id="tg-1",
            gdi_score=0.9,
            band="stop",
            computed_at=now_iso(),
        )

        decision = gate_gdi_action(snapshot, action_tier="tier_3", risk_level="critical", calibrated=False)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "log")

    def test_calibrated_gate_blocks_stop_band(self) -> None:
        snapshot = GDISnapshot(
            snapshot_id="gdi_1",
            goal_id="g_1",
            session_id="tg-1",
            gdi_score=0.9,
            band="stop",
            computed_at=now_iso(),
        )

        decision = gate_gdi_action(snapshot, action_tier="tier_3", risk_level="critical", calibrated=True)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.action, "block")


if __name__ == "__main__":
    unittest.main()

