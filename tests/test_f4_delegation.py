from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger


class _StubCoordinator:
    """Minimal non-None coordinator stub.

    ``ensure_autonomous_task_enqueued`` only checks ``self.coordinator is None``;
    it never invokes the coordinator (execution is ledger-driven, off this path).
    """


class BootstrapIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.memory = MemoryStore(self.root / "claw.db")
        self.observe = ObserveStream(self.root / "observe.db")
        self.ledger = TaskLedger(self.root / "claw.db", observe=self.observe)
        self.jobs = JobService(self.root / "claw.db", observe=self.observe)

    def _handler(self, *, with_coordinator: bool = True, with_jobs: bool = True) -> TaskHandler:
        return TaskHandler(
            coordinator=_StubCoordinator() if with_coordinator else None,
            observe=self.observe,
            task_ledger=self.ledger,
            job_service=self.jobs if with_jobs else None,
            get_session_state=self.memory.get_session_state,
            update_session_state=self.memory.update_session_state,
            workspace_root=self.root,
        )

    def _bootstrap(self, handler: TaskHandler, task_id: str):
        return handler.ensure_autonomous_task_enqueued(
            task_id=task_id,
            session_id="tg-1",
            objective="Revisa el feed de X",
            mode="chat",
            task_kind="authenticated_browse",
            source_text="Haz un repaso por X",
            delegation_metadata={"source": "f4_deterministic_delegation"},
        )

    def test_bootstrap_is_idempotent_on_deterministic_task_id(self) -> None:
        handler = self._handler()
        tid = "f4bdeliv:tg-1:111"

        r1 = self._bootstrap(handler, tid)
        r2 = self._bootstrap(handler, tid)

        self.assertEqual(r1.task_id, tid)
        self.assertEqual(r1.coordinator_job_id, r2.coordinator_job_id)
        self.assertTrue(r1.task_created)
        self.assertFalse(r2.task_created)
        self.assertTrue(r1.job_created)
        self.assertFalse(r2.job_created)
        self.assertEqual(r1.status, "started")
        self.assertEqual(r2.status, "started")

        # Exactly one ledger row and one coordinator job.
        record = self.ledger.get(tid)
        self.assertIsNotNone(record)
        assert record is not None
        # Resumable-by-startup-recovery shape (see _is_resumable_record).
        self.assertEqual(record.status, "running")
        self.assertEqual(record.runtime, "coordinator")
        self.assertIs(record.metadata.get("autonomous"), True)
        jobs = self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].job_id, r1.coordinator_job_id)

    def test_bootstrap_coordinator_unavailable(self) -> None:
        handler = self._handler(with_coordinator=False)
        tid = "f4bdeliv:tg-1:222"

        result = self._bootstrap(handler, tid)

        self.assertEqual(result.status, "coordinator_unavailable")
        self.assertIsNone(result.coordinator_job_id)
        self.assertFalse(result.task_created)
        self.assertFalse(result.job_created)
        # No durable state written.
        self.assertIsNone(self.ledger.get(tid))
        self.assertEqual(self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50), [])

    def test_bootstrap_job_service_unavailable(self) -> None:
        handler = self._handler(with_jobs=False)
        tid = "f4bdeliv:tg-1:333"

        result = self._bootstrap(handler, tid)

        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.coordinator_job_id)
        self.assertFalse(result.job_created)
        # No coordinator job could be enqueued.
        self.assertEqual(self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50), [])

    def test_bootstrap_concurrent_same_task_id_one_pair(self) -> None:
        handler = self._handler()
        tid = "f4bdeliv:tg-1:444"
        barrier = threading.Barrier(2)
        results: list = []
        results_lock = threading.Lock()
        errors: list = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                result = self._bootstrap(handler, tid)
            except Exception as exc:  # pragma: no cover - surfaced via errors list
                with results_lock:
                    errors.append(exc)
                return
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        # Exactly one ledger row.
        self.assertIsNotNone(self.ledger.get(tid))
        # Exactly one coordinator job, and both callers observe the same one.
        jobs = self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50)
        self.assertEqual(len(jobs), 1)
        job_ids = {result.coordinator_job_id for result in results}
        self.assertEqual(job_ids, {jobs[0].job_id})
        for result in results:
            self.assertEqual(result.status, "started")
        # job_created is authoritative (reserve atomically elects one creator):
        # exactly one of the two concurrent callers created the coordinator job.
        self.assertEqual([r.job_created for r in results].count(True), 1)

    def test_bootstrap_retry_after_progress_preserves_state(self) -> None:
        handler = self._handler()
        tid = "f4bdeliv:tg-1:555"

        first = self._bootstrap(handler, tid)
        self.assertTrue(first.task_created)

        # Simulate the coordinator having progressed AND succeeded the task:
        # goal_id/resume_count recorded in metadata, real evidence in artifacts,
        # terminal status with completed_at set. This mirrors the state
        # _resume_autonomous_record leaves behind once it runs (create() upsert).
        self.ledger.create(
            task_id=tid,
            session_id="tg-1",
            objective="Revisa el feed de X",
            runtime="coordinator",
            mode="chat",
            status="succeeded",
            metadata={"autonomous": True, "goal_id": "goal-xyz", "resume_count": 2},
            artifacts={"progress": "real-evidence"},
        )

        # A crash-after-bootstrap retry of the SAME delivery must NOT clobber or
        # resurrect the progressed/terminal row.
        second = self._bootstrap(handler, tid)

        record = self.ledger.get(tid)
        self.assertIsNotNone(record)
        assert record is not None
        # Not resurrected: terminal status and completed_at preserved.
        self.assertEqual(record.status, "succeeded")
        self.assertIsNotNone(record.completed_at)
        # Not clobbered: coordinator progress in metadata + artifacts preserved.
        self.assertEqual(record.metadata.get("goal_id"), "goal-xyz")
        self.assertEqual(record.metadata.get("resume_count"), 2)
        self.assertEqual(record.artifacts.get("progress"), "real-evidence")
        # Idempotent: the retry did not re-materialise the task.
        self.assertFalse(second.task_created)
        self.assertEqual(second.status, "started")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
