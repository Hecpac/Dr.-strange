"""Wave 3.5: defense-in-depth scrub of system-reminder markers.

Three layers must scrub the marker:
1. Chat output sanitizer (existing in bot_helpers).
2. Audit emit (observe.emit).
3. Memory store_message.

These tests cover layers 2-3 plus the shared helper. A regression that
removes scrubbing from any of them must turn red here.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.leak_scrub import redact_system_reminders, scrub_for_persistence
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream


class RedactSystemRemindersTests(unittest.TestCase):
    def test_redacts_basic_open_close_markers(self) -> None:
        value = "before <system-reminder>secret</system-reminder> after"
        out = redact_system_reminders(value)
        self.assertNotIn("<system-reminder>", out)
        self.assertNotIn("</system-reminder>", out)
        self.assertNotIn("secret", out)
        self.assertIn("[redacted: system-reminder]", out)

    def test_redacts_html_entity_encoded_markers(self) -> None:
        value = "&lt;system-reminder&gt;hidden&lt;/system-reminder&gt;"
        out = redact_system_reminders(value)
        self.assertNotIn("&lt;system-reminder&gt;", out)
        self.assertNotIn("&lt;/system-reminder&gt;", out)
        self.assertNotIn("hidden", out)

    def test_idempotent_on_clean_text(self) -> None:
        value = "completely normal text without markers"
        self.assertEqual(redact_system_reminders(value), value)

    def test_scrub_for_persistence_walks_nested_structures(self) -> None:
        payload = {
            "text": "leaked <system-reminder>x</system-reminder> here",
            "nested": [
                "<system-reminder>1</system-reminder>",
                {"deep": "deep <system-reminder>2</system-reminder>"},
            ],
            "untouched_int": 42,
            "untouched_none": None,
        }
        cleaned = scrub_for_persistence(payload)
        serialized = json.dumps(cleaned)
        self.assertNotIn("<system-reminder>", serialized)
        self.assertNotIn("</system-reminder>", serialized)
        self.assertEqual(cleaned["untouched_int"], 42)
        self.assertIsNone(cleaned["untouched_none"])

    def test_scrub_handles_tuples(self) -> None:
        cleaned = scrub_for_persistence(("ok", "<system-reminder>x</system-reminder>"))
        self.assertEqual(cleaned[0], "ok")
        self.assertNotIn("<system-reminder>", cleaned[1])
        self.assertNotIn("</system-reminder>", cleaned[1])


class ObserveEmitScrubTests(unittest.TestCase):
    def test_emit_scrubs_system_reminder_from_payload_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "audit.db"
            stream = ObserveStream(db)
            stream.emit(
                "dispatch_decision",
                payload={
                    "session_id": "s1",
                    "text_preview": "user said <system-reminder>poison</system-reminder> stuff",
                    "nested": {"reason": "<system-reminder>x</system-reminder>"},
                },
            )

            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT payload FROM observe_stream WHERE event_type = 'dispatch_decision'"
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            persisted = row[0]
            self.assertNotIn("system-reminder>", persisted)
            self.assertNotIn("&lt;system-reminder", persisted)
            self.assertIn("[redacted: system-reminder]", persisted)


class MemoryStoreMessageScrubTests(unittest.TestCase):
    def test_store_message_scrubs_system_reminder_from_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "memory.db"
            store = MemoryStore(db)
            store.store_message(
                "session-x",
                "user",
                "see <system-reminder>internal</system-reminder> here",
            )
            recent = store.get_recent_messages("session-x", limit=10)
            self.assertEqual(len(recent), 1)
            content = recent[0]["content"]
            self.assertNotIn("<system-reminder>", content)
            self.assertNotIn("</system-reminder>", content)
            self.assertIn("[redacted: system-reminder]", content)


if __name__ == "__main__":
    unittest.main()
