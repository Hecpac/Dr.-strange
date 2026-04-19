"""Tests for CheckpointService — DB snapshot primitive."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.checkpoint import CheckpointService
from claw_v2.memory import MemoryStore


class CheckpointCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.snapshots_dir = tmp / "snapshots"
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=self.snapshots_dir,
        )

    def test_create_returns_ckpt_id_with_expected_format(self) -> None:
        ckpt_id = self.service.create(trigger_reason="test")
        self.assertTrue(ckpt_id.startswith("ckpt_"))
        self.assertEqual(len(ckpt_id), 13)
        self.assertTrue(all(c in "0123456789abcdef" for c in ckpt_id[5:]))

    def test_create_writes_snapshot_file_to_disk(self) -> None:
        ckpt_id = self.service.create(trigger_reason="test")
        expected = self.snapshots_dir / f"{ckpt_id}.db"
        self.assertTrue(expected.exists())
        self.assertGreater(expected.stat().st_size, 0)

    def test_create_inserts_metadata_row(self) -> None:
        self.store.store_fact("before_snapshot", "value", source="test")
        ckpt_id = self.service.create(
            trigger_reason="pre-action",
            session_id="s1",
            consecutive_failures=2,
        )
        row = self.store._conn.execute(
            "SELECT ckpt_id, trigger_reason, session_id, consecutive_failures, "
            "file_path, pending_restore, restored_at "
            "FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["ckpt_id"], ckpt_id)
        self.assertEqual(row["trigger_reason"], "pre-action")
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["consecutive_failures"], 2)
        self.assertEqual(row["pending_restore"], 0)
        self.assertIsNone(row["restored_at"])

    def test_snapshot_file_contains_source_data(self) -> None:
        self.store.store_fact("my_key", "my_value", source="test")
        ckpt_id = self.service.create(trigger_reason="test")
        snap_path = self.snapshots_dir / f"{ckpt_id}.db"
        snap_conn = sqlite3.connect(snap_path)
        try:
            row = snap_conn.execute(
                "SELECT value FROM facts WHERE key = 'my_key'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "my_value")
        finally:
            snap_conn.close()

    def test_backup_invoked_with_expected_args(self) -> None:
        captured = {}
        real_conn = self.store._conn

        class ConnProxy:
            def __init__(self, inner):
                self._inner = inner
            def backup(self, target, **kwargs):
                captured["pages"] = kwargs.get("pages")
                captured["sleep"] = kwargs.get("sleep")
                return self._inner.backup(target, **kwargs)
            def __getattr__(self, name):
                return getattr(self._inner, name)

        with patch.object(self.store, "_conn", ConnProxy(real_conn)):
            self.service.create(trigger_reason="test")
        self.assertEqual(captured["pages"], 100)
        self.assertEqual(captured["sleep"], 0.001)

    def test_create_failure_cleans_up_file(self) -> None:
        real_conn = self.store._conn

        class FailingConnProxy:
            def __init__(self, inner):
                self._inner = inner
            def backup(self, target, **kwargs):
                raise sqlite3.OperationalError("simulated failure")
            def __getattr__(self, name):
                return getattr(self._inner, name)

        with patch.object(self.store, "_conn", FailingConnProxy(real_conn)):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.create(trigger_reason="test")
        self.assertEqual(list(self.snapshots_dir.glob("ckpt_*.db")), [])
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 0)
