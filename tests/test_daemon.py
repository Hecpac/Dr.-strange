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
