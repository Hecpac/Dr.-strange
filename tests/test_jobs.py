from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.jobs import JOB_TERMINAL_STATUSES, FormalLeaseRequiredError, JobService
from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import RuntimeDb


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


def _set_job_columns(service: JobService, job_id: str, **columns) -> None:
    assignments = []
    values = []
    for column, value in columns.items():
        assignments.append(f"{column} = ?")
        if column.endswith("_json"):
            values.append(json.dumps(value, sort_keys=True))
        else:
            values.append(value)
    with service._lock:
        service._conn.execute(
            f"UPDATE agent_jobs SET {', '.join(assignments)} WHERE job_id = ?",
            (*values, job_id),
        )
        service._conn.commit()


class JobServiceTests(unittest.TestCase):
    def test_prune_terminal_deletes_only_old_terminal_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(Path(tmpdir) / "claw.db", observe=observe)
            now = 1_000_000.0
            old = now - (31 * 86400)
            recent = now - (10 * 86400)

            completed = service.enqueue(kind="retention.completed")
            failed = service.enqueue(kind="retention.failed")
            cancelled = service.enqueue(kind="retention.cancelled")
            recent_completed = service.enqueue(kind="retention.recent")
            queued = service.enqueue(kind="retention.queued")
            running = service.enqueue(kind="retention.running")
            waiting = service.enqueue(kind="retention.waiting")
            retrying = service.enqueue(kind="retention.retrying")

            service.complete(completed.job_id)
            service.fail(failed.job_id, error="old failure", retry=False)
            service.cancel(cancelled.job_id)
            service.complete(recent_completed.job_id)
            service.claim(running.job_id, worker_id="worker-1", now=old)
            service.wait_for_approval(waiting.job_id)
            service.fail(retrying.job_id, error="retry later", retry=True)

            old_terminal_ids = (completed.job_id, failed.job_id, cancelled.job_id)
            active_ids = (queued.job_id, running.job_id, waiting.job_id, retrying.job_id)
            with service._lock:
                service._conn.execute(
                    "UPDATE agent_jobs SET completed_at = ?, updated_at = ? "
                    f"WHERE job_id IN ({', '.join('?' for _ in old_terminal_ids)})",
                    (old, old, *old_terminal_ids),
                )
                service._conn.execute(
                    "UPDATE agent_jobs SET updated_at = ? "
                    f"WHERE job_id IN ({', '.join('?' for _ in active_ids)})",
                    (old, *active_ids),
                )
                service._conn.execute(
                    "UPDATE agent_jobs SET completed_at = ?, updated_at = ? WHERE job_id = ?",
                    (recent, recent, recent_completed.job_id),
                )
                service._conn.commit()

            deleted = service.prune_terminal(retention_days=30, max_rows=10, now=now)

            self.assertEqual(deleted, 3)
            for job_id in old_terminal_ids:
                self.assertIsNone(service.get(job_id))
            for job_id in (*active_ids, recent_completed.job_id):
                self.assertIsNotNone(service.get(job_id))
            events = [event["event_type"] for event in observe.recent_events(limit=10)]
            self.assertIn("agent_jobs_pruned", events)

    def test_prune_terminal_is_bounded_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            now = 1_000_000.0
            old = now - (31 * 86400)
            job_ids: list[str] = []
            for index in range(3):
                record = service.enqueue(kind=f"retention.completed.{index}")
                service.complete(record.job_id)
                job_ids.append(record.job_id)
            with service._lock:
                service._conn.execute(
                    "UPDATE agent_jobs SET completed_at = ?, updated_at = ? "
                    f"WHERE job_id IN ({', '.join('?' for _ in job_ids)})",
                    (old, old, *job_ids),
                )
                service._conn.commit()

            self.assertEqual(service.prune_terminal(retention_days=30, max_rows=2, now=now), 2)
            self.assertEqual(service.summary(), {"completed": 1})
            self.assertEqual(service.prune_terminal(retention_days=30, max_rows=2, now=now), 1)
            self.assertEqual(service.summary(), {})

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
            self.assertEqual(second.lease_generation, 0)
            self.assertIsNone(second.lease_owner)
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

    def test_formal_claim_next_acquires_exclusive_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            seed = JobService(
                db_path,
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = seed.enqueue(kind="pipeline.issue")
            first = JobService(
                db_path,
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            second = JobService(
                db_path,
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            start = float(created.next_run_at or time.time())

            claimed = first.claim_next(worker_id="worker-1", now=start)
            claimed_again = second.claim_next(worker_id="worker-2", now=start + 1)

            self.assertIsNotNone(claimed)
            self.assertIsNone(claimed_again)
            persisted = seed.get(created.job_id)
            self.assertEqual(persisted.status, "running")
            self.assertEqual(persisted.worker_id, "worker-1")
            self.assertEqual(persisted.lease_owner, "worker-1")
            self.assertEqual(persisted.lease_heartbeat_at, start)
            self.assertEqual(persisted.lease_expires_at, start + 30)
            self.assertEqual(persisted.lease_generation, 1)
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_lease_acquired", events)
            self.assertIn("job_claimed", events)

    def test_formal_lease_heartbeat_extends_only_current_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue")
            start = float(created.next_run_at or time.time())
            claimed = service.claim_next(worker_id="worker-1", now=start)
            assert claimed is not None

            wrong_owner = service.heartbeat_lease(
                created.job_id,
                worker_id="worker-2",
                lease_generation=claimed.lease_generation,
                now=start + 10,
            )
            missing_generation = service.heartbeat_lease(
                created.job_id,
                worker_id="worker-1",
                now=start + 10,
            )
            extended = service.heartbeat_lease(
                created.job_id,
                worker_id="worker-1",
                lease_generation=claimed.lease_generation,
                now=start + 20,
            )
            expired = service.heartbeat_lease(
                created.job_id,
                worker_id="worker-1",
                lease_generation=claimed.lease_generation,
                now=start + 51,
            )

            self.assertIsNone(wrong_owner)
            self.assertIsNone(missing_generation)
            self.assertIsNotNone(extended)
            self.assertEqual(extended.lease_heartbeat_at, start + 20)
            self.assertEqual(extended.lease_expires_at, start + 50)
            self.assertIsNone(expired)

    def test_release_lease_returns_job_to_retrying_for_another_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue")
            start = float(created.next_run_at or time.time())
            claimed = service.claim_next(worker_id="worker-1", now=start)
            assert claimed is not None

            released = service.release_lease(
                created.job_id,
                worker_id="worker-1",
                lease_generation=claimed.lease_generation,
                now=start + 10,
            )
            reclaimed = service.claim_next(worker_id="worker-2", now=start + 11)

            self.assertEqual(released.status, "retrying")
            self.assertIsNone(released.lease_owner)
            self.assertIsNone(released.lease_expires_at)
            self.assertEqual(reclaimed.job_id, created.job_id)
            self.assertEqual(reclaimed.lease_owner, "worker-2")
            self.assertEqual(reclaimed.lease_generation, 2)

    def test_reclaim_expired_leases_waits_until_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(
                Path(tmpdir) / "claw.db",
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="coordinator.autonomous_task", max_attempts=3)
            start = float(created.next_run_at or time.time())
            service.claim_next(worker_id="coordinator", now=start)

            early = service.recover_stale_running(
                kinds=("coordinator.autonomous_task",),
                stale_after_seconds=1,
                now=start + 20,
            )
            expired = service.recover_stale_running(
                kinds=("coordinator.autonomous_task",),
                stale_after_seconds=1,
                now=start + 31,
            )

            self.assertEqual(early, [])
            self.assertEqual(len(expired), 1)
            job = service.get(created.job_id)
            self.assertEqual(job.status, "retrying")
            self.assertIsNone(job.lease_owner)
            self.assertIsNone(job.lease_expires_at)
            self.assertEqual(job.checkpoint["lease_reclaim"]["lease_owner"], "coordinator")
            self.assertGreaterEqual(
                job.checkpoint["lease_reclaim"]["lease_expired_by_seconds"], 1.0
            )
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_retrying", events)
            self.assertIn("stale_running_job_recovered", events)

    def test_late_heartbeat_after_reclaim_and_new_claim_does_not_extend_new_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue", max_attempts=3)
            start = float(created.next_run_at or time.time())
            first = service.claim_next(worker_id="worker-1", now=start)
            assert first is not None
            service.reclaim_expired_leases(now=start + 31)
            second = service.claim_next(worker_id="worker-2", now=start + 32)
            assert second is not None

            stale = service.heartbeat_lease(
                created.job_id,
                worker_id="worker-1",
                lease_generation=first.lease_generation,
                now=start + 40,
            )

            self.assertIsNone(stale)
            persisted = service.get(created.job_id)
            self.assertEqual(persisted.status, "running")
            self.assertEqual(persisted.lease_owner, "worker-2")
            self.assertEqual(persisted.lease_generation, second.lease_generation)
            self.assertEqual(persisted.lease_heartbeat_at, start + 32)
            self.assertEqual(persisted.lease_expires_at, start + 62)

    def test_late_release_after_reclaim_and_new_claim_does_not_free_new_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue", max_attempts=3)
            start = float(created.next_run_at or time.time())
            first = service.claim_next(worker_id="worker-1", now=start)
            assert first is not None
            service.reclaim_expired_leases(now=start + 31)
            second = service.claim_next(worker_id="worker-2", now=start + 32)
            assert second is not None

            stale = service.release_lease(
                created.job_id,
                worker_id="worker-1",
                lease_generation=first.lease_generation,
                now=start + 40,
            )

            self.assertIsNone(stale)
            persisted = service.get(created.job_id)
            self.assertEqual(persisted.status, "running")
            self.assertEqual(persisted.lease_owner, "worker-2")
            self.assertEqual(persisted.lease_generation, second.lease_generation)

    def test_late_complete_after_reclaim_and_new_claim_does_not_terminalize_new_lease(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue", max_attempts=3)
            start = float(created.next_run_at or time.time())
            first = service.claim_next(worker_id="worker-1", now=start)
            assert first is not None
            service.reclaim_expired_leases(now=start + 31)
            second = service.claim_next(worker_id="worker-2", now=start + 32)
            assert second is not None

            stale = service.complete(
                created.job_id,
                result={"late": True},
                lease_owner="worker-1",
                lease_generation=first.lease_generation,
            )

            self.assertIsNone(stale)
            persisted = service.get(created.job_id)
            self.assertEqual(persisted.status, "running")
            self.assertEqual(persisted.lease_owner, "worker-2")
            self.assertEqual(persisted.lease_generation, second.lease_generation)

    def test_reused_worker_id_requires_current_lease_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue", max_attempts=3)
            start = float(created.next_run_at or time.time())
            first = service.claim_next(worker_id="worker", now=start)
            assert first is not None
            service.reclaim_expired_leases(now=start + 31)
            second = service.claim_next(worker_id="worker", now=start + 32)
            assert second is not None

            stale_heartbeat = service.heartbeat_lease(
                created.job_id,
                worker_id="worker",
                lease_generation=first.lease_generation,
                now=start + 33,
            )
            stale_release = service.release_lease(
                created.job_id,
                worker_id="worker",
                lease_generation=first.lease_generation,
                now=start + 34,
            )
            current_heartbeat = service.heartbeat_lease(
                created.job_id,
                worker_id="worker",
                lease_generation=second.lease_generation,
                now=start + 35,
            )
            current_release = service.release_lease(
                created.job_id,
                worker_id="worker",
                lease_generation=second.lease_generation,
                now=start + 36,
            )

            self.assertIsNone(stale_heartbeat)
            self.assertIsNone(stale_release)
            self.assertIsNotNone(current_heartbeat)
            self.assertEqual(current_heartbeat.lease_expires_at, start + 65)
            self.assertIsNotNone(current_release)
            self.assertEqual(current_release.status, "retrying")
            self.assertIsNone(current_release.lease_owner)

    def test_formal_lifecycle_mutations_without_lease_generation_do_not_mutate(
        self,
    ) -> None:
        for operation in ("checkpoint", "wait", "complete", "fail", "reschedule"):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as tmpdir:
                    service = JobService(
                        Path(tmpdir) / "claw.db",
                        formal_leases_enabled=True,
                        default_lease_seconds=30,
                    )
                    created = service.enqueue(kind="pipeline.issue", max_attempts=3)
                    start = float(created.next_run_at or time.time())
                    claimed = service.claim_next(worker_id="worker-1", now=start)
                    assert claimed is not None

                    if operation == "checkpoint":
                        result = service.checkpoint(
                            created.job_id,
                            {"phase": "bad"},
                            lease_owner="worker-1",
                        )
                    elif operation == "wait":
                        result = service.wait_for_approval(
                            created.job_id,
                            checkpoint={"approval": "bad"},
                            lease_owner="worker-1",
                        )
                    elif operation == "complete":
                        result = service.complete(
                            created.job_id,
                            result={"done": True},
                            lease_owner="worker-1",
                        )
                    elif operation == "fail":
                        result = service.fail(
                            created.job_id,
                            error="boom",
                            lease_owner="worker-1",
                        )
                    else:
                        result = service.reschedule(
                            created.job_id,
                            checkpoint={"stage": "bad"},
                            lease_owner="worker-1",
                        )

                    self.assertIsNone(result)
                    persisted = service.get(created.job_id)
                    self.assertEqual(persisted.status, "running")
                    self.assertEqual(persisted.lease_owner, "worker-1")
                    self.assertEqual(persisted.lease_generation, claimed.lease_generation)
                    self.assertEqual(persisted.checkpoint, {})
                    self.assertEqual(persisted.result, {})
                    self.assertEqual(persisted.error, "")

    def test_formal_lifecycle_mutations_with_wrong_generation_do_not_mutate(
        self,
    ) -> None:
        for operation in ("checkpoint", "wait", "complete", "fail", "reschedule"):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as tmpdir:
                    service = JobService(
                        Path(tmpdir) / "claw.db",
                        formal_leases_enabled=True,
                        default_lease_seconds=30,
                    )
                    created = service.enqueue(kind="pipeline.issue", max_attempts=3)
                    start = float(created.next_run_at or time.time())
                    claimed = service.claim_next(worker_id="worker-1", now=start)
                    assert claimed is not None
                    wrong_generation = claimed.lease_generation + 1

                    if operation == "checkpoint":
                        result = service.checkpoint(
                            created.job_id,
                            {"phase": "bad"},
                            lease_owner="worker-1",
                            lease_generation=wrong_generation,
                        )
                    elif operation == "wait":
                        result = service.wait_for_approval(
                            created.job_id,
                            checkpoint={"approval": "bad"},
                            lease_owner="worker-1",
                            lease_generation=wrong_generation,
                        )
                    elif operation == "complete":
                        result = service.complete(
                            created.job_id,
                            result={"done": True},
                            lease_owner="worker-1",
                            lease_generation=wrong_generation,
                        )
                    elif operation == "fail":
                        result = service.fail(
                            created.job_id,
                            error="boom",
                            lease_owner="worker-1",
                            lease_generation=wrong_generation,
                        )
                    else:
                        result = service.reschedule(
                            created.job_id,
                            checkpoint={"stage": "bad"},
                            lease_owner="worker-1",
                            lease_generation=wrong_generation,
                        )

                    self.assertIsNone(result)
                    persisted = service.get(created.job_id)
                    self.assertEqual(persisted.status, "running")
                    self.assertEqual(persisted.lease_owner, "worker-1")
                    self.assertEqual(persisted.lease_generation, claimed.lease_generation)
                    self.assertEqual(persisted.checkpoint, {})
                    self.assertEqual(persisted.result, {})
                    self.assertEqual(persisted.error, "")

    def test_formal_lifecycle_mutations_with_current_generation_mutate(self) -> None:
        for operation in ("checkpoint", "wait", "complete", "fail", "reschedule"):
            with self.subTest(operation=operation):
                with tempfile.TemporaryDirectory() as tmpdir:
                    service = JobService(
                        Path(tmpdir) / "claw.db",
                        formal_leases_enabled=True,
                        default_lease_seconds=30,
                    )
                    created = service.enqueue(kind="pipeline.issue", max_attempts=3)
                    start = float(created.next_run_at or time.time())
                    claimed = service.claim_next(worker_id="worker-1", now=start)
                    assert claimed is not None
                    token = {
                        "lease_owner": "worker-1",
                        "lease_generation": claimed.lease_generation,
                    }

                    if operation == "checkpoint":
                        result = service.checkpoint(
                            created.job_id,
                            {"phase": "ok"},
                            **token,
                        )
                        self.assertEqual(result.status, "running")
                        self.assertEqual(result.checkpoint, {"phase": "ok"})
                        self.assertEqual(result.lease_owner, "worker-1")
                    elif operation == "wait":
                        result = service.wait_for_approval(
                            created.job_id,
                            checkpoint={"approval": "pending"},
                            **token,
                        )
                        self.assertEqual(result.status, "waiting_approval")
                        self.assertIsNone(result.lease_owner)
                    elif operation == "complete":
                        result = service.complete(
                            created.job_id,
                            result={"done": True},
                            **token,
                        )
                        self.assertEqual(result.status, "completed")
                        self.assertEqual(result.result, {"done": True})
                        self.assertIsNone(result.lease_owner)
                    elif operation == "fail":
                        result = service.fail(
                            created.job_id,
                            error="boom",
                            **token,
                        )
                        self.assertEqual(result.status, "retrying")
                        self.assertEqual(result.error, "boom")
                        self.assertIsNone(result.lease_owner)
                    else:
                        result = service.reschedule(
                            created.job_id,
                            checkpoint={"stage": "pending"},
                            result={"last_status": "pending"},
                            **token,
                        )
                        self.assertEqual(result.status, "retrying")
                        self.assertEqual(result.checkpoint, {"stage": "pending"})
                        self.assertEqual(result.result, {"last_status": "pending"})
                        self.assertIsNone(result.lease_owner)

    def test_formal_leases_work_with_shared_runtime_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = RuntimeDb(Path(tmpdir) / "claw.db")
            self.addCleanup(db.close)
            first = JobService(
                db.db_path,
                runtime_db=db,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            second = JobService(
                db.db_path,
                runtime_db=db,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = first.enqueue(kind="pipeline.issue")
            start = float(created.next_run_at or time.time())

            claimed = first.claim_next(worker_id="worker-1", now=start)
            claimed_again = second.claim_next(worker_id="worker-2", now=start + 1)
            assert claimed is not None
            heartbeat = second.heartbeat_lease(
                created.job_id,
                worker_id="worker-1",
                lease_generation=claimed.lease_generation,
                now=start + 10,
            )

            self.assertIsNone(claimed_again)
            self.assertIsNotNone(heartbeat)
            self.assertEqual(heartbeat.lease_heartbeat_at, start + 10)
            self.assertEqual(first.get(created.job_id).lease_owner, "worker-1")

    def test_formal_leases_late_complete_fail_on_terminal_returns_terminal_record(self) -> None:
        # Issue #153: under formal_leases_enabled a terminal job has no lease
        # (cleared on terminalization), so the lease-match guard used to return
        # None for a late complete()/fail() retry instead of the terminal record.
        # The terminal-idempotency check now runs before the lease-match check.
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue", max_attempts=3)
            start = float(created.next_run_at or time.time())
            claimed = service.claim_next(worker_id="worker-1", now=start)
            assert claimed is not None
            token = {
                "lease_owner": "worker-1",
                "lease_generation": claimed.lease_generation,
            }
            first_complete = service.complete(created.job_id, result={"done": True}, **token)
            self.assertEqual(first_complete.status, "completed")

            # Late retry with the SAME (now-stale) lease credentials must return
            # the terminal record, not None.
            late_complete = service.complete(
                created.job_id, result={"done": "again"}, **token
            )
            self.assertIsNotNone(late_complete)
            self.assertEqual(late_complete.status, "completed")
            self.assertEqual(late_complete.result, {"done": True})

            # A late fail() on the terminal job also returns the terminal record.
            late_fail = service.fail(created.job_id, error="boom", **token)
            self.assertIsNotNone(late_fail)
            self.assertEqual(late_fail.status, "completed")
            self.assertEqual(late_fail.error, "")

    def test_formal_claim_returns_none_when_running_update_loses_cas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(
                Path(tmpdir) / "claw.db",
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue")
            start = float(created.next_run_at or time.time())
            original = service._update_row_to_running_with_lease

            def lose_cas(row, *, worker_id, now, lease_seconds):
                service._conn.execute(
                    """
                    UPDATE agent_jobs
                    SET status = 'running',
                        worker_id = 'other-worker',
                        lease_owner = 'other-worker',
                        lease_expires_at = ?,
                        lease_heartbeat_at = ?,
                        lease_generation = COALESCE(lease_generation, 0) + 1,
                        attempts = COALESCE(attempts, 0) + 1,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now + 30, now, now, row["job_id"]),
                )
                return original(
                    row,
                    worker_id=worker_id,
                    now=now,
                    lease_seconds=lease_seconds,
                )

            service._update_row_to_running_with_lease = lose_cas  # type: ignore[method-assign]

            claimed = service.claim(
                created.job_id,
                worker_id="worker-1",
                now=start,
                lease_seconds=30,
            )

            self.assertIsNone(claimed)
            persisted = service.get(created.job_id)
            self.assertEqual(persisted.status, "running")
            self.assertEqual(persisted.lease_owner, "other-worker")
            self.assertEqual(persisted.lease_generation, 1)
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertNotIn("job_lease_acquired", events)
            self.assertNotIn("job_claimed", events)

    def test_claims_allowed_when_maintenance_flags_absent(self) -> None:
        with patch.dict(
            os.environ,
            {"CLAW_MAINTENANCE_MODE": "0", "CLAW_NO_JOB_CLAIM": "0"},
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                service = JobService(Path(tmpdir) / "claw.db")
                specific = service.enqueue(kind="notebooklm.research")
                next_job = service.enqueue(kind="pipeline.issue")

                claimed_specific = service.claim(specific.job_id, worker_id="worker-1")
                claimed_next = service.claim_next(
                    worker_id="worker-2",
                    kinds=("pipeline.issue",),
                )

                self.assertIsNotNone(claimed_specific)
                self.assertIsNotNone(claimed_next)
                assert claimed_specific is not None
                assert claimed_next is not None
                self.assertEqual(claimed_specific.status, "running")
                self.assertEqual(claimed_next.job_id, next_job.job_id)
                self.assertEqual(claimed_next.status, "running")

    def test_claims_blocked_by_maintenance_mode_before_running_transition(self) -> None:
        self._assert_claim_gate_blocks_transitions(
            flag_name="CLAW_MAINTENANCE_MODE",
            expected_reason="maintenance_mode_active",
        )

    def test_claims_blocked_by_no_job_claim_before_running_transition(self) -> None:
        self._assert_claim_gate_blocks_transitions(
            flag_name="CLAW_NO_JOB_CLAIM",
            expected_reason="no_job_claim_active",
        )

    def _assert_claim_gate_blocks_transitions(
        self,
        *,
        flag_name: str,
        expected_reason: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            observe = ObserveStream(root / "observe.db")
            service = JobService(root / "claw.db", observe=observe)
            queued = service.enqueue(kind="notebooklm.research")
            retrying = service.enqueue(kind="pipeline.issue")
            service.claim(retrying.job_id, worker_id="setup-worker")
            service.fail(
                retrying.job_id,
                error="setup_retry",
                retry=True,
                retry_delay_seconds=0,
            )

            with patch.dict(
                os.environ,
                {
                    "CLAW_MAINTENANCE_MODE": "0",
                    "CLAW_NO_JOB_CLAIM": "0",
                    flag_name: "1",
                },
                clear=False,
            ):
                specific_claim = service.claim(queued.job_id, worker_id="worker-1")
                next_claim = service.claim_next(
                    worker_id="worker-2",
                    kinds=("pipeline.issue",),
                    now=time.time() + 1,
                )

            self.assertIsNone(specific_claim)
            self.assertIsNone(next_claim)
            self.assertEqual(service.get(queued.job_id).status, "queued")
            self.assertEqual(service.get(retrying.job_id).status, "retrying")
            events = [
                event
                for event in observe.recent_events(limit=20)
                if event["event_type"] == "job_claim_blocked"
            ]
            self.assertEqual(len(events), 2)
            payloads = [event["payload"] for event in events]
            self.assertEqual(
                {payload["operation"] for payload in payloads},
                {"claim", "claim_next"},
            )
            self.assertEqual({payload["reason"] for payload in payloads}, {expected_reason})
            self.assertNotIn("setup_retry", str(payloads))

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

    def test_cancel_formal_leases_requires_formal_cancel_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue")
            claimed = service.claim_next(worker_id="worker-1", now=time.time())
            assert claimed is not None

            with self.assertRaises(FormalLeaseRequiredError) as caught:
                service.cancel(created.job_id, reason="user requested")

            self.assertTrue(getattr(caught.exception, "formal_leases_enabled", False))

    def test_cancel_formal_leases_does_not_terminalize_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(
                kind="pipeline.issue",
                metadata={"owner": "runner"},
            )
            claimed = service.claim_next(worker_id="worker-1", now=time.time())
            assert claimed is not None
            checkpointed = service.checkpoint(
                created.job_id,
                {"step": "started"},
                lease_owner=claimed.lease_owner,
                lease_generation=claimed.lease_generation,
            )
            assert checkpointed is not None

            try:
                service.cancel(created.job_id, reason="user requested")
            except Exception:
                pass

            persisted = service.get(created.job_id)
            assert persisted is not None
            self.assertEqual(persisted.status, "running")
            self.assertEqual(persisted.metadata, {"owner": "runner"})
            self.assertEqual(persisted.checkpoint, {"step": "started"})
            self.assertEqual(persisted.lease_owner, claimed.lease_owner)
            self.assertEqual(persisted.lease_generation, claimed.lease_generation)

    def test_request_cancel_records_intent_without_terminalizing_running_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(
                Path(tmpdir) / "claw.db",
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(
                kind="pipeline.issue",
                metadata={"owner": "runner", "nested": {"kept": True}},
            )
            start = float(created.next_run_at or time.time())
            claimed = service.claim_next(worker_id="worker-1", now=start)
            assert claimed is not None
            checkpointed = service.checkpoint(
                created.job_id,
                {"step": "started"},
                lease_owner=claimed.lease_owner,
                lease_generation=claimed.lease_generation,
            )
            assert checkpointed is not None
            _set_job_columns(service, created.job_id, result_json={"partial": "ok"})

            requested = service.request_cancel(
                created.job_id,
                actor="operator@example.com",
                reason="user requested stop",
                now=start + 5,
            )

            self.assertIsNotNone(requested)
            assert requested is not None
            self.assertEqual(requested.status, "running")
            self.assertEqual(requested.checkpoint, {"step": "started"})
            self.assertEqual(requested.result, {"partial": "ok"})
            self.assertEqual(requested.lease_owner, "worker-1")
            self.assertEqual(requested.lease_generation, claimed.lease_generation)
            self.assertEqual(requested.lease_expires_at, start + 30)
            self.assertEqual(
                requested.metadata,
                {
                    "owner": "runner",
                    "nested": {"kept": True},
                    "cancel_request": {
                        "requested_at": start + 5,
                        "requested_by": "operator@example.com",
                        "reason": "user requested stop",
                    },
                },
            )
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_cancel_requested", events)

    def test_request_cancel_preserves_queued_status_and_requires_actor_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db", formal_leases_enabled=True)
            queued = service.enqueue(kind="pipeline.issue", metadata={"owner": "queue"})

            requested = service.request_cancel(
                queued.job_id,
                actor="operator@example.com",
                reason="stop before start",
                now=123.0,
            )

            self.assertEqual(requested.status, "queued")
            self.assertEqual(requested.metadata["owner"], "queue")
            self.assertEqual(requested.metadata["cancel_request"]["requested_at"], 123.0)
            self.assertIsNone(requested.lease_owner)
            self.assertEqual(requested.lease_generation, 0)

            for kwargs in (
                {"actor": "", "reason": "stop"},
                {"actor": "operator@example.com", "reason": ""},
            ):
                with self.subTest(kwargs=kwargs):
                    another = service.enqueue(kind="pipeline.issue")
                    with self.assertRaises(ValueError):
                        service.request_cancel(another.job_id, **kwargs)
                    self.assertNotIn("cancel_request", service.get(another.job_id).metadata)

    def test_request_cancel_terminal_job_does_not_resurrect_or_mutate_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db", formal_leases_enabled=True)
            created = service.enqueue(kind="pipeline.issue", metadata={"done": True})
            _set_job_columns(
                service,
                created.job_id,
                status="completed",
                completed_at=10.0,
                updated_at=10.0,
            )

            requested = service.request_cancel(
                created.job_id,
                actor="operator@example.com",
                reason="late stop",
                now=20.0,
            )

            self.assertEqual(requested.status, "completed")
            self.assertEqual(requested.metadata, {"done": True})
            self.assertEqual(service.get(created.job_id).status, "completed")

    def test_worker_cancel_with_valid_lease_terminalizes_and_preserves_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            service = JobService(
                Path(tmpdir) / "claw.db",
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
            )
            created = service.enqueue(kind="pipeline.issue", metadata={"owner": "runner"})
            start = float(created.next_run_at or time.time())
            claimed = service.claim_next(worker_id="worker-1", now=start)
            assert claimed is not None
            checkpointed = service.checkpoint(
                created.job_id,
                {"stage": "running"},
                lease_owner=claimed.lease_owner,
                lease_generation=claimed.lease_generation,
            )
            assert checkpointed is not None
            _set_job_columns(service, created.job_id, result_json={"partial": "ok"})

            cancelled = service.worker_cancel(
                created.job_id,
                lease_owner="worker-1",
                lease_generation=claimed.lease_generation,
                reason="worker observed cancel request",
                now=start + 5,
            )

            self.assertIsNotNone(cancelled)
            assert cancelled is not None
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.error, "worker observed cancel request")
            self.assertEqual(cancelled.completed_at, start + 5)
            self.assertEqual(cancelled.metadata, {"owner": "runner"})
            self.assertEqual(cancelled.checkpoint, {"stage": "running"})
            self.assertEqual(cancelled.result, {"partial": "ok"})
            self.assertIsNone(cancelled.lease_owner)
            self.assertIsNone(cancelled.lease_expires_at)
            self.assertIsNone(cancelled.lease_heartbeat_at)
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_worker_cancelled", events)

    def test_worker_cancel_requires_current_unexpired_lease(self) -> None:
        for case in ("wrong_owner", "old_generation", "missing_generation", "expired_lease"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmpdir:
                    service = JobService(
                        Path(tmpdir) / "claw.db",
                        formal_leases_enabled=True,
                        default_lease_seconds=30,
                    )
                    created = service.enqueue(kind="pipeline.issue", metadata={"owner": "runner"})
                    start = float(created.next_run_at or time.time())
                    claimed = service.claim_next(worker_id="worker-1", now=start)
                    assert claimed is not None
                    kwargs = {
                        "lease_owner": "worker-1",
                        "lease_generation": claimed.lease_generation,
                        "reason": "worker cancel",
                        "now": start + 5,
                    }
                    if case == "wrong_owner":
                        kwargs["lease_owner"] = "worker-2"
                    elif case == "old_generation":
                        kwargs["lease_generation"] = claimed.lease_generation - 1
                    elif case == "missing_generation":
                        kwargs["lease_generation"] = None
                    else:
                        kwargs["now"] = start + 31

                    result = service.worker_cancel(created.job_id, **kwargs)

                    self.assertIsNone(result)
                    persisted = service.get(created.job_id)
                    self.assertEqual(persisted.status, "running")
                    self.assertEqual(persisted.metadata, {"owner": "runner"})
                    self.assertEqual(persisted.lease_owner, "worker-1")
                    self.assertEqual(persisted.lease_generation, claimed.lease_generation)
                    self.assertEqual(persisted.error, "")

    def test_admin_force_cancel_fails_closed_without_valid_authority(self) -> None:
        for case in ("no_validator", "validator_rejects", "validator_raises"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmpdir:
                    validator = None
                    if case == "validator_rejects":
                        validator = lambda actor, job_id, reason, token: False
                    elif case == "validator_raises":
                        def validator(actor, job_id, reason, token):
                            raise RuntimeError("validator unavailable")

                    service = JobService(
                        Path(tmpdir) / "claw.db",
                        formal_leases_enabled=True,
                        admin_cancel_authority_validator=validator,
                    )
                    created = service.enqueue(kind="pipeline.issue", metadata={"owner": "runner"})
                    start = float(created.next_run_at or time.time())
                    claimed = service.claim_next(worker_id="worker-1", now=start)
                    assert claimed is not None

                    result = service.admin_force_cancel(
                        created.job_id,
                        admin_actor="admin@example.com",
                        reason="operator requested",
                        authority_token="secret-token",
                        now=start + 1,
                    )

                    self.assertIsNone(result)
                    persisted = service.get(created.job_id)
                    self.assertEqual(persisted.status, "running")
                    self.assertEqual(persisted.metadata, {"owner": "runner"})
                    self.assertEqual(persisted.lease_owner, "worker-1")

    def test_admin_force_cancel_requires_actor_reason_and_authority_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(
                Path(tmpdir) / "claw.db",
                formal_leases_enabled=True,
                admin_cancel_authority_validator=lambda actor, job_id, reason, token: True,
            )
            created = service.enqueue(kind="pipeline.issue")

            for kwargs in (
                {
                    "admin_actor": "",
                    "reason": "operator requested",
                    "authority_token": "secret-token",
                },
                {
                    "admin_actor": "admin@example.com",
                    "reason": "",
                    "authority_token": "secret-token",
                },
                {
                    "admin_actor": "admin@example.com",
                    "reason": "operator requested",
                    "authority_token": "",
                },
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        service.admin_force_cancel(created.job_id, **kwargs)
                    self.assertEqual(service.get(created.job_id).status, "queued")

    def test_admin_force_cancel_with_authority_terminalizes_running_job_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            validator_calls = []

            def validator(actor, job_id, reason, token):
                validator_calls.append((actor, job_id, reason, token))
                return token == "secret-token"

            service = JobService(
                Path(tmpdir) / "claw.db",
                observe=observe,
                formal_leases_enabled=True,
                default_lease_seconds=30,
                admin_cancel_authority_validator=validator,
            )
            created = service.enqueue(kind="pipeline.issue", metadata={"owner": "runner"})
            start = float(created.next_run_at or time.time())
            claimed = service.claim_next(worker_id="worker-1", now=start)
            assert claimed is not None
            service.checkpoint(
                created.job_id,
                {"stage": "running"},
                lease_owner=claimed.lease_owner,
                lease_generation=claimed.lease_generation,
            )

            cancelled = service.admin_force_cancel(
                created.job_id,
                admin_actor="admin@example.com",
                reason="operator requested",
                authority_token="secret-token",
                now=start + 5,
            )

            self.assertIsNotNone(cancelled)
            assert cancelled is not None
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.error, "operator requested")
            self.assertEqual(cancelled.completed_at, start + 5)
            self.assertIsNone(cancelled.lease_owner)
            self.assertIsNone(cancelled.lease_expires_at)
            self.assertIsNone(cancelled.lease_heartbeat_at)
            self.assertEqual(
                validator_calls,
                [
                    (
                        "admin@example.com",
                        created.job_id,
                        "operator requested",
                        "secret-token",
                    )
                ],
            )
            audit = cancelled.metadata["admin_force_cancel"]
            self.assertEqual(audit["cancelled_at"], start + 5)
            self.assertEqual(audit["admin_actor"], "admin@example.com")
            self.assertEqual(audit["reason"], "operator requested")
            self.assertEqual(audit["previous_status"], "running")
            self.assertEqual(audit["previous_worker_id"], "worker-1")
            self.assertEqual(audit["previous_lease_owner"], "worker-1")
            self.assertEqual(audit["previous_lease_generation"], claimed.lease_generation)
            self.assertEqual(audit["previous_lease_expires_at"], start + 30)
            self.assertEqual(audit["authority_reference"], audit["correlation_id"])
            self.assertNotIn("authority_token_hash", audit)
            self.assertNotIn("secret-token", str(cancelled.metadata))
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_admin_force_cancelled", events)

    def test_admin_force_cancel_with_authority_can_cancel_active_non_running_statuses(self) -> None:
        for status in ("queued", "retrying", "waiting_approval"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    service = JobService(
                        Path(tmpdir) / "claw.db",
                        formal_leases_enabled=True,
                        admin_cancel_authority_validator=(
                            lambda actor, job_id, reason, token: token == "secret-token"
                        ),
                    )
                    created = service.enqueue(kind="pipeline.issue", metadata={"status": status})
                    if status != "queued":
                        _set_job_columns(
                            service,
                            created.job_id,
                            status=status,
                            updated_at=100.0,
                        )

                    cancelled = service.admin_force_cancel(
                        created.job_id,
                        admin_actor="admin@example.com",
                        reason="operator requested",
                        authority_token="secret-token",
                        now=101.0,
                    )

                    self.assertEqual(cancelled.status, "cancelled")
                    self.assertEqual(
                        cancelled.metadata["admin_force_cancel"]["previous_status"],
                        status,
                    )

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

    def test_recover_stale_running_accepts_single_kind_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            target = service.enqueue(kind="coordinator.autonomous_task", max_attempts=3)
            unrelated = service.enqueue(kind="c", max_attempts=3)
            service.claim(target.job_id, worker_id="coordinator", now=1000.0)
            service.claim(unrelated.job_id, worker_id="letter-runner", now=1000.0)

            recovered = service.recover_stale_running(
                kinds="coordinator.autonomous_task",
                stale_after_seconds=60,
                now=2000.0,
            )

            self.assertEqual([job.job_id for job in recovered], [target.job_id])
            self.assertEqual(service.get(target.job_id).status, "retrying")
            self.assertEqual(service.get(unrelated.job_id).status, "running")

    def test_recover_stale_running_preserves_zero_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            created = service.enqueue(kind="coordinator.autonomous_task", max_attempts=3)
            service.claim(created.job_id, worker_id="coordinator", now=100.0)
            with service._lock:
                service._conn.execute(
                    """
                    UPDATE agent_jobs
                    SET updated_at = ?, started_at = ?, created_at = ?
                    WHERE job_id = ?
                    """,
                    (0.0, 100.0, 200.0, created.job_id),
                )
                service._conn.commit()

            recovered = service.recover_stale_running(
                kinds=("coordinator.autonomous_task",),
                stale_after_seconds=60,
                now=120.0,
            )

            self.assertEqual([job.job_id for job in recovered], [created.job_id])
            job = service.get(created.job_id)
            self.assertEqual(job.status, "retrying")
            self.assertEqual(job.checkpoint["stale_running_recovery"]["age_seconds"], 120.0)

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

    def test_recover_stale_running_no_retry_fails_terminally_below_budget(self) -> None:
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
                no_retry=True,
                error="stale_running_no_durable_consumer",
            )

            self.assertEqual(len(recovered), 1)
            job = service.get(created.job_id)
            self.assertEqual(job.status, "failed")
            self.assertEqual(job.error, "stale_running_no_durable_consumer")
            self.assertIsNotNone(job.completed_at)
            self.assertEqual(job.attempts, 1)
            events = [event["event_type"] for event in observe.job_events(created.job_id)]
            self.assertIn("job_failed", events)
            self.assertNotIn("job_retrying", events)

    def test_recover_stale_running_matches_kind_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = JobService(Path(tmpdir) / "claw.db")
            research = service.enqueue(kind="notebooklm.research", max_attempts=3)
            podcast = service.enqueue(kind="notebooklm.podcast", max_attempts=3)
            unrelated = service.enqueue(kind="coordinator.autonomous_task", max_attempts=3)
            for rec in (research, podcast, unrelated):
                service.claim(rec.job_id, worker_id="w", now=1000.0)

            recovered = service.recover_stale_running(
                kind_prefix="notebooklm.",
                stale_after_seconds=60,
                now=2000.0,
                no_retry=True,
            )

            self.assertEqual({job.job_id for job in recovered}, {research.job_id, podcast.job_id})
            self.assertEqual(service.get(research.job_id).status, "failed")
            self.assertEqual(service.get(podcast.job_id).status, "failed")
            self.assertEqual(service.get(unrelated.job_id).status, "running")


if __name__ == "__main__":
    unittest.main()
