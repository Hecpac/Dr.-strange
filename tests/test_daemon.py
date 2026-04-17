from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from claw_v2.cron import CronScheduler, ScheduledJob
from claw_v2.daemon import ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot


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

        original_tick = daemon.tick

        def counting_tick(**kwargs):
            nonlocal tick_count
            tick_count += 1
            if tick_count >= 2:
                shutdown.set()
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
        call_count = 0

        def exploding_tick(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shutdown.set()
            raise RuntimeError("boom")

        daemon.tick = exploding_tick
        await daemon.run_loop(shutdown, interval=0.01)
        self.assertGreaterEqual(call_count, 2)
        observe.emit.assert_called()


if __name__ == "__main__":
    unittest.main()
