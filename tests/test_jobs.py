from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claw_v2.jobs import JOB_TERMINAL_STATUSES, JobService
from claw_v2.observe import ObserveStream


class _ClosedOnceConn:
    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.failures = 1

    def execute(self, *args, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class JobServiceTests(unittest.TestCase):
    def test_enqueue_is_idempotent_for_active_resume_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")

            first = service.enqueue(
                kind="notebooklm.research", payload={"notebook_id": "nb1"}, resume_key="nlm:nb1"
            )
            second = service.enqueue(
                kind="notebooklm.research", payload={"notebook_id": "nb1"}, resume_key="nlm:nb1"
            )

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

            claimed = service.claim_next(
                worker_id="worker-1", kinds=["pipeline.issue"], now=time.time()
            )
            self.assertEqual(claimed.job_id, created.job_id)
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.attempts, 1)

            checkpointed = service.checkpoint(created.job_id, {"phase": "tests"})
            self.assertEqual(checkpointed.checkpoint, {"phase": "tests"})

            completed = service.complete(created.job_id, result={"pr": "https://example.com/pr/1"})
            self.assertEqual(completed.status, "completed")
            self.assertEqual(completed.result["pr"], "https://example.com/pr/1")
            self.assertIsNotNone(completed.completed_at)

    def test_list_recovers_once_after_closed_database_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="pipeline.issue")
            service._conn = _ClosedOnceConn(service._conn)

            rows = service.list(statuses=("queued",), kinds=("pipeline.issue",))

            self.assertEqual([row.job_id for row in rows], [created.job_id])

    def test_reschedule_keeps_pending_poller_active_without_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="notebooklm.orchestrate")

            claimed = service.claim_next(worker_id="notebooklm", now=time.time())
            self.assertEqual(claimed.status, "running")

            next_run_at = time.time() + 30
            rescheduled = service.reschedule(
                created.job_id,
                checkpoint={"stage": "outputs_generating"},
                result={"last_status": "pending"},
                next_run_at=next_run_at,
            )

            self.assertEqual(rescheduled.status, "retrying")
            self.assertEqual(rescheduled.error, "")
            self.assertEqual(rescheduled.attempts, 1)
            self.assertEqual(rescheduled.checkpoint["stage"], "outputs_generating")
            self.assertEqual(rescheduled.result["last_status"], "pending")
            self.assertGreaterEqual(rescheduled.next_run_at, next_run_at)

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

    def test_update_does_not_resurrect_terminal_job_across_service_instances(self) -> None:
        # Defense-in-depth twin of the claim_next transactionality guard:
        # _update must not let a second JobService connection flip a job
        # terminal between its SELECT and its UPDATE. `completer` reads the
        # running row, a sibling instance commits `failed` into the read->write
        # window, and the late completion must not overwrite (resurrect) the
        # terminal status. Without BEGIN IMMEDIATE, completer's UPDATE clobbers
        # the committed `failed` with `completed` (a torn write).
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            seed = JobService(db_path)
            created = seed.enqueue(kind="pipeline.issue")
            seed.claim_next(worker_id="worker-seed")  # -> running

            completer = JobService(db_path)
            failer = JobService(db_path)
            barrier = threading.Barrier(2)
            results = {}

            def widen_completer_write_window(statement: str) -> None:
                # Fires as each completer statement begins. The SELECT has
                # already read `running` by the time the UPDATE starts; hold
                # that window open so `failer` can commit `failed` into it.
                if statement.strip().upper().startswith("UPDATE AGENT_JOBS"):
                    time.sleep(0.2)

            completer._conn.set_trace_callback(widen_completer_write_window)

            def run_complete() -> None:
                barrier.wait()
                results["complete"] = completer.complete(created.job_id, result={"ok": True})

            def run_fail() -> None:
                barrier.wait()
                time.sleep(0.01)  # let completer's SELECT read `running` first
                results["fail"] = failer.fail(created.job_id, error="boom", retry=False)

            threads = [threading.Thread(target=run_complete), threading.Thread(target=run_fail)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            persisted = seed.get(created.job_id).status
            # No torn write: every caller's returned status must match the row
            # that actually persisted.
            self.assertIn(persisted, JOB_TERMINAL_STATUSES)
            self.assertEqual(results["complete"].status, persisted)
            self.assertEqual(results["fail"].status, persisted)

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

    def test_complete_does_not_resurrect_terminal_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            rec = service.enqueue(kind="demo")
            service.fail(rec.job_id, error="boom", retry=False)
            self.assertEqual(service.get(rec.job_id).status, "failed")
            out = service.complete(rec.job_id, result={"ok": True})  # must not resurrect
            self.assertIsNotNone(out)
            self.assertEqual(out.status, "failed")
            self.assertEqual(service.get(rec.job_id).status, "failed")

    def test_fail_does_not_resurrect_completed_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            rec = service.enqueue(kind="demo")
            service.complete(rec.job_id, result={"ok": True})
            out = service.fail(rec.job_id, error="late")  # must not resurrect, must not deadlock
            self.assertIsNotNone(out)
            self.assertEqual(out.status, "completed")

    def test_recover_stale_running_job_retries_below_attempt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(Path(tmpdir) / "claw.db", observe=observe)
            now = time.time()
            created = service.enqueue(kind="notebooklm.research", max_attempts=3)
            claimed = service.claim(created.job_id, worker_id="notebooklm", now=now)
            self.assertEqual(claimed.status, "running")

            recovered = service.recover_stale_running(
                kinds=("notebooklm.research",),
                stale_after_seconds=60,
                now=now + 120,
            )

            self.assertEqual(len(recovered), 1)
            job = service.get(created.job_id)
            self.assertEqual(job.status, "retrying")
            self.assertEqual(job.error, "stale_running_timeout")
            self.assertEqual(job.attempts, 1)
            self.assertEqual(
                job.checkpoint["stale_running_recovery"]["previous_worker_id"],
                "notebooklm",
            )
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_retrying", events)
            self.assertIn("stale_running_job_recovered", events)

    def test_recover_stale_running_job_fails_at_attempt_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(Path(tmpdir) / "claw.db", observe=observe)
            now = time.time()
            created = service.enqueue(kind="coordinator.autonomous_task", max_attempts=1)
            service.claim(created.job_id, worker_id="coordinator", now=now)

            recovered = service.recover_stale_running(
                kinds=("coordinator.autonomous_task",),
                stale_after_seconds=60,
                now=now + 120,
            )

            self.assertEqual(len(recovered), 1)
            job = service.get(created.job_id)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error, "stale_running_timeout")
            self.assertIsNotNone(job.completed_at)
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_failed", events)
            self.assertIn("stale_running_job_recovered", events)

    def test_recover_stale_running_job_leaves_recent_running_job_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            now = time.time()
            created = service.enqueue(kind="notebooklm.research", max_attempts=3)
            service.claim(created.job_id, worker_id="notebooklm", now=now)

            recovered = service.recover_stale_running(
                kinds=("notebooklm.research",),
                stale_after_seconds=60,
                now=now + 10,
            )

            self.assertEqual(recovered, [])
            self.assertEqual(service.get(created.job_id).status, "running")


if __name__ == "__main__":
    unittest.main()
