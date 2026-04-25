from __future__ import annotations

import sqlite3
import tempfile
import threading
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

    def test_enqueue_resume_key_creates_new_job_after_terminal_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            first = service.enqueue(kind="notebooklm.research", resume_key="nlm:nb1")
            service.complete(first.job_id)

            second = service.enqueue(kind="notebooklm.research", resume_key="nlm:nb1")

            self.assertNotEqual(second.job_id, first.job_id)
            self.assertEqual(second.status, "queued")
            self.assertEqual(service.summary(), {"completed": 1, "queued": 1})

    def test_schema_migrates_global_resume_key_unique_constraint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE agent_jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'waiting_approval', 'retrying', 'completed', 'failed', 'cancelled')),
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    resume_key TEXT UNIQUE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    worker_id TEXT,
                    next_run_at REAL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL
                );
                INSERT INTO agent_jobs (
                    job_id, kind, status, payload_json, checkpoint_json, result_json,
                    metadata_json, error, resume_key, attempts, max_attempts, worker_id,
                    next_run_at, created_at, started_at, completed_at, updated_at
                )
                VALUES (
                    'job:old', 'notebooklm.research', 'completed', '{}', '{}', '{}',
                    '{}', '', 'nlm:nb1', 1, 3, 'worker-1',
                    1, 1, 1, 2, 2
                );
                """
            )
            conn.commit()
            conn.close()

            service = JobService(db_path)
            second = service.enqueue(kind="notebooklm.research", resume_key="nlm:nb1")

            self.assertNotEqual(second.job_id, "job:old")
            self.assertEqual(second.status, "queued")
            self.assertEqual(service.summary(), {"completed": 1, "queued": 1})

    def test_claim_specific_job_marks_it_running_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            service = JobService(db_path)
            other = JobService(db_path)
            created = service.enqueue(kind="notebooklm.research")

            claimed = service.claim(created.job_id, worker_id="worker-1")
            claimed_again = other.claim(created.job_id, worker_id="worker-2")

            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.worker_id, "worker-1")
            self.assertIsNone(claimed_again)
            self.assertEqual(service.get(created.job_id).worker_id, "worker-1")

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

    def test_claim_next_is_transactional_across_service_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            seed = JobService(db_path)
            seed.enqueue(kind="pipeline.issue")
            services = [JobService(db_path), JobService(db_path)]
            barrier = threading.Barrier(len(services))
            lock = threading.Lock()
            results = []

            def slow_select(statement: str) -> None:
                if "FROM agent_jobs" in statement and "LIMIT 1" in statement:
                    time.sleep(0.05)

            def worker(index: int, service: JobService) -> None:
                barrier.wait()
                claimed = service.claim_next(worker_id=f"worker-{index}")
                with lock:
                    results.append(claimed)

            for service in services:
                service._conn.set_trace_callback(slow_select)

            threads = [
                threading.Thread(target=worker, args=(index, service))
                for index, service in enumerate(services)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            claimed = [record for record in results if record is not None]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(seed.summary(), {"running": 1})

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

    def test_wait_for_approval_records_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="pipeline.issue")

            waiting = service.wait_for_approval(created.job_id, checkpoint={"approval_id": "ap-1"})

            self.assertEqual(waiting.status, "waiting_approval")
            self.assertEqual(waiting.checkpoint, {"approval_id": "ap-1"})
            self.assertEqual([job.job_id for job in service.resume_candidates()], [created.job_id])

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
