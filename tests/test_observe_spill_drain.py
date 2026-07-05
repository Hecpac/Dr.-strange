from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import RuntimeDb


def _spill_line(
    event_type: str,
    payload: dict,
    *,
    dropped_at: float = 123.0,
    trace_id: str | None = None,
) -> str:
    record = {
        "dropped_at": dropped_at,
        "event_type": event_type,
        "payload": json.dumps(payload),
    }
    if trace_id is not None:
        record["trace_id"] = trace_id
    return json.dumps(record, sort_keys=True)


class ObserveSpillDrainTests(unittest.TestCase):
    def test_drain_spill_inserts_events_and_removes_durable_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            spill_path = observe.db_path.with_suffix(".spill.jsonl")
            spill_path.write_text(
                _spill_line("spilled_event", {"n": 1}, trace_id="trace-1") + "\n",
                encoding="utf-8",
            )

            result = observe.drain_spill()

            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.remaining_lines, 0)
            self.assertFalse(spill_path.exists())
            rows = observe.trace_events("trace-1")
            self.assertEqual([row["event_type"] for row in rows], ["spilled_event"])
            self.assertEqual(rows[0]["payload"]["n"], 1)
            with observe._lock:
                markers = observe._conn.execute(
                    "SELECT COUNT(*) FROM observe_spill_drain"
                ).fetchone()
            self.assertEqual(markers[0], 1)

    def test_drain_spill_replay_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            spill_path = observe.db_path.with_suffix(".spill.jsonl")
            raw = _spill_line("spilled_event", {"n": 1})
            spill_path.write_text(raw + "\n", encoding="utf-8")
            first = observe.drain_spill()
            self.assertEqual(first.inserted, 1)

            spill_path.write_text(raw + "\n", encoding="utf-8")
            second = observe.drain_spill()

            self.assertEqual(second.already_present, 1)
            with observe._lock:
                count = observe._conn.execute(
                    "SELECT COUNT(*) FROM observe_stream WHERE event_type = 'spilled_event'"
                ).fetchone()[0]
            self.assertEqual(count, 1)
            self.assertFalse(spill_path.exists())

    def test_drain_spill_preserves_malformed_lines_and_drains_valid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            spill_path = observe.db_path.with_suffix(".spill.jsonl")
            malformed = "{not-json"
            spill_path.write_text(
                malformed + "\n" + _spill_line("valid_spill", {"ok": True}) + "\n",
                encoding="utf-8",
            )

            result = observe.drain_spill()

            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.malformed, 1)
            self.assertEqual(spill_path.read_text(encoding="utf-8"), malformed + "\n")
            events = observe.recent_events(limit=5, event_type="valid_spill")
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["payload"]["ok"])

    def test_drain_spill_leaves_file_untouched_when_runtime_db_lock_is_contended(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = RuntimeDb(Path(tmpdir) / "observe.db")
            self.addCleanup(db.close)
            observe = ObserveStream(db.db_path, runtime_db=db)
            spill_path = observe.db_path.with_suffix(".spill.jsonl")
            raw = _spill_line("contended_spill", {"n": 1})
            spill_path.write_text(raw + "\n", encoding="utf-8")

            @contextlib.contextmanager
            def busy_try_acquire():
                yield False

            with patch.object(db, "try_acquire", busy_try_acquire):
                with patch("claw_v2.observe.OBSERVE_LOCKED_RETRY_DELAY_SECONDS", 0):
                    result = observe.drain_spill(max_attempts=2)

            self.assertEqual(result.failed, 1)
            self.assertEqual(result.inserted, 0)
            self.assertEqual(spill_path.read_text(encoding="utf-8"), raw + "\n")
            with db.cursor() as cur:
                count = cur.execute(
                    "SELECT COUNT(*) FROM observe_stream WHERE event_type = 'contended_spill'"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_drain_spill_keeps_lines_that_fail_before_durable_insert(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            spill_path = observe.db_path.with_suffix(".spill.jsonl")
            first = _spill_line("first_spill", {"n": 1})
            second = _spill_line("second_spill", {"n": 2})
            spill_path.write_text(first + "\n" + second + "\n", encoding="utf-8")
            real_insert = observe._insert_spill_record_locked

            def fail_second(record):
                if record.event_type == "second_spill":
                    return "failed"
                return real_insert(record)

            with patch.object(observe, "_insert_spill_record_locked", side_effect=fail_second):
                result = observe.drain_spill()

            self.assertEqual(result.inserted, 1)
            self.assertEqual(result.failed, 1)
            self.assertEqual(spill_path.read_text(encoding="utf-8"), second + "\n")
            events = observe.recent_events(limit=5, event_type="first_spill")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["n"], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
