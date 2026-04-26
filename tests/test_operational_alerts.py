from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream
from claw_v2.operational_alerts import OperationalAlertRouter


class OperationalAlertRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.observe = ObserveStream(Path(self._tmp.name) / "observe.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_forwards_actionable_event_to_notify(self) -> None:
        notifications: list[str] = []
        router = OperationalAlertRouter(observe=self.observe, notify=notifications.append)
        router.install()

        self.observe.emit(
            "firecrawl_paused",
            payload={"reason": "insufficient_credits", "paused_seconds": 86400},
        )

        self.assertEqual(len(notifications), 1)
        self.assertIn("Firecrawl paused", notifications[0])
        self.assertIn("insufficient_credits", notifications[0])
        events = self.observe.recent_events(limit=2)
        self.assertTrue(any(event["event_type"] == "operational_alert_sent" for event in events))

    def test_suppresses_events_already_reported_to_user(self) -> None:
        notifications: list[str] = []
        router = OperationalAlertRouter(observe=self.observe, notify=notifications.append)
        router.install()

        self.observe.emit(
            "nlm_research_degraded",
            payload={"reason": "no_results", "notebook_id": "nb1", "user_notified": True},
        )

        self.assertEqual(notifications, [])
        events = self.observe.recent_events(limit=2)
        suppressed = [event for event in events if event["event_type"] == "operational_alert_suppressed"]
        self.assertEqual(suppressed[0]["payload"]["reason"], "user_notified")

    def test_dedupes_repeated_alerts_within_cooldown(self) -> None:
        now = [100.0]
        notifications: list[str] = []
        router = OperationalAlertRouter(
            observe=self.observe,
            notify=notifications.append,
            clock=lambda: now[0],
        )
        router.install()

        payload = {"job": "wiki_research", "error": "boom"}
        self.observe.emit("scheduled_job_error", payload=payload)
        self.observe.emit("scheduled_job_error", payload=payload)

        self.assertEqual(len(notifications), 1)
        events = self.observe.recent_events(limit=3)
        self.assertTrue(any(event["event_type"] == "operational_alert_suppressed" for event in events))

    def test_alerts_when_llm_provider_circuit_opens(self) -> None:
        notifications: list[str] = []
        router = OperationalAlertRouter(observe=self.observe, notify=notifications.append)
        router.install()

        self.observe.emit(
            "llm_circuit_open",
            provider="anthropic",
            model="claude",
            payload={"provider": "anthropic", "failures": 3, "reason": "timeout"},
        )

        self.assertEqual(len(notifications), 1)
        self.assertIn("LLM provider circuit opened", notifications[0])
        self.assertIn("anthropic", notifications[0])

    def test_alerts_when_auto_research_pauses_after_provider_failure(self) -> None:
        notifications: list[str] = []
        router = OperationalAlertRouter(observe=self.observe, notify=notifications.append)
        router.install()

        self.observe.emit(
            "auto_research_adapter_error",
            payload={
                "agent": "perf-optimizer",
                "reason": "codex_timeout",
                "consecutive_failures": 1,
                "error": "Codex CLI timed out after 120.0s",
            },
        )

        self.assertEqual(len(notifications), 1)
        self.assertIn("Auto-research provider failure", notifications[0])
        self.assertIn("perf-optimizer", notifications[0])
        self.assertIn("codex_timeout", notifications[0])

    def test_alerts_when_pipeline_poll_degrades(self) -> None:
        notifications: list[str] = []
        router = OperationalAlertRouter(observe=self.observe, notify=notifications.append)
        router.install()

        self.observe.emit(
            "pipeline_poll_degraded",
            payload={
                "poller": "pipeline_poll",
                "reason": "timeout",
                "consecutive_failures": 1,
                "backoff_seconds": 300.0,
                "error": "The read operation timed out",
            },
        )

        self.assertEqual(len(notifications), 1)
        self.assertIn("Pipeline poll degraded", notifications[0])
        self.assertIn("pipeline_poll", notifications[0])
        self.assertIn("timeout", notifications[0])


if __name__ == "__main__":
    unittest.main()
