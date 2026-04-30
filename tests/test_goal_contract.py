from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from claw_v2.goal_contract import GOAL_SCHEMA_VERSION, create_goal, load_goals, update_goal


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class GoalContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_create_goal_persists_schema_versioned_contract(self) -> None:
        goal = create_goal(
            self.root,
            objective="Ship P0 telemetry",
            allowed_actions=["write_file"],
            success_criteria=["pytest passes"],
            risk_profile="tier_2",
            anchor_source="task_id:123",
        )

        self.assertEqual(goal.schema_version, GOAL_SCHEMA_VERSION)
        self.assertTrue(goal.goal_id.startswith("g_"))
        loaded = load_goals(self.root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].objective, "Ship P0 telemetry")
        self.assertEqual(loaded[0].allowed_actions, ["write_file"])

    def test_parent_goal_id_none_serializes_as_json_null(self) -> None:
        create_goal(self.root, objective="Root goal", parent_goal_id=None)

        raw = (self.root / "goals.jsonl").read_text(encoding="utf-8").strip()
        self.assertIsNone(json.loads(raw)["parent_goal_id"])

    def test_update_goal_appends_new_version_with_same_goal_id(self) -> None:
        goal = create_goal(self.root, objective="Old objective")
        updated = update_goal(self.root, goal, objective="New objective", constraints=["stay local"])

        loaded = load_goals(self.root)
        self.assertEqual([item.goal_id for item in loaded], [goal.goal_id, goal.goal_id])
        self.assertEqual(updated.objective, "New objective")
        self.assertEqual(loaded[-1].constraints, ["stay local"])

    def test_invalid_risk_profile_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "risk_profile"):
            create_goal(self.root, objective="bad", risk_profile="tier_4")  # type: ignore[arg-type]

    def test_observe_receives_goal_events(self) -> None:
        observe = FakeObserve()
        goal = create_goal(self.root, objective="Observable", observe=observe)
        update_goal(self.root, goal, objective="Still observable", observe=observe)

        self.assertEqual([event[0] for event in observe.events], ["goal_initialized", "goal_updated"])


if __name__ == "__main__":
    unittest.main()
