from __future__ import annotations

import unittest
from typing import Any

from claw_v2.mission_controller import MissionController, MissionRecord


class _FakeStateStore:
    """Mimics MemoryStore.get/update_session_state for tests."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}

    def get(self, session_id: str) -> dict[str, Any]:
        return dict(self._states.get(session_id, {}))

    def update(self, session_id: str, **kwargs: Any) -> None:
        existing = self._states.setdefault(session_id, {})
        existing.update(kwargs)


class MissionControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._store = _FakeStateStore()
        self._clock_now = 1000.0
        self.mc = MissionController(
            get_session_state=self._store.get,
            update_session_state=self._store.update,
            clock=lambda: self._clock_now,
        )

    def test_start_mission_persists_active_mission(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="dame ai news de hoy",
            task_kind="ai_news_brief",
            route="skill",
        )
        self.assertEqual(m.task_kind, "ai_news_brief")
        self.assertEqual(m.status, "planning")
        # Persisted under active_object["_mission"]
        state = self._store.get("s1")
        self.assertIn("active_object", state)
        self.assertIn("_mission", state["active_object"])
        self.assertEqual(
            state["active_object"]["_mission"]["mission_id"], m.mission_id
        )

    def test_does_not_pollute_notebook_active_object_keys(self) -> None:
        # Pre-existing notebook in active_object
        self._store.update(
            "s1",
            active_object={"kind": "notebook", "id": "nb-1", "title": "Z"},
        )
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="research",
            task_kind="research",
            route="local",
        )
        active = self._store.get("s1")["active_object"]
        self.assertEqual(active["kind"], "notebook")
        self.assertEqual(active["id"], "nb-1")
        self.assertEqual(active["title"], "Z")
        self.assertEqual(active["_mission"]["mission_id"], m.mission_id)

    def test_latest_relevant_returns_active_not_terminal(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="x",
            task_kind="ai_news_brief",
            route="skill",
        )
        latest = self.mc.latest_relevant("s1")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.mission_id, m.mission_id)
        # Mark complete with no required evidence — succeeds.
        self.mc.complete_if_verified(m.mission_id, session_id="s1")
        latest = self.mc.latest_relevant("s1")
        self.assertIsNone(latest)

    def test_continue_resumes_active_mission_before_new_task(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="x",
            task_kind="ai_news_brief",
            route="skill",
        )
        self.mc.mark_interrupted(m.mission_id, session_id="s1", reason="restart")
        # Resume same task_kind keeps the mission_id.
        resumed = self.mc.start_or_resume(
            session_id="s1",
            objective="continúa",
            task_kind="ai_news_brief",
            route="skill",
        )
        self.assertEqual(resumed.mission_id, m.mission_id)
        self.assertEqual(resumed.status, "executing")

    def test_blocked_mission_records_reason(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="x",
            task_kind="x_trends",
            route="cdp",
        )
        self.mc.mark_blocked(
            m.mission_id, session_id="s1", reason="cdp_unavailable"
        )
        latest = self.mc.latest_relevant("s1")
        # blocked is non-terminal, still reachable
        self.assertIsNotNone(latest)
        self.assertEqual(latest.status, "blocked")
        self.assertEqual(latest.blocked_reason, "cdp_unavailable")

    def test_mission_does_not_auto_complete_without_evidence(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="x",
            task_kind="ai_news_brief",
            route="skill",
            evidence_required=["sources", "claim_map", "fetched_at"],
        )
        result = self.mc.complete_if_verified(m.mission_id, session_id="s1")
        self.assertIsNotNone(result)
        self.assertNotEqual(result.status, "succeeded")
        # After collecting evidence, completes.
        self.mc.record_evidence(
            m.mission_id,
            session_id="s1",
            evidence={"sources": [], "claim_map": {}, "fetched_at": "x"},
        )
        result = self.mc.complete_if_verified(m.mission_id, session_id="s1")
        self.assertEqual(result.status, "succeeded")

    def test_record_evidence_advances_phase(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="x",
            task_kind="ai_news_brief",
            route="skill",
        )
        updated = self.mc.record_evidence(
            m.mission_id, session_id="s1", evidence={"sources": []}
        )
        self.assertEqual(updated.status, "collecting_evidence")
        self.assertEqual(updated.phase, "collecting_evidence")

    def test_failed_mission_terminal(self) -> None:
        m = self.mc.start_or_resume(
            session_id="s1",
            objective="x",
            task_kind="research",
            route="local",
        )
        self.mc.fail(m.mission_id, session_id="s1", reason="hit_budget_limit")
        self.assertIsNone(self.mc.latest_relevant("s1"))


if __name__ == "__main__":
    unittest.main()
