from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import (
    PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,
    PENDING_VERIFICATION_RECONCILIATION_RESUME_KEY,
    ClawDaemon,
    PendingVerificationReconciliationJobRunner,
    RecoveryJobDrainRunner,
    TickResult,
)
from claw_v2.heartbeat import HeartbeatSnapshot
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.task_ledger import TaskLedger


class DaemonTickTests(unittest.TestCase):
    def _make_daemon(self) -> tuple[ClawDaemon, MagicMock, MagicMock]:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="2026-01-01T00:00:00",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        heartbeat.emit.return_value = heartbeat.collect.return_value
        observe = MagicMock()
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
        return daemon, heartbeat, observe

    def test_tick_enqueues_pending_verification_reconciliation_agent_job(self) -> None:
        # PR 1A: daemon.tick() may enqueue reconciliation work, but must not run
        # the reconciler or drain inside the daemon control path.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(ledger, job_service=jobs)

            daemon.tick(now=1_000_000)

            queued = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)
            self.assertEqual(len(queued), 1)
            job = queued[0]
            self.assertEqual(job.status, "queued")
            self.assertEqual(job.kind, PENDING_VERIFICATION_RECONCILIATION_JOB_KIND)
            self.assertEqual(job.resume_key, PENDING_VERIFICATION_RECONCILIATION_RESUME_KEY)
            self.assertFalse(job.payload["drain_apply"])
            ledger.list.assert_not_called()
            ledger.mark_terminal.assert_not_called()
            ledger.drain_reconcilable_unverified.assert_not_called()

            emitted = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("pending_verification_reconciliation_enqueued", emitted)
            self.assertNotIn("pending_verification_reconciliation", emitted)
            tick_payloads = [
                c.kwargs["payload"]
                for c in observe.emit.call_args_list
                if c.args[0] == "daemon_tick"
            ]
            self.assertEqual(
                tick_payloads[0]["pending_verification_reconciliation_job_id"], job.job_id
            )
            self.assertNotIn("pending_verification_unverified", tick_payloads[0])

    def _make_daemon_with_ledger(self, ledger: MagicMock, **kwargs) -> tuple[ClawDaemon, MagicMock]:
        scheduler = kwargs.pop("scheduler", CronScheduler())
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="2026-01-01T00:00:00",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        observe = MagicMock()
        daemon = ClawDaemon(
            scheduler=scheduler, heartbeat=heartbeat, observe=observe, task_ledger=ledger, **kwargs
        )
        return daemon, observe

    def test_pending_verification_skip_within_interval_omits_field(self) -> None:
        # A skipped enqueue must not report pending_verification_unverified=0 as
        # if it were a real backlog count.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(
                ledger,
                job_service=jobs,
                pending_verification_interval=900,
                # F0.3: idle ticks are now sampled out of observe_stream. Force
                # every tick to emit so this test can assert the field-omission
                # semantics (the second, skipped tick must not carry a stale
                # reconciliation job id) rather than the sampling behavior.
                tick_emit_sample=1,
            )
            daemon.tick(now=1_000_000)  # enqueue runs
            daemon.tick(now=1_000_030)  # within interval -> skipped

            queued = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)
            self.assertEqual(len(queued), 1)
            enqueued = [
                c
                for c in observe.emit.call_args_list
                if c.args[0] == "pending_verification_reconciliation_enqueued"
            ]
            self.assertEqual(len(enqueued), 1)
            tick_payloads = [
                c.kwargs["payload"]
                for c in observe.emit.call_args_list
                if c.args[0] == "daemon_tick"
            ]
            self.assertEqual(len(tick_payloads), 2)
            self.assertIn("pending_verification_reconciliation_job_id", tick_payloads[0])
            self.assertNotIn("pending_verification_reconciliation_job_id", tick_payloads[1])
            self.assertNotIn("pending_verification_unverified", tick_payloads[0])
            self.assertNotIn("pending_verification_unverified", tick_payloads[1])

    def test_resume_key_prevents_duplicate_active_reconciliation_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(
                ledger,
                job_service=jobs,
                pending_verification_interval=0,
            )

            daemon.tick(now=1_000_000)
            daemon.tick(now=1_000_001)

            queued = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)
            self.assertEqual(len(queued), 1)
            enqueued_payloads = [
                c.kwargs["payload"]
                for c in observe.emit.call_args_list
                if c.args[0] == "pending_verification_reconciliation_enqueued"
            ]
            self.assertEqual(len(enqueued_payloads), 2)
            self.assertEqual(enqueued_payloads[0]["job_id"], queued[0].job_id)
            self.assertEqual(enqueued_payloads[1]["job_id"], queued[0].job_id)

    def test_reconciliation_enqueue_failure_does_not_crash_tick(self) -> None:
        # A1: an enqueue exception must be contained — scheduler and the rest of
        # the tick still run, and the ledger is not scanned inline.
        ledger = MagicMock()
        ledger.mark_stale_running_lost.return_value = 0
        jobs = MagicMock()
        jobs.list.return_value = []
        jobs.enqueue.side_effect = RuntimeError("boom")
        daemon, observe = self._make_daemon_with_ledger(ledger, job_service=jobs)
        probe = MagicMock()
        daemon.scheduler.register(ScheduledJob(name="probe", interval_seconds=60, handler=probe))

        result = daemon.tick(now=1_000_000)  # must not raise

        self.assertIn("probe", result.executed_jobs)
        probe.assert_called_once()
        ledger.list.assert_not_called()
        ledger.mark_terminal.assert_not_called()
        errors = [
            c
            for c in observe.emit.call_args_list
            if c.args[0] == "pending_verification_reconciliation_enqueue_error"
        ]
        self.assertEqual(len(errors), 1)

    def test_drain_disabled_does_not_apply(self) -> None:
        # D: flag OFF — the job emits telemetry but must NOT call the drain.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.return_value = []
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(
                ledger,
                job_service=jobs,
                pending_verification_drain_apply=False,
            )
            daemon.tick(now=1_000_000)
            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
            )

            self.assertTrue(runner.run_once())

            ledger.drain_reconcilable_unverified.assert_not_called()
            job = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "completed")
            self.assertFalse(job.result["drain_apply"])

    def test_drain_enabled_applies_with_caps(self) -> None:
        # D: flag ON — the job calls the drain with apply=True and daemon caps.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.return_value = []
            ledger.drain_reconcilable_unverified.return_value = {"applied": 0}
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(
                ledger,
                job_service=jobs,
                pending_verification_drain_apply=True,
            )
            daemon.tick(now=1_000_000)
            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
            )

            self.assertTrue(runner.run_once())

            ledger.drain_reconcilable_unverified.assert_called_once()
            _, kwargs = ledger.drain_reconcilable_unverified.call_args
            self.assertTrue(kwargs["apply"])
            self.assertEqual(kwargs["max_apply"], 10)
            self.assertEqual(kwargs["max_scan"], 500)
            job = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.result["drain_result"], {"applied": 0})

    def test_drain_failure_does_not_crash_tick(self) -> None:
        # D: a drain exception must be contained in the job result.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.return_value = []
            ledger.drain_reconcilable_unverified.side_effect = RuntimeError("boom")
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(
                ledger,
                job_service=jobs,
                pending_verification_drain_apply=True,
            )
            daemon.tick(now=1_000_000)
            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
            )

            self.assertTrue(runner.run_once())  # must not raise

            job = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.result["drain_error"], "boom")

    def test_stale_running_reconciliation_job_is_reclaimed_and_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.return_value = []
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(ledger, job_service=jobs)
            now = time.time()
            daemon.tick(now=now)
            claimed_at = time.time() + 1
            stuck = jobs.claim_next(
                worker_id="dead-worker",
                kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,),
                now=claimed_at,
            )
            self.assertIsNotNone(stuck)

            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
                stale_running_seconds=1,
            )
            processed = runner.run_available(now=claimed_at + 2)

            self.assertEqual(processed, 1)
            job = jobs.get(stuck.job_id)
            self.assertIsNotNone(job)
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.attempts, 2)
            ledger.list.assert_called_once_with(statuses=("completed_unverified",), limit=100)
            stale_events = [
                c.kwargs["payload"]
                for c in observe.emit.call_args_list
                if c.args[0] == "daemon_reconciliation_job_stale_reclaimed"
            ]
            self.assertEqual(stale_events[0]["source"], "stale_running_reaper")
            self.assertEqual(stale_events[0]["job_id"], stuck.job_id)
            event_names = [c.args[0] for c in observe.emit.call_args_list]
            self.assertIn("daemon_reconciliation_job_started", event_names)
            self.assertIn("daemon_reconciliation_job_completed", event_names)

    def test_reconciliation_runner_respects_shutdown_before_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(ledger, job_service=jobs)
            daemon.tick(now=1_000_000)
            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
                should_stop=lambda: True,
            )

            self.assertEqual(runner.run_available(), 0)

            job = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "queued")
            ledger.list.assert_not_called()

    def test_drain_is_interval_gated_with_the_report(self) -> None:
        # The live drain shares the pending-verification interval gate through
        # enqueue: a second tick within the interval must NOT add a second job.
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.return_value = []
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(
                ledger,
                job_service=jobs,
                pending_verification_drain_apply=True,
            )
            daemon.tick(now=1_000_000)  # interval elapsed from 0 -> enqueues
            daemon.tick(now=1_000_010)  # within the 15-min interval -> skipped
            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
            )
            runner.run_available(limit=10)

            self.assertEqual(ledger.drain_reconcilable_unverified.call_count, 1)
            jobs_for_kind = jobs.list(
                kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10
            )
            self.assertEqual(len(jobs_for_kind), 1)

    def test_reconciliation_job_failure_retries_without_crashing_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.side_effect = RuntimeError("boom")
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon, observe = self._make_daemon_with_ledger(ledger, job_service=jobs)
            daemon.tick(now=1_000_000)
            runner = PendingVerificationReconciliationJobRunner(
                job_service=jobs,
                task_ledger=ledger,
                observe=observe,
                retry_delay_seconds=0,
            )

            self.assertTrue(runner.run_once())

            job = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)[0]
            self.assertEqual(job.status, "retrying")
            self.assertEqual(job.error, "boom")
            failed_events = [
                c.kwargs["payload"]
                for c in observe.emit.call_args_list
                if c.args[0] == "daemon_reconciliation_job_failed"
            ]
            self.assertEqual(len(failed_events), 1)
            self.assertEqual(failed_events[0]["job_id"], job.job_id)
            self.assertEqual(failed_events[0]["error_type"], "RuntimeError")
            self.assertEqual(failed_events[0]["error_preview"], "boom")
            probe = MagicMock()
            daemon.scheduler.register(
                ScheduledJob(name="probe", interval_seconds=60, handler=probe)
            )

            result = daemon.tick(now=1_001_000)

            self.assertIn("probe", result.executed_jobs)
            probe.assert_called_once()

    def test_drain_flag_defaults_off_and_reads_env(self) -> None:
        # Default OFF; env opt-in flips it on.
        import os
        from unittest import mock as _mock

        off, _ = self._make_daemon_with_ledger(MagicMock())
        self.assertFalse(off.pending_verification_drain_apply)
        with _mock.patch.dict(os.environ, {"CLAW_PENDING_VERIFICATION_DRAIN_APPLY": "1"}):
            on, _ = self._make_daemon_with_ledger(MagicMock())
        self.assertTrue(on.pending_verification_drain_apply)

    def test_tick_does_not_call_heartbeat_emit(self) -> None:
        daemon, heartbeat, _ = self._make_daemon()
        daemon.tick(now=1000)
        heartbeat.emit.assert_not_called()
        heartbeat.collect.assert_called_once()

    def test_tick_runs_scheduled_jobs(self) -> None:
        daemon, _, _ = self._make_daemon()
        handler = MagicMock()
        daemon.scheduler.register(
            ScheduledJob(name="test_job", interval_seconds=60, handler=handler)
        )
        result = daemon.tick(now=1000)
        self.assertIn("test_job", result.executed_jobs)
        handler.assert_called_once()

    def test_scheduler_reports_job_errors_to_sink(self) -> None:
        error_sink = MagicMock()
        scheduler = CronScheduler(error_sink=error_sink)

        def failing() -> None:
            raise RuntimeError("boom")

        scheduler.register(ScheduledJob(name="bad_job", interval_seconds=60, handler=failing))
        executed = scheduler.run_due(now=1000)

        self.assertEqual(executed, ["bad_job"])
        error_sink.assert_called_once()
        job, exc = error_sink.call_args.args
        self.assertEqual(job.name, "bad_job")
        self.assertEqual(str(exc), "boom")

    def test_tick_reconciles_stale_running_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="long task",
                runtime="coordinator",
                status="running",
            )
            with ledger._lock:
                ledger._conn.execute(
                    "UPDATE agent_tasks SET updated_at = ? WHERE task_id = ?",
                    (time.time() - 120, "task-1"),
                )
                ledger._conn.commit()
            daemon, _, observe = self._make_daemon()
            daemon.task_ledger = ledger
            daemon.stale_task_seconds = 60
            daemon.task_reconciliation_interval = 0

            daemon.tick(now=1000)

            self.assertEqual(ledger.get("task-1").status, "lost")
            observe.emit.assert_any_call(
                "daemon_task_reconciliation",
                payload={"lost_tasks": 1, "older_than_seconds": 60},
            )

    def test_tick_cancels_jobs_for_terminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ledger = TaskLedger(db_path)
            jobs = JobService(db_path)
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="old task",
                runtime="coordinator",
                status="lost",
            )
            job = jobs.enqueue(
                kind="coordinator.autonomous_task",
                payload={"task_id": "task-1", "session_id": "s1"},
                resume_key="coordinator:task-1",
            )
            daemon, _, observe = self._make_daemon()
            daemon.task_ledger = ledger
            daemon.job_service = jobs

            daemon.tick(now=1000)

            cancelled = jobs.get(job.job_id)
            self.assertIsNotNone(cancelled)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.error, "orphaned_by_task:lost")
            observe.emit.assert_any_call(
                "daemon_job_reconciliation",
                payload={"cancelled_orphan_jobs": 1},
            )


