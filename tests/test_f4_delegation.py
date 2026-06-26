from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.f4_delegation import (
    F4_DELEGATION_JOB_KIND,
    F4DelegationJobRunner,
    f4b_delivery_task_id,
)
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

    def test_bootstrap_on_terminal_task_mints_no_new_coordinator_job(self) -> None:
        # Part A guard: a crash-recovery re-invocation of the bootstrap against an
        # ALREADY-TERMINAL task (e.g. stale-reclaim re-running it after the task
        # finished) must NOT mint a spurious coordinator job. The prior coordinator
        # job is terminal too, so it is out of the ACTIVE-resume_key partial unique
        # index (jobs.py:58-61) and an unconditional reserve() would re-INSERT.
        handler = self._handler()
        tid = "f4bdeliv:tg-1:terminal"

        first = self._bootstrap(handler, tid)
        self.assertEqual(first.status, "started")
        coord_job_id = first.coordinator_job_id
        self.assertIsNotNone(coord_job_id)

        # Drive BOTH the coordinator job and the ledger row terminal, mirroring real
        # completion + orphan reconciliation. Without terminalising the job too the
        # guard would never be exercised (reserve would find it active and dedup).
        self.jobs.cancel(coord_job_id, reason="orphaned_by_task:failed")
        self.ledger.mark_terminal(tid, status="failed", error="boom")
        before = self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50)
        self.assertEqual(len(before), 1)

        second = self._bootstrap(handler, tid)

        # No SPURIOUS coordinator job minted.
        after = self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].job_id, coord_job_id)
        self.assertFalse(second.task_created)
        self.assertFalse(second.job_created)
        self.assertEqual(second.status, "started")
        # Discoverable existing/terminal coordinator job id is reflected back.
        self.assertEqual(second.coordinator_job_id, coord_job_id)
        # No resumable/running row resurrected: still terminal.
        record = self.ledger.get(tid)
        assert record is not None
        self.assertEqual(record.status, "failed")

    def test_terminal_task_not_resumed_no_reexecution(self) -> None:
        # Confirms WHY a (hypothetical) spurious coordinator job cannot cause
        # re-execution: ledger-driven recovery only lists status="running" rows
        # (task_handler.py:1928), so a terminal row never reaches
        # _resume_autonomous_record. (Independently: no runtime runner ever does
        # claim_next(kinds=("coordinator.autonomous_task",)) — the coordinator job
        # is a reservation/tracking handle, never directly executed.)
        handler = self._handler()
        tid = "f4bdeliv:tg-1:terminal-resume"
        self.ledger.create(
            task_id=tid,
            session_id="tg-1",
            objective="Revisa el feed de X",
            runtime="coordinator",
            mode="chat",
            status="failed",
            metadata={"autonomous": True},
        )

        resumed = handler.resume_interrupted_autonomous_tasks()

        self.assertEqual(resumed, 0)
        record = self.ledger.get(tid)
        assert record is not None
        self.assertEqual(record.status, "failed")


class DeliveryTaskIdTests(unittest.TestCase):
    def test_deterministic_and_stable(self) -> None:
        key = "f4b-delegation:tg-1:111"
        first = f4b_delivery_task_id(key)
        second = f4b_delivery_task_id(key)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("f4bdeliv:"))
        # Distinct delivery keys map to distinct task ids.
        self.assertNotEqual(first, f4b_delivery_task_id("f4b-delegation:tg-1:222"))


