"""Tests for the two-timeline transcript writer (Petri verifier commit #6)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.verification import (
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptStream,
    harness_stream_path,
    read_harness_stream,
    read_target_stream,
    record_harness_event,
    record_target_event,
    target_stream_path,
)


class TranscriptTests(unittest.TestCase):
    def test_target_and_harness_streams_live_in_separate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record_target_event(
                root,
                task_id="t-1",
                event_type="agent_message",
                payload={"text": "user-facing reply"},
            )
            record_harness_event(
                root,
                task_id="t-1",
                event_type="verifier_call",
                payload={"score": 0.9},
            )

            target_path = target_stream_path(root, "t-1")
            harness_path = harness_stream_path(root, "t-1")

            self.assertTrue(target_path.exists())
            self.assertTrue(harness_path.exists())
            self.assertNotEqual(target_path, harness_path)

            target_records = read_target_stream(root, "t-1")
            harness_records = read_harness_stream(root, "t-1")

            self.assertEqual(len(target_records), 1)
            self.assertEqual(len(harness_records), 1)
            self.assertEqual(target_records[0].stream, TranscriptStream.TARGET)
            self.assertEqual(harness_records[0].stream, TranscriptStream.HARNESS)
            self.assertEqual(target_records[0].event_type, "agent_message")
            self.assertEqual(harness_records[0].event_type, "verifier_call")

    def test_harness_event_does_not_appear_in_target_stream(self) -> None:
        """Hard requirement from spec section 4.4: the judge must not see
        harness records. Test the file-level isolation that backs that."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for i in range(3):
                record_harness_event(
                    root,
                    task_id="iso",
                    event_type="verifier_retry",
                    payload={"attempt": i},
                )

            target_records = read_target_stream(root, "iso")
            self.assertEqual(target_records, [])

            harness_records = read_harness_stream(root, "iso")
            self.assertEqual(len(harness_records), 3)

    def test_records_carry_v2_schema_version_and_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record_target_event(
                root,
                task_id="task-42",
                event_type="agent_message",
                payload={"text": "hi"},
            )
            records = read_target_stream(root, "task-42")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].schema_version, TRANSCRIPT_SCHEMA_VERSION)
            self.assertEqual(records[0].schema_version, "petri.transcript.v2")
            self.assertEqual(records[0].task_id, "task-42")

    def test_unsafe_chars_in_task_id_are_normalized_in_filename(self) -> None:
        """Telegram task_ids contain ``:`` and other separators that are not
        portable across filesystems. Make sure the writer normalizes them so
        we never collide with directory traversal or fail on Windows."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            task_id = "tg-574707975:foo/bar"
            record_target_event(
                root,
                task_id=task_id,
                event_type="agent_message",
                payload={"text": "hi"},
            )
            written = list(Path(tmpdir).glob("*-target.jsonl"))
            self.assertEqual(len(written), 1)
            self.assertNotIn(":", written[0].name)
            self.assertNotIn("/", written[0].name.replace("tg-574707975_foo_bar-target.jsonl", ""))

            recovered = read_target_stream(root, task_id)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].task_id, task_id)

    def test_read_returns_empty_for_missing_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.assertEqual(read_target_stream(root, "ghost"), [])
            self.assertEqual(read_harness_stream(root, "ghost"), [])

    def test_record_rejects_empty_task_id_and_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(ValueError):
                record_target_event(root, task_id="", event_type="x")
            with self.assertRaises(ValueError):
                record_target_event(root, task_id="t", event_type="")

    def test_two_tasks_do_not_share_streams(self) -> None:
        """One stream per task — task A's harness must not contaminate task B
        even when they're in the same telemetry root."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record_target_event(root, task_id="A", event_type="msg", payload={"t": "A"})
            record_harness_event(root, task_id="B", event_type="retry", payload={"t": "B"})

            self.assertEqual(len(read_target_stream(root, "A")), 1)
            self.assertEqual(read_target_stream(root, "B"), [])
            self.assertEqual(read_harness_stream(root, "A"), [])
            self.assertEqual(len(read_harness_stream(root, "B")), 1)


if __name__ == "__main__":
    unittest.main()
