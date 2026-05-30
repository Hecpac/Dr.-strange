from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon, TickResult
from claw_v2.heartbeat import HeartbeatSnapshot
from claw_v2.jobs import JobService
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

    def test_tick_emits_pending_verification_reconciliation_dry_run(self) -> None:
        # P1 Checkpoint A: the daemon must CALL the (already dry-run) reconciler
        # so pending_verification_reconciliation telemetry is emitted. Before
        # wiring there was no caller -> 0 events in production. Dry-run: the
        # ledger is only read (.list), never transitioned.
        from types import SimpleNamespace

        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="2026-01-01T00:00:00",
            pending_approvals=0,
            pending_approval_ids=[],
            agents={},
            lane_metrics={},
        )
        observe = MagicMock()
        ledger = MagicMock()
        ledger.mark_stale_running_lost.return_value = 0
        ledger.list.return_value = [
            SimpleNamespace(
                task_id="t1", channel="telegram", external_session_id="s",
                session_id="tg-1", verification_status="needs_verification",
                summary="ran cat", error="", completed_at=0.0,
                artifacts={"evidence_manifest": {"tools_run": ["Read"]}},
            ),
            SimpleNamespace(
                task_id="t2", channel="telegram", external_session_id="s",
                session_id="tg-1", verification_status="needs_verification",
                summary="wrote file", error="", completed_at=0.0,
                artifacts={"evidence_manifest": {"tools_run": ["Write"]}},
            ),
        ]
        daemon = ClawDaemon(scheduler=scheduler, heartbeat=heartbeat, observe=observe, task_ledger=ledger)

        daemon.tick(now=1_000_000)

        emitted = [call.args[0] for call in observe.emit.call_args_list]
        self.assertIn("pending_verification_reconciliation", emitted)
        ledger.list.assert_any_call(statuses=("completed_unverified",), limit=500)
        # Dry-run guarantee: Checkpoint A must not transition any row.
        ledger.mark_terminal.assert_not_called()

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
        # A1: a skipped reconciler run (interval not elapsed) must NOT report
        # pending_verification_unverified=0 as if it were a real backlog count.
        from types import SimpleNamespace

        ledger = MagicMock()
        ledger.mark_stale_running_lost.return_value = 0
        ledger.list.return_value = [
            SimpleNamespace(
                task_id="t1", channel="telegram", external_session_id="s",
                session_id="tg-1", verification_status="needs_verification",
                summary="x", error="", completed_at=0.0,
                artifacts={"evidence_manifest": {"tools_run": ["Read"]}},
            ),
        ]
        daemon, observe = self._make_daemon_with_ledger(ledger, pending_verification_interval=900)
        daemon.tick(now=1_000_000)   # reconciler runs
        daemon.tick(now=1_000_030)   # within interval -> skipped

        recon = [c for c in observe.emit.call_args_list if c.args[0] == "pending_verification_reconciliation"]
        self.assertEqual(len(recon), 1)  # only the first tick emitted it
        tick_payloads = [c.kwargs["payload"] for c in observe.emit.call_args_list if c.args[0] == "daemon_tick"]
        self.assertEqual(len(tick_payloads), 2)
        self.assertIn("pending_verification_unverified", tick_payloads[0])      # ran
        self.assertNotIn("pending_verification_unverified", tick_payloads[1])   # skipped -> omitted

    def test_reconciler_failure_does_not_crash_tick(self) -> None:
        # A1: a reconciler exception must be contained — scheduler and the rest of
        # the tick still run, and no ledger transition occurs.
        ledger = MagicMock()
        ledger.mark_stale_running_lost.return_value = 0
        ledger.list.side_effect = RuntimeError("boom")  # build_reconciliation_report raises
        daemon, observe = self._make_daemon_with_ledger(ledger)
        probe = MagicMock()
        daemon.scheduler.register(ScheduledJob(name="probe", interval_seconds=60, handler=probe))

        result = daemon.tick(now=1_000_000)  # must not raise

        self.assertIn("probe", result.executed_jobs)
        probe.assert_called_once()
        ledger.mark_terminal.assert_not_called()
        recon = [c for c in observe.emit.call_args_list if c.args[0] == "pending_verification_reconciliation"]
        self.assertEqual(recon, [])  # raised before the emit

    def test_tick_does_not_call_heartbeat_emit(self) -> None:
        daemon, heartbeat, _ = self._make_daemon()
        daemon.tick(now=1000)
        heartbeat.emit.assert_not_called()
        heartbeat.collect.assert_called_once()

    def test_tick_runs_scheduled_jobs(self) -> None:
        daemon, _, _ = self._make_daemon()
        handler = MagicMock()
        daemon.scheduler.register(ScheduledJob(name="test_job", interval_seconds=60, handler=handler))
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


class DaemonRunLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_loop_stops_on_shutdown(self) -> None:
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = HeartbeatSnapshot(
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
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
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
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
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
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
            timestamp="t", pending_approvals=0, pending_approval_ids=[], agents={}, lane_metrics={},
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


if __name__ == "__main__":
    unittest.main()
