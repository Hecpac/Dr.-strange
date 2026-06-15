from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.observe import OBSERVE_SQLITE_BUSY_TIMEOUT_MS, ObserveStream


class ObserveSubscribeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stream = ObserveStream(Path(self._tmp.name) / "observe.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_subscribe_callback_invoked_on_emit(self) -> None:
        received: list[dict] = []
        self.stream.subscribe("daemon_health_check_notification", received.append)
        self.stream.emit("daemon_health_check_notification", payload={"status": "ok"})
        self.assertEqual(received, [{"status": "ok"}])

    def test_subscribe_callback_not_invoked_for_other_events(self) -> None:
        received: list[dict] = []
        self.stream.subscribe("daemon_health_check_notification", received.append)
        self.stream.emit("unrelated_event", payload={"x": 1})
        self.assertEqual(received, [])

    def test_subscribe_callback_exception_does_not_break_emit(self) -> None:
        def boom(_payload: dict) -> None:
            raise RuntimeError("subscriber failed")

        good_received: list[dict] = []
        self.stream.subscribe("evt", boom)
        self.stream.subscribe("evt", good_received.append)
        self.stream.emit("evt", payload={"n": 1})
        self.assertEqual(good_received, [{"n": 1}])

    def test_locked_database_drops_event_without_breaking_emit(self) -> None:
        # PR #91 (M3/M4): the diagnostic write is dropped after retries, but
        # in-process subscribers still fire — a transient SQLite lock must not
        # swallow task-completion notifications wired as subscribers.
        received: list[dict] = []
        self.stream.subscribe("evt", received.append)
        fake_conn = MagicMock()
        fake_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        self.stream._conn = fake_conn

        with patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0):
            self.stream.emit("evt", payload={"n": 1})

        self.assertEqual(fake_conn.execute.call_count, 3)
        self.assertGreaterEqual(fake_conn.rollback.call_count, 1)
        self.assertEqual(received, [{"n": 1}])
        # AM-OBSDROP (2026-06-12): a dropped event must leave a recoverable
        # JSONL trace next to the DB instead of vanishing.
        spill = self.stream.db_path.with_suffix(".spill.jsonl")
        self.assertTrue(spill.exists())
        import json as _json

        spilled = _json.loads(spill.read_text().splitlines()[-1])
        self.assertEqual(spilled["event_type"], "evt")
        self.assertEqual(_json.loads(spilled["payload"]), {"n": 1})

    def test_emit_persists_even_when_no_subscribers(self) -> None:
        self.stream.emit("lonely_event", payload={"a": 1})
        events = self.stream.recent_events(limit=1)
        self.assertEqual(events[0]["event_type"], "lonely_event")
        self.assertEqual(events[0]["payload"], {"a": 1})

    def test_observe_connection_uses_short_busy_timeout(self) -> None:
        busy_timeout = self.stream._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(busy_timeout, OBSERVE_SQLITE_BUSY_TIMEOUT_MS)

    def test_observe_indexes_are_created(self) -> None:
        rows = self.stream._conn.execute("PRAGMA index_list(observe_stream)").fetchall()
        names = {row[1] for row in rows}
        self.assertIn("idx_observe_stream_event_time", names)
        self.assertIn("idx_observe_stream_trace_id_id", names)
        self.assertIn("idx_observe_stream_job_id_id", names)
        self.assertIn("idx_observe_stream_root_trace_id_id", names)

    def test_job_events_filters_and_orders_by_job_id(self) -> None:
        self.stream.emit("other_started", job_id="job-2", payload={"step": 0})
        self.stream.emit("job_started", job_id="job-1", artifact_id="plan:1", payload={"step": 1})
        self.stream.emit(
            "job_completed", job_id="job-1", artifact_id="outcome:1", payload={"step": 2}
        )

        events = self.stream.job_events("job-1")

        self.assertEqual(
            [event["event_type"] for event in events], ["job_started", "job_completed"]
        )
        self.assertEqual([event["payload"]["step"] for event in events], [1, 2])
        self.assertEqual(events[0]["artifact_id"], "plan:1")

    def test_multiple_subscribers_all_invoked(self) -> None:
        a: list[dict] = []
        b: list[dict] = []
        self.stream.subscribe("evt", a.append)
        self.stream.subscribe("evt", b.append)
        self.stream.emit("evt", payload={"k": "v"})
        self.assertEqual(a, [{"k": "v"}])
        self.assertEqual(b, [{"k": "v"}])


if __name__ == "__main__":
    unittest.main()