class RecoveryJobDrainRunnerTests(unittest.TestCase):
    """C1 (2026-06-10 audit): recovery_jobs accumulated forever because
    resolve_recovery_job had no runtime caller — a false promise of continuity.
    The off-tick drainer surfaces each promised-but-abandoned request to the
    operator and marks it resolved, idempotently."""

    def _store(self) -> MemoryStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return MemoryStore(Path(tmp.name) / "claw.db")

    def test_notifies_and_resolves_each_pending_job(self) -> None:
        store = self._store()
        store.create_recovery_job(
            "sess-1",
            turn_id=None,
            failure_reason="image_error",
            original_request_sanitized="arregla el deploy de producción",
        )
        sent: list[str] = []
        drainer = RecoveryJobDrainRunner(memory=store, notifier=sent.append, min_age_seconds=0.0)

        self.assertEqual(drainer.run_once(), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("arregla el deploy", sent[0])
        self.assertEqual(store.list_pending_recovery_jobs(), [])
        # Idempotent: the cemetery is drained, so a second pass does nothing and
        # never re-notifies.
        self.assertEqual(drainer.run_once(), 0)
        self.assertEqual(len(sent), 1)

    def test_keeps_job_pending_when_notify_fails(self) -> None:
        # notify-then-resolve: if the operator notification fails, the job must
        # stay pending so the next cycle retries it — never silently dropped.
        store = self._store()
        store.create_recovery_job(
            "sess-1",
            turn_id=None,
            failure_reason="x",
            original_request_sanitized="haz la tarea pendiente",
        )

        def boom(_message: str) -> None:
            raise RuntimeError("telegram unreachable")

        drainer = RecoveryJobDrainRunner(memory=store, notifier=boom, min_age_seconds=0.0)
        self.assertEqual(drainer.run_once(), 0)
        self.assertEqual(len(store.list_pending_recovery_jobs()), 1)

    def test_query_fetches_at_most_the_requested_limit(self) -> None:
        # PR #90 review round 2 (codex P2): the per-cycle cap must bound the SQL
        # query, not just slice in Python — the stale backlog can be unbounded,
        # so materializing every row each cycle defeats the cap.
        store = self._store()
        for i in range(5):
            store.create_recovery_job(
                f"sess-{i}",
                turn_id=None,
                failure_reason="x",
                original_request_sanitized=f"r{i}",
            )
        self.assertEqual(len(store.list_pending_recovery_jobs(older_than_seconds=0.0, limit=2)), 2)

    def test_clamps_negative_inter_message_delay(self) -> None:
        # PR #90 review round 2 (gemini): a misconfigured negative delay would
        # raise ValueError inside time.sleep — clamp it to 0.
        drainer = RecoveryJobDrainRunner(
            memory=self._store(),
            notifier=lambda _m: None,
            inter_message_delay_seconds=-5.0,
        )
        self.assertEqual(drainer.inter_message_delay_seconds, 0.0)

    def test_does_not_drain_fresh_jobs(self) -> None:
        # PR #90 review (codex P2): the brain tells the user a recovery request
        # is queued "para retomarlo cuando el contexto se limpie", so a
        # just-created job must NOT be dismissed minutes later — only stale
        # backlog gets drained.
        store = self._store()
        store.create_recovery_job(
            "sess-1",
            turn_id=None,
            failure_reason="x",
            original_request_sanitized="pedido reciente",
        )
        sent: list[str] = []
        drainer = RecoveryJobDrainRunner(memory=store, notifier=sent.append, min_age_seconds=3600.0)
        self.assertEqual(drainer.run_once(), 0)
        self.assertEqual(sent, [])
        self.assertEqual(len(store.list_pending_recovery_jobs()), 1)

    def test_truncates_long_request_in_message(self) -> None:
        # PR #90 review (gemini): Telegram rejects messages over 4096 chars; an
        # unbounded request would brick the drain forever. Truncate defensively.
        store = self._store()
        store.create_recovery_job(
            "sess-1",
            turn_id=None,
            failure_reason="x",
            original_request_sanitized="A" * 1000,
        )
        sent: list[str] = []
        drainer = RecoveryJobDrainRunner(memory=store, notifier=sent.append, min_age_seconds=0.0)
        self.assertEqual(drainer.run_once(), 1)
        self.assertLess(len(sent[0]), 500)
        self.assertIn("...", sent[0])

    def test_caps_per_cycle_and_paces_between_sends(self) -> None:
        # PR #90 review (gemini): a large backlog must not block the thread or
        # blow Telegram's per-chat rate limit. Cap per cycle and pace sends.
        store = self._store()
        for i in range(3):
            store.create_recovery_job(
                f"sess-{i}",
                turn_id=None,
                failure_reason="x",
                original_request_sanitized=f"req{i}",
            )
        sent: list[str] = []
        slept: list[float] = []
        drainer = RecoveryJobDrainRunner(
            memory=store,
            notifier=sent.append,
            min_age_seconds=0.0,
            max_per_cycle=2,
            sleep=slept.append,
        )
        self.assertEqual(drainer.run_once(), 2)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(slept), 1)  # one pace between the two sends
        self.assertEqual(len(store.list_pending_recovery_jobs()), 1)  # 1 left for next cycle


