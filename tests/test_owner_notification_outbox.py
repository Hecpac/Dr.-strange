from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.daemon import (
    OWNER_NOTIFICATION_JOB_KIND,
    OWNER_NOTIFICATION_MAX_ATTEMPTS,
    OWNER_NOTIFICATION_RESUME_PREFIX,
    OwnerNotificationDrainRunner,
)
from claw_v2.jobs import JobService
from claw_v2.lifecycle import enqueue_owner_notification


class _FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict | None = None, **kwargs) -> None:
        self.events.append((event_type, payload or {}))

    def event_types(self) -> list[str]:
        return [event_type for event_type, _ in self.events]


class OwnerNotificationEnqueueTests(unittest.TestCase):
    """Slice 1b (blind-spot pass 2026-07-06 finding #6): a failed Telegram send
    of a terminal-task notification must leave a durable owner_notification job
    instead of a warning-only drop."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.jobs = JobService(Path(self._tmp.name) / "claw.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_failed_send_enqueues_durable_notification(self) -> None:
        queued = enqueue_owner_notification(
            self.jobs,
            session_id="tg-574707975",
            message="Task done",
            notification_key="task-1#attempt-0",
        )

        self.assertTrue(queued)
        job = self.jobs.claim_next(worker_id="t", kinds=(OWNER_NOTIFICATION_JOB_KIND,))
        assert job is not None
        self.assertEqual(job.kind, OWNER_NOTIFICATION_JOB_KIND)
        self.assertEqual(job.payload["chat_id"], "574707975")
        self.assertEqual(job.payload["message"], "Task done")
        self.assertEqual(job.payload["notification_key"], "task-1#attempt-0")
        self.assertEqual(job.resume_key, f"{OWNER_NOTIFICATION_RESUME_PREFIX}task-1#attempt-0")
        self.assertEqual(job.max_attempts, OWNER_NOTIFICATION_MAX_ATTEMPTS)

    def test_enqueue_dedups_active_window_by_notification_key(self) -> None:
        self.assertTrue(
            enqueue_owner_notification(
                self.jobs, session_id="tg-1", message="m1", notification_key="k#attempt-0"
            )
        )
        self.assertTrue(
            enqueue_owner_notification(
                self.jobs, session_id="tg-1", message="m1", notification_key="k#attempt-0"
            )
        )

        first = self.jobs.claim_next(worker_id="t", kinds=(OWNER_NOTIFICATION_JOB_KIND,))
        second = self.jobs.claim_next(worker_id="t", kinds=(OWNER_NOTIFICATION_JOB_KIND,))
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_enqueue_refuses_non_telegram_or_incomplete_input(self) -> None:
        self.assertFalse(
            enqueue_owner_notification(
                self.jobs, session_id="web-1", message="m", notification_key="k"
            )
        )
        self.assertFalse(
            enqueue_owner_notification(
                self.jobs, session_id="tg-abc", message="m", notification_key="k"
            )
        )
        self.assertFalse(
            enqueue_owner_notification(
                self.jobs, session_id="tg-1", message="", notification_key="k"
            )
        )
        self.assertFalse(
            enqueue_owner_notification(
                self.jobs, session_id="tg-1", message="m", notification_key=None
            )
        )
        self.assertFalse(
            enqueue_owner_notification(None, session_id="tg-1", message="m", notification_key="k")
        )
        self.assertIsNone(self.jobs.claim_next(worker_id="t", kinds=(OWNER_NOTIFICATION_JOB_KIND,)))


class OwnerNotificationDrainRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.jobs = JobService(Path(self._tmp.name) / "claw.db")
        self.observe = _FakeObserve()
        self.sent: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _runner(self, *, send=None, **kwargs) -> OwnerNotificationDrainRunner:
        return OwnerNotificationDrainRunner(
            job_service=self.jobs,
            send=send or (lambda chat_id, message: self.sent.append((chat_id, message))),
            observe=self.observe,
            sleep=lambda seconds: None,
            **kwargs,
        )

    def _enqueue(self, key: str = "task-1#attempt-0", **payload_overrides) -> str:
        payload = {
            "session_id": "tg-574707975",
            "chat_id": "574707975",
            "message": "Task done",
            "notification_key": key,
        }
        payload.update(payload_overrides)
        record = self.jobs.enqueue(
            kind=OWNER_NOTIFICATION_JOB_KIND,
            payload=payload,
            resume_key=f"{OWNER_NOTIFICATION_RESUME_PREFIX}{key}",
            max_attempts=OWNER_NOTIFICATION_MAX_ATTEMPTS,
        )
        return record.job_id

    def test_delivers_and_completes(self) -> None:
        job_id = self._enqueue()

        delivered = self._runner().run_once()

        self.assertEqual(delivered, 1)
        self.assertEqual(self.sent, [("574707975", "Task done")])
        job = self.jobs.get(job_id)
        assert job is not None
        self.assertEqual(job.status, "completed")
        self.assertIn("owner_notification_delivered", self.observe.event_types())

    def test_send_failure_marks_retrying_with_backoff(self) -> None:
        job_id = self._enqueue()

        def _boom(chat_id: str, message: str) -> None:
            raise RuntimeError("telegram down")

        runner = self._runner(send=_boom)
        self.assertEqual(runner.run_once(), 0)

        job = self.jobs.get(job_id)
        assert job is not None
        self.assertEqual(job.status, "retrying")
        self.assertGreater(job.next_run_at or 0, time.time())
        # Backoff: a second cycle before next_run_at claims nothing.
        self.assertEqual(runner.run_once(), 0)
        self.assertEqual(self.jobs.get(job_id).status, "retrying")

    def test_stale_notification_terminalizes_with_event_and_no_send(self) -> None:
        job_id = self._enqueue()

        delivered = self._runner(stale_after_seconds=0.0).run_once()

        self.assertEqual(delivered, 0)
        self.assertEqual(self.sent, [])
        job = self.jobs.get(job_id)
        assert job is not None
        self.assertEqual(job.status, "failed")
        self.assertIn("stale_notification", job.error or "")
        self.assertIn("owner_notification_expired", self.observe.event_types())

    def test_invalid_payload_terminalizes(self) -> None:
        record = self.jobs.enqueue(kind=OWNER_NOTIFICATION_JOB_KIND, payload={})

        delivered = self._runner().run_once()

        self.assertEqual(delivered, 0)
        job = self.jobs.get(record.job_id)
        assert job is not None
        self.assertEqual(job.status, "failed")
        self.assertIn("invalid_notification_payload", job.error or "")

    def test_exhausted_attempts_terminalize(self) -> None:
        record = self.jobs.enqueue(
            kind=OWNER_NOTIFICATION_JOB_KIND,
            payload={
                "session_id": "tg-1",
                "chat_id": "1",
                "message": "m",
                "notification_key": "k",
            },
            max_attempts=1,
        )

        def _boom(chat_id: str, message: str) -> None:
            raise RuntimeError("telegram down")

        self._runner(send=_boom).run_once()

        job = self.jobs.get(record.job_id)
        assert job is not None
        self.assertEqual(job.status, "failed")

    def test_max_per_cycle_bounds_work(self) -> None:
        for index in range(3):
            self._enqueue(key=f"task-{index}#attempt-0")

        delivered = self._runner(max_per_cycle=2).run_once()

        self.assertEqual(delivered, 2)
        self.assertEqual(len(self.sent), 2)


if __name__ == "__main__":
    unittest.main()
