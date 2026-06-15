from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2 import liveness
from claw_v2.cron import CronScheduler
from claw_v2.daemon import ClawDaemon
from claw_v2.heartbeat import HeartbeatSnapshot


def _snapshot() -> HeartbeatSnapshot:
    return HeartbeatSnapshot(
        timestamp="t",
        pending_approvals=0,
        pending_approval_ids=[],
        agents={},
        lane_metrics={},
    )


class LivenessSinkModuleTests(unittest.TestCase):
    def test_sink_path_is_data_dir_plus_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            self.assertEqual(path, Path(tmpdir) / liveness.LIVENESS_SINK_FILENAME)

    def test_write_then_read_roundtrips_a_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            payload = {"pid": 1, "ts": 2.0, "web_transport_serving": True}
            liveness.write_liveness(path, payload)
            self.assertEqual(liveness.read_liveness(path), payload)

    def test_write_overwrites_rather_than_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            liveness.write_liveness(path, {"n": 1})
            liveness.write_liveness(path, {"n": 2})
            # A single JSON object on disk (overwrite), not two concatenated.
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"n": 2})
            self.assertEqual(liveness.read_liveness(path), {"n": 2})

    def test_write_leaves_no_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            liveness.write_liveness(path, {"n": 1})
            leftovers = [p.name for p in Path(tmpdir).iterdir() if p.name != path.name]
            self.assertEqual(leftovers, [])

    def test_read_missing_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            self.assertIsNone(liveness.read_liveness(path))

    def test_read_invalid_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(liveness.read_liveness(path))

    def test_read_non_dict_json_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertIsNone(liveness.read_liveness(path))

    def test_read_byte_corrupted_sink_returns_none(self) -> None:
        # Ultra #117 finding: invalid UTF-8 bytes raise UnicodeDecodeError (a
        # ValueError, NOT an OSError). read_liveness must degrade to None, not
        # let it escape into the diagnostics/watchdog health path.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = liveness.liveness_sink_path(tmpdir)
            path.write_bytes(b"\xff\xfe\x00\x80not utf-8")
            self.assertIsNone(liveness.read_liveness(path))


class _FakeWebTransport:
    def __init__(self, serving: bool) -> None:
        self._serving = serving

    def is_serving(self) -> bool:
        return self._serving


class LifecycleHeartbeatWriterTests(unittest.TestCase):
    """Criterion 1: the lifecycle heartbeat writer writes a sink record that
    CONTAINS web_transport_serving.
    """

    def test_writer_writes_sink_with_web_transport_serving(self) -> None:
        from claw_v2.lifecycle import write_liveness_heartbeat_record

        with tempfile.TemporaryDirectory() as tmpdir:
            sink = liveness.liveness_sink_path(tmpdir)
            web = _FakeWebTransport(serving=True)
            write_liveness_heartbeat_record(
                sink_path=sink,
                boot_id="boot-abc",
                web_transport=web,
                web_chat_enabled=True,
            )
            record = liveness.read_liveness(sink)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertIn("web_transport_serving", record)
            self.assertEqual(record["web_transport_serving"], True)
            self.assertEqual(record["boot_id"], "boot-abc")
            self.assertEqual(record["pid"], os.getpid())
            self.assertEqual(record["source"], "lifecycle")
            self.assertIsInstance(record["ts"], (int, float))

    def test_writer_records_none_when_web_chat_disabled(self) -> None:
        from claw_v2.lifecycle import write_liveness_heartbeat_record

        with tempfile.TemporaryDirectory() as tmpdir:
            sink = liveness.liveness_sink_path(tmpdir)
            web = _FakeWebTransport(serving=True)
            write_liveness_heartbeat_record(
                sink_path=sink,
                boot_id="boot-abc",
                web_transport=web,
                web_chat_enabled=False,
            )
            record = liveness.read_liveness(sink)
            assert record is not None
            self.assertIsNone(record["web_transport_serving"])

    def test_seed_writes_fresh_record_with_none_web_state(self) -> None:
        """Criterion 5 (seed): the first-boot seed writes a record WITHOUT
        calling is_serving() (web_transport_serving=None) so an old/stale sink
        is replaced at startup."""
        from claw_v2.lifecycle import seed_liveness_heartbeat_record

        with tempfile.TemporaryDirectory() as tmpdir:
            sink = liveness.liveness_sink_path(tmpdir)
            # Pretend a stale record from a previous boot is on disk.
            liveness.write_liveness(
                sink,
                {
                    "pid": 999999,
                    "ts": time.time() - 10_000,
                    "boot_id": "old-boot",
                    "web_transport_serving": True,
                    "source": "lifecycle",
                },
            )
            web = _FakeWebTransport(serving=True)
            calls: list[str] = []

            class _SpyWeb(_FakeWebTransport):
                def is_serving(self) -> bool:  # pragma: no cover - must not run
                    calls.append("is_serving")
                    return super().is_serving()

            seed_liveness_heartbeat_record(
                sink_path=sink,
                boot_id="new-boot",
                web_transport=_SpyWeb(serving=True),
                web_chat_enabled=True,
            )
            record = liveness.read_liveness(sink)
            assert record is not None
            self.assertEqual(record["boot_id"], "new-boot")
            self.assertIsNone(record["web_transport_serving"])
            # Seed must NOT probe is_serving (transport may not be up yet).
            self.assertEqual(calls, [])
            del web