class DaemonOffLoopEmitTests(unittest.IsolatedAsyncioTestCase):
    """PR #91 review (gemini): the M3 fix offloads diagnostic emits to a thread
    via asyncio.to_thread. If the emit raises (the very SQLite contention M3/M4
    address), the exception propagates out of `await` and TERMINATES the daemon
    loop. The offloaded emit must swallow failures so the loop survives."""

    async def test_emit_off_loop_swallows_emit_failure(self) -> None:
        observe = MagicMock()
        observe.emit.side_effect = sqlite3.OperationalError("database is locked")
        daemon = ClawDaemon(scheduler=CronScheduler(), heartbeat=MagicMock(), observe=observe)

        # Must NOT raise even though the underlying emit fails.
        await daemon._emit_off_loop("daemon_tick_error", payload={"x": 1})

        observe.emit.assert_called_once_with("daemon_tick_error", payload={"x": 1})

    async def test_emit_off_loop_noop_without_observe(self) -> None:
        daemon = ClawDaemon(scheduler=CronScheduler(), heartbeat=MagicMock(), observe=None)
        await daemon._emit_off_loop("daemon_tick_error", payload={"x": 1})  # no raise


class DaemonRunLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_loop_stops_on_shutdown(self) -> None:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat)
        shutdown = asyncio.Event()
        shutdown.set()
        await daemon.run_loop(shutdown, interval=0.01)

    async def test_run_loop_ticks_before_stopping(self) -> None:
        scheduler = CronScheduler()
        tick_count = 0
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat)
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()

        original_tick = daemon.tick

        def counting_tick(**kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                loop.call_soon_threadsafe(shutdown.set)
            return original_tick(**kwargs)

        daemon.tick = counting_tick
        await daemon.run_loop(shutdown, interval=0.01)
        self.assertGreaterEqual(tick_count, 2)

    async def test_run_loop_survives_tick_exception(self) -> None:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        observe = MagicMock()
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        call_count = 0

        def exploding_tick(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                loop.call_soon_threadsafe(shutdown.set)
            raise RuntimeError("boom")

        daemon.tick = exploding_tick
        await daemon.run_loop(shutdown, interval=0.01)
        self.assertGreaterEqual(call_count, 2)
        observe.emit.assert_called()

    async def test_run_loop_emits_liveness_while_tick_blocks(self) -> None:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        observe = MagicMock()
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe)
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_tick(**kwargs):
            time.sleep(0.05)
            loop.call_soon_threadsafe(shutdown.set)
            return TickResult(executed_jobs=[], heartbeat=heartbeat.collect.return_value)

        daemon.tick = slow_tick
        await daemon.run_loop(shutdown, interval=0.01)

        event_names = [call.args[0] for call in observe.emit.call_args_list]
        self.assertIn("daemon_heartbeat", event_names)

    async def test_run_loop_processes_pending_verification_job_outside_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = CronScheduler()
            heartbeat = MagicMock()
            heartbeat.collect.return_value = HeartbeatSnapshot(
                timestamp="t",
                pending_approvals=0,
                pending_approval_ids=[],
                agents={},
                lane_metrics={},
            )
            observe = MagicMock()
            ledger = MagicMock()
            ledger.mark_stale_running_lost.return_value = 0
            ledger.list.return_value = []
            jobs = JobService(Path(tmpdir) / "claw.db")
            daemon = ClawDaemon(
                scheduler=scheduler,
                heartbeat=heartbeat,
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
            )
            shutdown = asyncio.Event()
            loop = asyncio.get_running_loop()

            async def stop_after_job() -> None:
                deadline = loop.time() + 1.0
                while loop.time() < deadline:
                    rows = jobs.list(
                        kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,),
                        limit=10,
                    )
                    if rows and rows[0].status == "completed":
                        shutdown.set()
                        return
                    await asyncio.sleep(0.01)
                shutdown.set()

            await asyncio.gather(
                daemon.run_loop(shutdown, interval=0.01),
                stop_after_job(),
            )

            rows = jobs.list(kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,), limit=10)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "completed")
            ledger.list.assert_called_once_with(statuses=("completed_unverified",), limit=100)
            emitted = [call.args[0] for call in observe.emit.call_args_list]
            self.assertIn("pending_verification_reconciliation_enqueued", emitted)
            self.assertIn("pending_verification_reconciliation", emitted)
            self.assertIn("pending_verification_reconciliation_job_completed", emitted)


if __name__ == "__main__":
    unittest.main()