class F4DelegationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.memory = MemoryStore(self.root / "claw.db")
        self.observe = ObserveStream(self.root / "observe.db")
        self.ledger = TaskLedger(self.root / "claw.db", observe=self.observe)
        self.jobs = JobService(self.root / "claw.db", observe=self.observe)

    def _handler(self, *, with_coordinator: bool = True) -> TaskHandler:
        return TaskHandler(
            coordinator=_StubCoordinator() if with_coordinator else None,
            observe=self.observe,
            task_ledger=self.ledger,
            job_service=self.jobs,
            get_session_state=self.memory.get_session_state,
            update_session_state=self.memory.update_session_state,
            workspace_root=self.root,
        )

    def _runner(
        self, *, with_coordinator: bool = True, stale_running_seconds: float = 6 * 60 * 60
    ) -> F4DelegationJobRunner:
        return F4DelegationJobRunner(
            job_service=self.jobs,
            task_handler=self._handler(with_coordinator=with_coordinator),
            observe=self.observe,
            stale_running_seconds=stale_running_seconds,
        )

    def _payload(self, *, task_id: str) -> dict:
        return {
            "task_id": task_id,
            "session_id": "tg-1",
            "objective": "Revisa el feed de X",
            "mode": "chat",
            "task_kind": "authenticated_browse",
            "source_text": "Haz un repaso por X",
            "delegation_metadata": {"source": "f4_deterministic_delegation"},
        }

    def _enqueue_delivery(self, *, delivery_key: str, task_id: str):
        job, created = self.jobs.reserve(
            resume_key=delivery_key,
            kind=F4_DELEGATION_JOB_KIND,
            payload=self._payload(task_id=task_id),
        )
        self.assertTrue(created)
        return job

    def _coordinator_jobs(self) -> list:
        return self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50)

    def test_runner_bootstraps_one_task_and_completes_delivery_job(self) -> None:
        runner = self._runner()
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)

        processed = runner.run_available()

        self.assertEqual(processed, 1)
        # Exactly one agent_tasks row.
        self.assertIsNotNone(self.ledger.get(task_id))
        # Exactly one coordinator job.
        coord_jobs = self._coordinator_jobs()
        self.assertEqual(len(coord_jobs), 1)
        # Delivery job completed, checkpoint links task + coordinator job.
        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.checkpoint.get("task_id"), task_id)
        self.assertEqual(final.checkpoint.get("coordinator_job_id"), coord_jobs[0].job_id)
        self.assertEqual(final.result.get("coordinator_job_id"), coord_jobs[0].job_id)

    def test_runner_rerun_after_reclaim_no_duplicate(self) -> None:
        runner = self._runner()
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)

        # Simulate a crash AFTER bootstrap but BEFORE checkpoint/complete: the
        # runner claimed the delivery job (-> running) and materialised the task,
        # then died, leaving the delivery job stuck running.
        claimed = self.jobs.claim_next(worker_id="f4b_delegation", kinds=(F4_DELEGATION_JOB_KIND,))
        assert claimed is not None
        self.assertEqual(claimed.job_id, job.job_id)
        first = self._handler().ensure_autonomous_task_enqueued(**self._payload(task_id=task_id))
        self.assertEqual(first.status, "started")
        self.assertTrue(first.task_created)

        # Stale-running reclaim re-queues the crashed delivery job (-> retrying).
        now_future = time.time() + 7 * 60 * 60
        self.assertEqual(runner.reclaim_stale_running(now=now_future), 1)
        reclaimed = self.jobs.get(job.job_id)
        assert reclaimed is not None
        self.assertEqual(reclaimed.status, "retrying")

        # Re-run: claims the retrying job, re-bootstraps idempotently, completes.
        self.assertEqual(runner.run_available(now=now_future), 1)

        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")
        # Idempotent: still exactly one task row + one coordinator job.
        self.assertIsNotNone(self.ledger.get(task_id))
        coord_jobs = self._coordinator_jobs()
        self.assertEqual(len(coord_jobs), 1)
        self.assertEqual(final.checkpoint.get("task_id"), task_id)
        self.assertEqual(final.checkpoint.get("coordinator_job_id"), coord_jobs[0].job_id)

    def test_runner_bootstrap_failure_terminalizes_not_deletes(self) -> None:
        # coordinator=None -> ensure_autonomous_task_enqueued returns
        # "coordinator_unavailable"; the delivery job must fail/retry, never delete.
        runner = self._runner(with_coordinator=False)
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)

        # First failure retries (attempts < max_attempts): NOT deleted, NOT terminal yet.
        self.assertEqual(runner.run_available(), 1)
        after_first = self.jobs.get(job.job_id)
        assert after_first is not None
        self.assertEqual(after_first.status, "retrying")
        self.assertEqual(after_first.error, "coordinator unavailable")

        # Drive past max_attempts -> terminal "failed", advancing past each retry delay.
        now = time.time()
        for _ in range(5):
            now += 120
            runner.run_available(now=now)

        final = self.jobs.get(job.job_id)
        assert final is not None  # quarantined, NEVER deleted
        self.assertEqual(final.status, "failed")
        # No task / coordinator job ever materialised.
        self.assertIsNone(self.ledger.get(task_id))
        self.assertEqual(self._coordinator_jobs(), [])

    def test_runner_maintenance_leaves_job_queued(self) -> None:
        runner = self._runner()
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)

        with patch.dict(os.environ, {"CLAW_MAINTENANCE_MODE": "1"}, clear=False):
            processed = runner.run_available()

        self.assertEqual(processed, 0)
        # The claim was gated: job untouched, no task, no coordinator job.
        after = self.jobs.get(job.job_id)
        assert after is not None
        self.assertEqual(after.status, "queued")
        self.assertIsNone(self.ledger.get(task_id))
        self.assertEqual(self._coordinator_jobs(), [])

    def test_runner_bootstrap_exception_retries_not_stuck_running(self) -> None:
        # A *raised* bootstrap error (e.g. a transient DB write failure) must not
        # leave the claimed job wedged in 'running' until the 6h stale reclaim:
        # the runner recovers it for retry, the same as the reference runner.
        runner = self._runner()
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)

        def _boom(**_kwargs: object) -> object:
            raise RuntimeError("transient bootstrap failure")

        runner.task_handler.ensure_autonomous_task_enqueued = _boom  # type: ignore[method-assign]

        processed = runner.run_available()

        self.assertEqual(processed, 1)
        after = self.jobs.get(job.job_id)
        assert after is not None  # recovered, NEVER deleted
        self.assertEqual(after.status, "retrying")
        # Nothing materialised from the failed bootstrap.
        self.assertIsNone(self.ledger.get(task_id))
        self.assertEqual(self._coordinator_jobs(), [])

    def _assert_linkage_raise_recovers(self, broken_attr: str) -> None:
        # A *raised* error from the checkpoint/complete linkage (which sits AFTER
        # a successful bootstrap) must recover via fail(retry=True), not wedge the
        # claimed job in 'running' until the 6h stale reclaim. The bootstrap has
        # already materialised exactly one task + coordinator job; a re-run must
        # NOT duplicate either.
        runner = self._runner()
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)

        original = getattr(self.jobs, broken_attr)

        def _boom(*_a: object, **_k: object) -> object:
            raise RuntimeError(f"{broken_attr} disk failure")

        setattr(self.jobs, broken_attr, _boom)
        try:
            self.assertEqual(runner.run_available(), 1)
        finally:
            setattr(self.jobs, broken_attr, original)

        after = self.jobs.get(job.job_id)
        assert after is not None
        self.assertEqual(after.status, "retrying")  # NOT stuck 'running', NOT deleted
        # Bootstrap already materialised exactly one task + coordinator job.
        self.assertIsNotNone(self.ledger.get(task_id))
        self.assertEqual(len(self._coordinator_jobs()), 1)

        # Re-run with the real method restored -> idempotent: completes cleanly,
        # no duplicate task or coordinator job.
        now_future = time.time() + 7 * 60 * 60
        self.assertEqual(runner.run_available(now=now_future), 1)
        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")
        self.assertIsNotNone(self.ledger.get(task_id))
        coord_jobs = self._coordinator_jobs()
        self.assertEqual(len(coord_jobs), 1)
        self.assertEqual(final.checkpoint.get("task_id"), task_id)
        self.assertEqual(final.checkpoint.get("coordinator_job_id"), coord_jobs[0].job_id)

    def test_runner_checkpoint_raise_retries_not_stuck_running(self) -> None:
        self._assert_linkage_raise_recovers("checkpoint")

    def test_runner_complete_raise_retries_not_stuck_running(self) -> None:
        self._assert_linkage_raise_recovers("complete")

    def test_runner_honors_should_stop(self) -> None:
        task_id = "f4bdeliv:tg-1:111"
        job = self._enqueue_delivery(delivery_key="f4b-delegation:tg-1:111", task_id=task_id)
        runner = F4DelegationJobRunner(
            job_service=self.jobs,
            task_handler=self._handler(),
            observe=self.observe,
            should_stop=lambda: True,
        )

        processed = runner.run_available()

        self.assertEqual(processed, 0)
        # Graceful stop: claim never happened, nothing materialised.
        after = self.jobs.get(job.job_id)
        assert after is not None
        self.assertEqual(after.status, "queued")
        self.assertIsNone(self.ledger.get(task_id))
        self.assertEqual(self._coordinator_jobs(), [])


