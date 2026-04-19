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


class CheckpointRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.snapshots_dir = tmp / "snapshots"
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=self.snapshots_dir, ring_size=10,
        )

    def test_ring_keeps_exactly_N_snapshots(self) -> None:
        created: list[str] = []
        for i in range(11):
            created.append(self.service.create(trigger_reason=f"ckpt-{i}"))
        files = sorted(self.snapshots_dir.glob("ckpt_*.db"))
        self.assertEqual(len(files), 10)
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 10)

    def test_oldest_snapshot_is_purged_first(self) -> None:
        created: list[str] = []
        for i in range(11):
            created.append(self.service.create(trigger_reason=f"ckpt-{i}"))
        first_ckpt_id = created[0]
        row = self.store._conn.execute(
            "SELECT ckpt_id FROM checkpoints WHERE ckpt_id = ?",
            (first_ckpt_id,),
        ).fetchone()
        self.assertIsNone(row)
        first_path = self.snapshots_dir / f"{first_ckpt_id}.db"
        self.assertFalse(first_path.exists())
        for ckpt_id in created[1:]:
            self.assertTrue((self.snapshots_dir / f"{ckpt_id}.db").exists())

    def test_custom_ring_size_respected(self) -> None:
        svc = CheckpointService(
            memory=self.store,
            snapshots_dir=Path(tempfile.mkdtemp()),
            ring_size=3,
        )
        for i in range(5):
            svc.create(trigger_reason=f"t-{i}")
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 3)

    def test_rotation_failure_does_not_rollback_new_snapshot(self) -> None:
        # Prime with 10 snapshots.
        for i in range(10):
            self.service.create(trigger_reason=f"base-{i}")
        # Force Path.unlink to fail on the next rotation attempt (the oldest file).
        original_unlink = Path.unlink
        def fail_on_unlink(self, *args, **kwargs):
            if self.name.startswith("ckpt_") and self.suffix == ".db":
                raise OSError("simulated rotation failure")
            return original_unlink(self, *args, **kwargs)
        with patch.object(Path, "unlink", fail_on_unlink):
            new_ckpt = self.service.create(trigger_reason="new")
        # The new ckpt must still exist in the DB (rotation failure is non-fatal).
        row = self.store._conn.execute(
            "SELECT ckpt_id FROM checkpoints WHERE ckpt_id = ?", (new_ckpt,),
        ).fetchone()
        self.assertIsNotNone(row)


class CheckpointListTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=tmp / "snapshots",
        )

    def test_list_empty(self) -> None:
        self.assertEqual(self.service.list(), [])

    def test_latest_returns_none_when_empty(self) -> None:
        self.assertIsNone(self.service.latest())

    def test_list_ordered_desc_by_created_at(self) -> None:
        ids = [self.service.create(trigger_reason=f"t-{i}") for i in range(3)]
        rows = self.service.list()
        self.assertEqual([r["ckpt_id"] for r in rows], list(reversed(ids)))

    def test_latest_returns_newest(self) -> None:
        ids = [self.service.create(trigger_reason=f"t-{i}") for i in range(3)]
        self.assertEqual(self.service.latest()["ckpt_id"], ids[-1])

    def test_list_exposes_expected_fields(self) -> None:
        self.service.create(
            trigger_reason="t", session_id="s1", consecutive_failures=2,
        )
        rows = self.service.list()
        self.assertEqual(len(rows), 1)
        keys = set(rows[0].keys())
        for expected in ("ckpt_id", "created_at", "trigger_reason",
                         "session_id", "consecutive_failures",
                         "file_path", "pending_restore", "restored_at"):
            self.assertIn(expected, keys)


class CheckpointScheduleRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.snapshots_dir = tmp / "snapshots"
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=self.snapshots_dir,
        )

    def test_schedule_restore_sets_flag(self) -> None:
        ckpt_id = self.service.create(trigger_reason="t")
        self.service.schedule_restore(ckpt_id)
        row = self.store._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 1)

    def test_schedule_restore_clears_previous_pending(self) -> None:
        a = self.service.create(trigger_reason="a")
        b = self.service.create(trigger_reason="b")
        self.service.schedule_restore(a)
        self.service.schedule_restore(b)
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints WHERE pending_restore = 1"
        ).fetchone()["c"]
        self.assertEqual(count, 1)
        row = self.store._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?", (a,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 0)

    def test_schedule_restore_unknown_id_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.service.schedule_restore("ckpt_deadbeef")

    def test_schedule_restore_missing_file_raises(self) -> None:
        ckpt_id = self.service.create(trigger_reason="t")
        (self.snapshots_dir / f"{ckpt_id}.db").unlink()
        with self.assertRaises(FileNotFoundError):
            self.service.schedule_restore(ckpt_id)


class CheckpointHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        from claw_v2.checkpoint_handler import CheckpointHandler
        from claw_v2.bot_commands import CommandContext
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=tmp / "snapshots",
        )
        self.handler = CheckpointHandler(checkpoint=self.service)
        self._Context = CommandContext

    def _ctx(self, text: str) -> "CommandContext":
        return self._Context(user_id="u1", session_id="s1", text=text, stripped=text)

    def test_commands_registers_rollback_and_checkpoints(self) -> None:
        names = [c.name for c in self.handler.commands()]
        self.assertIn("rollback", names)
        self.assertIn("checkpoints", names)

    def test_rollback_without_arg_returns_usage(self) -> None:
        reply = self.handler.handle_command(self._ctx("/rollback"))
        self.assertIn("Uso", reply)
        self.assertIn("/rollback", reply)

    def test_rollback_last_schedules_latest(self) -> None:
        ckpt_id = self.service.create(trigger_reason="t")
        reply = self.handler.handle_command(self._ctx("/rollback last"))
        self.assertIn(ckpt_id, reply)
        self.assertIn("/restart", reply)
        row = self.store._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 1)

    def test_rollback_by_id_schedules_exact(self) -> None:
        a = self.service.create(trigger_reason="a")
        b = self.service.create(trigger_reason="b")
        reply = self.handler.handle_command(self._ctx(f"/rollback {a}"))
        self.assertIn(a, reply)
        row = self.store._conn.execute(
            "SELECT ckpt_id FROM checkpoints WHERE pending_restore = 1"
        ).fetchone()
        self.assertEqual(row["ckpt_id"], a)

    def test_rollback_unknown_id_returns_error(self) -> None:
        reply = self.handler.handle_command(self._ctx("/rollback ckpt_deadbeef"))
        self.assertIn("no encontrado", reply.lower())
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints WHERE pending_restore = 1"
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_rollback_last_without_any_checkpoints_returns_error(self) -> None:
        reply = self.handler.handle_command(self._ctx("/rollback last"))
        self.assertIn("no hay checkpoints", reply.lower())

    def test_checkpoints_list_renders_rows(self) -> None:
        a = self.service.create(trigger_reason="pre-action")
        b = self.service.create(trigger_reason="manual")
        reply = self.handler.handle_command(self._ctx("/checkpoints list"))
        self.assertIn(a, reply)
        self.assertIn(b, reply)
        self.assertIn("pre-action", reply)

    def test_checkpoints_list_empty(self) -> None:
        reply = self.handler.handle_command(self._ctx("/checkpoints list"))
        self.assertIn("sin checkpoints", reply.lower())

    def test_checkpoints_without_subcommand_returns_list_anyway(self) -> None:
        # Unknown subcommand → still useful: show usage.
        reply = self.handler.handle_command(self._ctx("/checkpoints"))
        self.assertIn("/checkpoints list", reply)


class CheckpointEndToEndTests(unittest.TestCase):
    def test_restore_reverts_to_prior_state(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        db_path = tmp / "claw.db"
        snapshots_dir = tmp / "snapshots"

        # Phase 1 — seed, snapshot, mutate, schedule.
        store = MemoryStore(db_path)
        store.store_fact("k", "before", source="test")
        service = CheckpointService(memory=store, snapshots_dir=snapshots_dir)
        ckpt_id = service.create(trigger_reason="seed")
        store.store_fact("k", "after", source="test")
        service.schedule_restore(ckpt_id)
        store._conn.close()

        # Phase 2 — reopen (simulates restart). Apply happens in __init__.
        store2 = MemoryStore(db_path)
        values = [f["value"] for f in store2.search_facts("k")]
        self.assertIn("before", values)
        self.assertNotIn("after", values)

        row = store2._conn.execute(
            "SELECT pending_restore, restored_at FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 0)
        self.assertIsNotNone(row["restored_at"])


class CheckpointHandlerWiredInBotTests(unittest.TestCase):
    def test_checkpoint_commands_registered_in_bot(self) -> None:
        # This is a structural test — we don't want to construct the full bot
        # (expensive; pulls in many dependencies). Instead, grep the source to
        # assert the handler is instantiated and its commands are registered.
        import pathlib
        bot_src = pathlib.Path(
            "/Users/hector/Projects/Dr.-strange/claw_v2/bot.py"
        ).read_text()
        self.assertIn("from claw_v2.checkpoint_handler import CheckpointHandler", bot_src)
        self.assertIn("CheckpointHandler(", bot_src)
        self.assertIn("self._checkpoint_handler.commands()", bot_src)
