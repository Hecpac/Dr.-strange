from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.jobs import JobService
from claw_v2.observe import ObserveStream


class JobServiceTests(unittest.TestCase):
    def test_enqueue_is_idempotent_for_active_resume_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")

            first = service.enqueue(kind="notebooklm.research", payload={"notebook_id": "nb1"}, resume_key="nlm:nb1")
            second = service.enqueue(kind="notebooklm.research", payload={"notebook_id": "nb1"}, resume_key="nlm:nb1")

            self.assertEqual(second.job_id, first.job_id)
            self.assertEqual(service.summary(), {"queued": 1})

    def test_enqueue_resume_key_stays_idempotent_after_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            first = service.enqueue(kind="notebooklm.research", resume_key="nlm:nb1")
            service.complete(first.job_id)

            second = service.enqueue(kind="notebooklm.research", resume_key="nlm:nb1")

            self.assertEqual(second.job_id, first.job_id)
            self.assertEqual(second.status, "completed")

    def test_claim_checkpoint_complete_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="pipeline.issue", payload={"issue_id": "HEC-1"})

            claimed = service.claim_next(worker_id="worker-1", kinds=["pipeline.issue"], now=time.time())
            self.assertEqual(claimed.job_id, created.job_id)
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.attempts, 1)

            checkpointed = service.checkpoint(created.job_id, {"phase": "tests"})
            self.assertEqual(checkpointed.checkpoint, {"phase": "tests"})

            completed = service.complete(created.job_id, result={"pr": "https://example.com/pr/1"})
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.result["pr"], "https://example.com/pr/1")
            self.assertIsNotNone(completed.completed_at)

    def test_fail_retries_until_attempt_budget_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="pipeline.issue", max_attempts=2)

            service.claim_next(worker_id="worker-1", now=time.time())
            retrying = service.fail(created.job_id, error="tests failed", retry_delay_seconds=5)
            self.assertEqual(retrying.status, "retrying")
            self.assertEqual(retrying.error, "tests failed")
            self.assertGreater(retrying.next_run_at, time.time())

            service.claim_next(worker_id="worker-1", now=time.time() + 10)
            failed = service.fail(created.job_id, error="tests failed again", retry_delay_seconds=5)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.error, "tests failed again")

    def test_cancel_marks_active_job_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="notebooklm.podcast")

            cancelled = service.cancel(created.job_id, reason="user requested")

            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.error, "user requested")
            self.assertEqual(service.resume_candidates(), [])

    def test_emits_observe_events_with_job_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(Path(tmpdir) / "claw.db", observe=observe)

            created = service.enqueue(kind="pipeline.issue")
            service.claim_next(worker_id="worker-1")
            service.complete(created.job_id)

            events = observe.job_events(created.job_id)
            self.assertEqual(
                [event["event_type"] for event in events],
                ["job_enqueued", "job_claimed", "job_completed"],
            )
            self.assertTrue(all(event["job_id"] == created.job_id for event in events))


if __name__ == "__main__":
    unittest.main()