class DaemonLivenessLoopSamplingTests(unittest.IsolatedAsyncioTestCase):
    """Criterion 3 (loop) + Criterion 4: the daemon liveness loop is sampled
    and never writes the sink."""

    async def test_liveness_loop_samples_emits_below_cycle_count(self) -> None:
        observe = MagicMock()
        scheduler = CronScheduler()
        heartbeat = MagicMock()
        heartbeat.collect.return_value = _snapshot()
        sample = 15
        daemon = ClawDaemon(
            scheduler=scheduler,
            heartbeat=heartbeat,
            observe=observe,
            liveness_emit_sample=sample,
        )
        shutdown = asyncio.Event()
        cycles = 0
        target = 40

        async def counting_wait() -> bool:
            # Stand in for ``shutdown.wait()`` inside the loop's wait_for. Count
            # each cycle and trip the event once the target is reached so the
            # loop terminates deterministically without sleeping.
            nonlocal cycles
            cycles += 1
            if cycles >= target:
                shutdown.set()
            return True

        shutdown.wait = counting_wait  # type: ignore[assignment]
        await daemon._run_liveness_heartbeat_loop(shutdown, interval=10)

        emits = [c for c in observe.emit.call_args_list if c.args[0] == "daemon_heartbeat"]
        # Sampled: far fewer than the number of cycles we drove.
        self.assertGreater(len(emits), 0)
        self.assertLess(len(emits), cycles)
        self.assertLessEqual(len(emits), cycles // sample + 1)

    async def test_liveness_loop_never_writes_the_sink(self) -> None:
        # Criterion 4: lifecycle wrote web_transport_serving=True; the daemon
        # loop must NOT clobber it (it must not touch the sink at all).
        with tempfile.TemporaryDirectory() as tmpdir:
            sink = liveness.liveness_sink_path(tmpdir)
            liveness.write_liveness(
                sink,
                {
                    "pid": 1,
                    "ts": time.time(),
                    "boot_id": "b",
                    "web_transport_serving": True,
                    "source": "lifecycle",
                },
            )
            observe = MagicMock()
            heartbeat = MagicMock()
            heartbeat.collect.return_value = _snapshot()
            daemon = ClawDaemon(
                scheduler=CronScheduler(),
                heartbeat=heartbeat,
                observe=observe,
                liveness_emit_sample=1,
            )
            shutdown = asyncio.Event()
            cycles = 0

            async def counting_wait() -> bool:
                nonlocal cycles
                cycles += 1
                if cycles >= 3:
                    shutdown.set()
                return True

            shutdown.wait = counting_wait  # type: ignore[assignment]
            await daemon._run_liveness_heartbeat_loop(shutdown, interval=10)

            record = liveness.read_liveness(sink)
            assert record is not None
            self.assertEqual(record["web_transport_serving"], True)
            self.assertEqual(record["source"], "lifecycle")


class DaemonTickSamplingTests(unittest.TestCase):
    """Criterion 3 (tick): idle ticks emit daemon_tick far fewer than M times."""

    def _make_daemon(self, *, tick_emit_sample: int) -> tuple[ClawDaemon, MagicMock]:
        heartbeat = MagicMock()
        heartbeat.collect.return_value = _snapshot()
        observe = MagicMock()
        daemon = ClawDaemon(
            scheduler=CronScheduler(),
            heartbeat=heartbeat,
            observe=observe,
            tick_emit_sample=tick_emit_sample,
        )
        return daemon, observe

    def test_idle_ticks_are_sampled(self) -> None:
        sample = 30
        daemon, observe = self._make_daemon(tick_emit_sample=sample)
        m = 90
        for i in range(m):
            daemon.tick(now=1000 + i)
        tick_emits = [c for c in observe.emit.call_args_list if c.args[0] == "daemon_tick"]
        self.assertGreater(len(tick_emits), 0)
        self.assertLess(len(tick_emits), m)
        self.assertLessEqual(len(tick_emits), m // sample + 1)

    def test_meaningful_tick_always_emits(self) -> None:
        # A tick with reconciliation work must always emit regardless of sample.
        sample = 1000
        daemon, observe = self._make_daemon(tick_emit_sample=sample)
        ledger = MagicMock()
        ledger.mark_stale_running_lost.return_value = 3
        ledger.list.return_value = []
        daemon.task_ledger = ledger
        daemon.task_reconciliation_interval = 0
        daemon.stale_task_seconds = 60
        daemon.tick(now=1000)
        tick_emits = [c for c in observe.emit.call_args_list if c.args[0] == "daemon_tick"]
        self.assertEqual(len(tick_emits), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
