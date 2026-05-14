from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.cli.think import (
    _filter_events,
    _format_event,
    _payload_summary,
    _short_ts,
    main,
)
from claw_v2.observe import ObserveStream


class FormatterTests(unittest.TestCase):
    def test_short_ts_handles_iso_with_T_separator(self) -> None:
        self.assertEqual(_short_ts("2026-05-10T11:47:32.123456"), "11:47:32")

    def test_short_ts_handles_space_separator(self) -> None:
        self.assertEqual(_short_ts("2026-05-10 11:47:32"), "11:47:32")

    def test_short_ts_handles_unknown_formats_gracefully(self) -> None:
        self.assertEqual(_short_ts(""), "")
        self.assertEqual(_short_ts(None), "")
        self.assertEqual(_short_ts("garbage"), "garbage")

    def test_dispatch_decision_summary_includes_handler_route_reason(self) -> None:
        payload = {
            "session_id": "sess-abcdef12",
            "handler": "task_intent",
            "route": "intercepted",
            "reason": "task_intent_matched",
            "text_preview": "hola dale procede",
        }
        line = _payload_summary("dispatch_decision", payload, {})
        self.assertIn("handler=task_intent", line)
        self.assertIn("route=intercepted", line)
        self.assertIn("reason=task_intent_matched", line)
        self.assertIn("hola dale procede", line)

    def test_dispatch_decision_summary_shows_matched_pattern_when_present(self) -> None:
        payload = {
            "handler": "shortcut",
            "route": "intercepted",
            "reason": "shortcut_matched",
            "matched_pattern": "shortcut.url_extract",
            "text_preview": "https://example.com",
        }
        line = _payload_summary("dispatch_decision", payload, {})
        self.assertIn("matched=shortcut.url_extract", line)

    def test_kairos_decide_failed_summary_shows_kind_and_error(self) -> None:
        payload = {"error_kind": "codex_timeout", "error": "Codex CLI timed out after 300.0s"}
        line = _payload_summary("kairos_decide_failed", payload, {})
        self.assertIn("kind=codex_timeout", line)
        self.assertIn("Codex CLI timed out", line)

    def test_circuit_breaker_tripped_summary(self) -> None:
        payload = {"breaker": "cost_per_hour", "value": 10.5, "threshold": 10.0, "actor": "brain"}
        line = _payload_summary("circuit_breaker_tripped", payload, {})
        self.assertIn("breaker=cost_per_hour", line)
        self.assertIn("value=10.5", line)
        self.assertIn("threshold=10.0", line)

    def test_format_event_emits_consistent_columns(self) -> None:
        event = {
            "event_type": "dispatch_decision",
            "timestamp": "2026-05-10T11:47:32",
            "payload": {
                "session_id": "sess-12345678",
                "handler": "shortcut",
                "route": "intercepted",
                "reason": "shortcut_matched",
                "text_preview": "abc",
            },
        }
        line = _format_event(event)
        self.assertIn("11:47:32", line)
        self.assertIn("dispatch_decision", line)
        self.assertIn("12345678", line)


class FilterEventsTests(unittest.TestCase):
    def test_filter_by_event_type(self) -> None:
        events = [
            {"event_type": "dispatch_decision", "payload": {}},
            {"event_type": "llm_response", "payload": {}},
            {"event_type": "dispatch_decision", "payload": {}},
        ]
        filtered = list(_filter_events(events, event_type="dispatch_decision", session=None))
        self.assertEqual(len(filtered), 2)

    def test_filter_by_session(self) -> None:
        events = [
            {"event_type": "x", "payload": {"session_id": "abc"}},
            {"event_type": "x", "payload": {"session_id": "def"}},
            {"event_type": "x", "payload": {"session_id": "abc"}},
        ]
        filtered = list(_filter_events(events, event_type=None, session="abc"))
        self.assertEqual(len(filtered), 2)

    def test_filter_combined_type_and_session(self) -> None:
        events = [
            {"event_type": "a", "payload": {"session_id": "1"}},
            {"event_type": "a", "payload": {"session_id": "2"}},
            {"event_type": "b", "payload": {"session_id": "1"}},
        ]
        filtered = list(_filter_events(events, event_type="a", session="1"))
        self.assertEqual(len(filtered), 1)


class EndToEndTailTests(unittest.TestCase):
    def test_main_tail_prints_events_from_real_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            stream = ObserveStream(db_path)
            stream.emit("dispatch_decision", payload={"session_id": "sess-x", "handler": "shortcut", "route": "intercepted", "reason": "matched", "text_preview": "go"})
            stream.emit("llm_response", payload={"session_id": "sess-x", "cost_estimate": 0.01})

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = main(["--db", str(db_path), "tail", "--limit", "10"])
            self.assertEqual(rc, 0)
            output = buf.getvalue()
            self.assertIn("dispatch_decision", output)
            self.assertIn("llm_response", output)
            self.assertIn("shortcut", output)


if __name__ == "__main__":
    unittest.main()
