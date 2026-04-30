from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.action_events import (
    ACTION_EVENT_SCHEMA_VERSION,
    ActionResult,
    ProposedAction,
    emit_event,
    load_events,
    recover_orphan_actions,
)
from claw_v2.evidence_ledger import load_claims
from claw_v2.goal_contract import create_goal, update_goal


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class ActionEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_emit_event_persists_schema_versioned_event(self) -> None:
        event = emit_event(
            self.root,
            event_type="action_executed",
            actor="claw",
            goal_id="g_1",
            goal_revision=2,
            session_id="tg-1",
            proposed_next_action=ProposedAction(
                tool="git_push",
                args_redacted={"remote": "origin", "telegram_bot_token": "secret-token-123456"},
                tier="tier_2_5",
                rationale_brief="publish authorized branch",
            ),
            risk_level="medium",
            claims=["c_1"],
            evidence_refs=["c_1"],
            result=ActionResult(status="success", output_hash="sha256:abc"),
        )

        self.assertEqual(event.schema_version, ACTION_EVENT_SCHEMA_VERSION)
        loaded = load_events(self.root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].goal_revision, 2)
        self.assertEqual(loaded[0].proposed_next_action.tier, "tier_2_5")  # type: ignore[union-attr]
        raw = (self.root / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("secret-token-123456", raw)

    def test_emit_event_uses_latest_goal_revision_when_omitted(self) -> None:
        goal = create_goal(self.root, objective="Revise me")
        update_goal(self.root, goal, constraints=["new constraint"])

        event = emit_event(
            self.root,
            event_type="action_proposed",
            actor="claw",
            goal_id=goal.goal_id,
            session_id="tg-1",
            proposed_next_action=ProposedAction(tool="ReadFile"),
        )

        self.assertEqual(event.goal_revision, 2)

    def test_recover_orphan_actions_marks_proposed_action_failed(self) -> None:
        goal = create_goal(self.root, objective="Recover orphan")
        proposed = emit_event(
            self.root,
            event_type="action_proposed",
            actor="claw",
            goal_id=goal.goal_id,
            goal_revision=goal.goal_revision,
            session_id="tg-1",
            proposed_next_action=ProposedAction(tool="DeployPreview", tier="tier_2"),
        )

        recovered = recover_orphan_actions(self.root)

        self.assertEqual(recovered, 1)
        events = load_events(self.root)
        failed = events[-1]
        self.assertEqual(failed.event_type, "action_failed")
        self.assertEqual(failed.originating_event_id, proposed.event_id)
        self.assertEqual(failed.result.error, "interrupted_by_restart")  # type: ignore[union-attr]
        claims = load_claims(self.root)
        self.assertEqual(claims[-1].claim_type, "risk_signal")
        self.assertEqual(claims[-1].verification_status, "unverified")

    def test_recover_orphan_actions_ignores_finalized_proposed_action(self) -> None:
        goal = create_goal(self.root, objective="Already finalized")
        proposed = emit_event(
            self.root,
            event_type="action_proposed",
            actor="claw",
            goal_id=goal.goal_id,
            goal_revision=goal.goal_revision,
            session_id="tg-1",
            proposed_next_action=ProposedAction(tool="RunTests", tier="tier_2"),
        )
        emit_event(
            self.root,
            event_type="action_executed",
            actor="claw",
            goal_id=goal.goal_id,
            goal_revision=goal.goal_revision,
            originating_event_id=proposed.event_id,
            session_id="tg-1",
            proposed_next_action=ProposedAction(tool="RunTests", tier="tier_2"),
            result=ActionResult(status="success", output_hash="sha256:ok"),
        )

        recovered = recover_orphan_actions(self.root)

        self.assertEqual(recovered, 0)
        self.assertNotIn("interrupted_by_restart", (self.root / "events.jsonl").read_text(encoding="utf-8"))

    def test_invalid_event_type_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "event_type"):
            emit_event(
                self.root,
                event_type="unknown",  # type: ignore[arg-type]
                actor="claw",
                goal_id="g_1",
                session_id="tg-1",
            )

    def test_observe_mirrors_typed_event(self) -> None:
        observe = FakeObserve()
        emit_event(
            self.root,
            event_type="risk_escalated",
            actor="claw",
            goal_id="g_1",
            session_id="tg-1",
            risk_level="high",
            observe=observe,
        )

        self.assertEqual(observe.events[0][0], "risk_escalated")
        self.assertEqual(observe.events[0][1]["schema_version"], ACTION_EVENT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