class F4DelegationCrashBoundaryTests(unittest.TestCase):
    """Crash-boundary matrix for the durable delegation lane.

    At each window we simulate a process crash by CLOSING every sqlite connection
    on the shared DB files and rebuilding fresh service instances on the SAME
    paths (reopen == process restart), then resume the lane (run the runner /
    redeliver). Invariants asserted at EVERY window:

      * exactly one ``agent_tasks`` row for the deterministic ``task_id``,
      * exactly one ``coordinator.autonomous_task`` job (no second logical task),
      * an eventual terminal / ``completed`` delegation job.
    """

    delivery_key = "f4b-delegation:tg-1:111"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.task_id = f4b_delivery_task_id(self.delivery_key)
        self._open()

    def _open(self) -> None:
        self.memory = MemoryStore(self.root / "claw.db")
        self.observe = ObserveStream(self.root / "observe.db")
        self.ledger = TaskLedger(self.root / "claw.db", observe=self.observe)
        self.jobs = JobService(self.root / "claw.db", observe=self.observe)

    def _reopen(self) -> None:
        # Simulate a process crash + restart. There is no close() API; on the test
        # path each service owns its sqlite connection on ``_conn``, so close those
        # (releasing WAL/locks deterministically for the 25x loop) and rebuild every
        # service on the same DB paths.
        for service in (self.jobs, self.ledger, self.memory, self.observe):
            try:
                service._conn.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
        self._open()

    def _handler(self) -> TaskHandler:
        return TaskHandler(
            coordinator=_StubCoordinator(),
            observe=self.observe,
            task_ledger=self.ledger,
            job_service=self.jobs,
            get_session_state=self.memory.get_session_state,
            update_session_state=self.memory.update_session_state,
            workspace_root=self.root,
        )

    def _runner(self) -> F4DelegationJobRunner:
        return F4DelegationJobRunner(
            job_service=self.jobs,
            task_handler=self._handler(),
            observe=self.observe,
        )

    def _payload(self) -> dict:
        return {
            "task_id": self.task_id,
            "session_id": "tg-1",
            "objective": "Revisa el feed de X",
            "mode": "chat",
            "task_kind": "authenticated_browse",
            "source_text": "Haz un repaso por X",
            "delegation_metadata": {"source": "f4_deterministic_delegation"},
        }

    def _enqueue_delivery(self):
        return self.jobs.reserve(
            resume_key=self.delivery_key,
            kind=F4_DELEGATION_JOB_KIND,
            payload=self._payload(),
        )

    def _delivery_jobs(self) -> list:
        return self.jobs.list(kinds=[F4_DELEGATION_JOB_KIND], limit=50)

    def _coordinator_jobs(self) -> list:
        return self.jobs.list(kinds=["coordinator.autonomous_task"], limit=50)

    def _assert_one_logical_task(self):
        record = self.ledger.get(self.task_id)
        self.assertIsNotNone(record)
        coord = self._coordinator_jobs()
        self.assertEqual(len(coord), 1)
        return record, coord[0]

    def test_window1_crash_before_delivery_commit_redelivery_enqueues_one(self) -> None:
        # Crash BEFORE the delivery job committed: nothing persisted. Reopen
        # (restart) then redeliver -> exactly one f4b.delegation job; the runner
        # then bootstraps exactly one logical task and completes the delivery job.
        self._reopen()
        self.assertEqual(self._delivery_jobs(), [])

        job, created = self._enqueue_delivery()
        self.assertTrue(created)
        self.assertEqual(len(self._delivery_jobs()), 1)

        self.assertEqual(self._runner().run_available(), 1)

        self._assert_one_logical_task()
        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")

    def test_window2_crash_after_commit_before_claim_runner_bootstraps_once(self) -> None:
        # Crash AFTER the delivery job committed but BEFORE any claim.
        job, created = self._enqueue_delivery()
        self.assertTrue(created)
        self._reopen()

        self.assertEqual(self._runner().run_available(), 1)

        _, coord = self._assert_one_logical_task()
        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.checkpoint.get("task_id"), self.task_id)
        self.assertEqual(final.checkpoint.get("coordinator_job_id"), coord.job_id)

    def test_window3_crash_after_claim_before_bootstrap_reclaim_bootstraps_once(self) -> None:
        # Crash AFTER claim, BEFORE bootstrap: the delivery job is left 'running'.
        job, _ = self._enqueue_delivery()
        claimed = self.jobs.claim_next(worker_id="f4b_delegation", kinds=(F4_DELEGATION_JOB_KIND,))
        assert claimed is not None
        self.assertEqual(claimed.job_id, job.job_id)
        self.assertEqual(claimed.status, "running")
        self._reopen()

        # run_available reclaims the stale 'running' job (default 6h window vs a
        # now far in the future), then claims + bootstraps it exactly once.
        now_future = time.time() + 7 * 60 * 60
        self.assertEqual(self._runner().run_available(now=now_future), 1)

        _, coord = self._assert_one_logical_task()
        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.checkpoint.get("coordinator_job_id"), coord.job_id)

    def test_window4_crash_after_bootstrap_before_complete_idempotent(self) -> None:
        # Crash AFTER claim + bootstrap, BEFORE checkpoint/complete: the task +
        # coordinator job already exist; the delivery job is left 'running'.
        job, _ = self._enqueue_delivery()
        claimed = self.jobs.claim_next(worker_id="f4b_delegation", kinds=(F4_DELEGATION_JOB_KIND,))
        assert claimed is not None
        boot = self._handler().ensure_autonomous_task_enqueued(**self._payload())
        self.assertEqual(boot.status, "started")
        self.assertTrue(boot.task_created)
        _, coord_before = self._assert_one_logical_task()
        self._reopen()

        now_future = time.time() + 7 * 60 * 60
        self.assertEqual(self._runner().run_available(now=now_future), 1)

        # Idempotent: still exactly ONE task row + ONE coordinator job (same one).
        _, coord_after = self._assert_one_logical_task()
        self.assertEqual(coord_after.job_id, coord_before.job_id)
        final = self.jobs.get(job.job_id)
        assert final is not None
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.checkpoint.get("task_id"), self.task_id)
        self.assertEqual(final.checkpoint.get("coordinator_job_id"), coord_after.job_id)

    def test_window5_crash_after_delivery_completion_terminal_task_no_new_work(self) -> None:
        # First delivery runs to completion: task 'running', coordinator job active.
        job1, _ = self._enqueue_delivery()
        self.assertEqual(self._runner().run_available(), 1)
        _, coord1 = self._assert_one_logical_task()
        first_final = self.jobs.get(job1.job_id)
        assert first_final is not None
        self.assertEqual(first_final.status, "completed")

        # The coordinator finished: terminalise BOTH the coordinator job (mirrors
        # orphan reconciliation) AND the ledger row, so the deterministic task_id is
        # fully terminal before the redelivery. (Without terminalising the job too,
        # reserve would dedup on the still-active job and the guard would be moot.)
        self.jobs.cancel(coord1.job_id, reason="orphaned_by_task:failed")
        self.ledger.mark_terminal(self.task_id, status="failed", error="boom")
        self.assertEqual(len(self._coordinator_jobs()), 1)

        self._reopen()

        # Redeliver the SAME delivery_key: the prior delivery job is terminal (out
        # of the active resume_key index), so reserve mints a fresh delivery job.
        job2, created = self._enqueue_delivery()
        self.assertTrue(created)
        self.assertNotEqual(job2.job_id, job1.job_id)

        self.assertEqual(self._runner().run_available(), 1)

        # Part A guard: terminal task -> NO new coordinator job, NO second task row,
        # row NOT resurrected (stays terminal -> no re-execution scheduling).
        self.assertEqual(len(self._coordinator_jobs()), 1)
        record_after = self.ledger.get(self.task_id)
        assert record_after is not None
        self.assertEqual(record_after.status, "failed")
        # The redelivery job terminalises (bootstrap returned "started").
        final2 = self.jobs.get(job2.job_id)
        assert final2 is not None
        self.assertEqual(final2.status, "completed")
        self.assertEqual(final2.checkpoint.get("coordinator_job_id"), coord1.job_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
