"""T10 (incidente 2026-06-12) — WAL generation guard.

Something unlinked the -wal/-shm sidecars under the daemon's live
connections; every writer then failed "database is locked" forever and
messages/events/task closes silently stopped persisting. These tests pin the
recovery machinery: orphan detection, registry-wide heal, the observe-stream
retry hook, and the bounded terminal-write retry in the task ledger.
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import (
    connect_runtime_sqlite,
    heal_orphaned_wal,
    make_store_wal_heal,
    register_wal_heal,
    wal_sidecars_orphaned,
)
from claw_v2.task_ledger import TaskLedger


class _AlwaysLockedConn:
    """Stand-in for a connection wedged on an orphaned WAL generation."""

    def __init__(self) -> None:
        self.closed = False

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    def commit(self) -> None:
        raise sqlite3.OperationalError("database is locked")

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _LockedNTimesConn:
    """Delegates to a real connection after N locked failures."""

    def __init__(self, real: sqlite3.Connection, failures: int) -> None:
        self._real = real
        self.failures = failures

    def execute(self, *args, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _seed_db(db_path: Path) -> None:
    conn = connect_runtime_sqlite(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS seed (x INTEGER)")
    conn.execute("INSERT INTO seed VALUES (1)")
    conn.commit()
    conn.close()


class WalOrphanDetectionTests(unittest.TestCase):
    def test_orphan_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            conn = connect_runtime_sqlite(db_path)
            conn.execute("INSERT INTO seed VALUES (2)")
            conn.commit()
            self.assertTrue(Path(f"{db_path}-wal").exists())
            self.assertFalse(wal_sidecars_orphaned(db_path))
            Path(f"{db_path}-wal").unlink()
            self.assertTrue(wal_sidecars_orphaned(db_path))
            conn.close()

    def test_missing_or_empty_db_is_not_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"
            self.assertFalse(wal_sidecars_orphaned(db_path))
            db_path.touch()
            self.assertFalse(wal_sidecars_orphaned(db_path))


class HealRegistryTests(unittest.TestCase):
    def test_heal_reopens_all_registered_stores(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)

            class _Store:
                def __init__(self) -> None:
                    self.db_path = db_path
                    self._conn = _AlwaysLockedConn()
                    self._lock = threading.Lock()

            stores = [_Store(), _Store()]
            wedged = [store._conn for store in stores]
            for store in stores:
                register_wal_heal(db_path, make_store_wal_heal(store))

            # Sidecars present -> no heal.
            keeper = connect_runtime_sqlite(db_path)
            keeper.execute("INSERT INTO seed VALUES (3)")
            keeper.commit()
            self.assertFalse(heal_orphaned_wal(db_path))
            for store, old in zip(stores, wedged):
                self.assertIs(store._conn, old)

            keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            keeper.close()  # last live conn: SQLite removes the sidecars
            self.assertTrue(heal_orphaned_wal(db_path))
            for store, old in zip(stores, wedged):
                self.assertTrue(old.closed)
                self.assertIsNot(store._conn, old)
                store._conn.execute("INSERT INTO seed VALUES (4)")
                store._conn.commit()


class ObservePersistHealTests(unittest.TestCase):
    def test_persist_survives_orphaned_wal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "obs.db"
            observe = ObserveStream(db_path)
            # Production shape: the main file holds the checkpointed data, so
            # losing the sidecars loses only un-checkpointed frames.
            observe._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            real = observe._conn
            # Simulate the wedge: the live connection fails locked forever and
            # the sidecars are gone from disk.
            observe._conn = _AlwaysLockedConn()
            real.close()
            Path(f"{db_path}-wal").unlink(missing_ok=True)
            Path(f"{db_path}-shm").unlink(missing_ok=True)

            observe.emit("wal_heal_probe", payload={"k": "v"})

            events = [e["event_type"] for e in observe.recent_events(limit=5)]
            self.assertIn(
                "wal_heal_probe",
                events,
                "the event must persist after the generation heal, not drop",
            )

    def test_persist_still_drops_when_lock_is_not_wal_related(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "obs.db"
            observe = ObserveStream(db_path)
            real = observe._conn
            observe._conn = _AlwaysLockedConn()
            # Sidecars INTACT (real conn still open): heal must not trigger,
            # the event spills as before.
            self.assertTrue(Path(f"{db_path}-wal").exists())
            observe.emit("plain_lock_probe", payload={})
            observe._conn = real
            events = [e["event_type"] for e in observe.recent_events(limit=5)]
            self.assertNotIn("plain_lock_probe", events)


class TerminalWriteRetryTests(unittest.TestCase):
    def test_mark_terminal_retries_through_transient_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            ledger = TaskLedger(db_path)
            ledger.create(
                task_id="t-1",
                session_id="tg-1",
                objective="obj",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="m",
                status="running",
            )
            ledger._conn = _LockedNTimesConn(ledger._conn, failures=2)
            record = ledger.mark_terminal(
                "t-1", status="failed", summary="s", error="boom"
            )
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "failed")

    def test_mark_terminal_raises_visibly_after_exhausted_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            events: list[str] = []

            class _Obs:
                def emit(self, event_type, **kwargs):
                    events.append(event_type)

            ledger = TaskLedger(db_path, observe=_Obs())
            ledger.create(
                task_id="t-2",
                session_id="tg-1",
                objective="obj",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="m",
                status="running",
            )
            ledger._conn = _LockedNTimesConn(ledger._conn, failures=99)
            with self.assertRaises(sqlite3.OperationalError):
                ledger.mark_terminal("t-2", status="failed", summary="s", error="boom")
            self.assertIn("task_terminal_write_contention", events)


class WalGenerationSwapTests(unittest.TestCase):
    """Live drill 2026-06-12: an external process can delete our sidecars and
    leave fresh ones of its own — the wal 'exists' but it is a different
    generation. Detection must be by inode, not mere existence."""

    def test_inode_swap_triggers_orphan_detection(self) -> None:
        from claw_v2.sqlite_runtime import note_wal_generation

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            conn = connect_runtime_sqlite(db_path)
            conn.execute("INSERT INTO seed VALUES (2)")
            conn.commit()
            note_wal_generation(db_path)
            self.assertFalse(wal_sidecars_orphaned(db_path))

            # External generation swap: our wal disappears; a different file
            # (new inode) takes its place and STAYS on disk.
            wal = Path(f"{db_path}-wal")
            wal.unlink()
            wal.write_bytes(b"")
            self.assertTrue(
                wal_sidecars_orphaned(db_path),
                "a swapped wal inode must count as a broken generation",
            )
            conn.close()

    def test_void_write_drift_detected_on_success_path(self) -> None:
        # Live drill: the victim keeps 'succeeding' into the orphaned inode
        # (no lock errors). The success-path drift check must heal anyway.
        from claw_v2.sqlite_runtime import _WAL_GENERATION_INODES, _registry_key

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "obs.db"
            observe = ObserveStream(db_path)
            observe.emit("seed_event", payload={})
            wedged_or_first_conn = observe._conn
            # Simulate the external swap: our stamp no longer matches the
            # on-disk wal inode.
            key = _registry_key(db_path)
            assert key in _WAL_GENERATION_INODES
            _WAL_GENERATION_INODES[key] = _WAL_GENERATION_INODES[key] + 12345

            observe.emit("drift_probe", payload={})

            self.assertIsNot(
                observe._conn,
                wedged_or_first_conn,
                "success-path drift must trigger the generation heal",
            )
            events = [e["event_type"] for e in observe.recent_events(limit=10)]
            self.assertIn("drift_probe", events)

    def test_heal_clears_generation_stamp(self) -> None:
        from claw_v2.sqlite_runtime import _WAL_GENERATION_INODES, _registry_key, note_wal_generation

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            conn = connect_runtime_sqlite(db_path)
            conn.execute("INSERT INTO seed VALUES (2)")
            conn.commit()
            note_wal_generation(db_path)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.close()
            self.assertTrue(heal_orphaned_wal(db_path))
            self.assertNotIn(_registry_key(db_path), _WAL_GENERATION_INODES)


class HealHandleLifecycleTests(unittest.TestCase):
    def test_dead_store_handles_are_pruned(self) -> None:
        # PR #97 review (gemini HIGH): the registry must not keep dead stores
        # alive nor crash on them.
        import gc

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)

            class _Store:
                def __init__(self) -> None:
                    self.db_path = db_path
                    self._conn = _AlwaysLockedConn()
                    self._lock = threading.Lock()

            store = _Store()
            handle = make_store_wal_heal(store)
            register_wal_heal(db_path, handle)
            self.assertTrue(handle.alive)
            del store
            gc.collect()
            self.assertFalse(handle.alive)
            # Orphaned state + dead handle: heal must run without raising.
            self.assertTrue(heal_orphaned_wal(db_path))


class TerminalWriteHealExhaustionTests(unittest.TestCase):
    def test_exhaustion_after_successful_heal_still_raises(self) -> None:
        # PR #97 review (gemini CRITICAL): a heal on the last attempt used to
        # fall out of the loop without writing OR raising — mark_terminal
        # continued silently with the task still 'running'.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            events: list[str] = []

            class _Obs:
                def emit(self, event_type, **kwargs):
                    events.append(event_type)

            ledger = TaskLedger(db_path, observe=_Obs())
            ledger.create(
                task_id="t-3",
                session_id="tg-1",
                objective="obj",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="m",
            status="running",
            )
            # Make the heal trigger (orphaned sidecars) while the ledger conn
            # keeps failing locked even after the heal round.
            ledger._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            real = ledger._conn
            ledger._conn = _AlwaysLockedConn()
            real.close()
            Path(f"{db_path}-wal").unlink(missing_ok=True)
            Path(f"{db_path}-shm").unlink(missing_ok=True)

            # The registered heal handle will reopen ledger._conn to a REAL
            # connection mid-retry; force it back to locked each time so the
            # post-heal budget also exhausts.
            class _RelockingLedgerConn(_AlwaysLockedConn):
                pass

            original_reopen_marker = {}

            import claw_v2.sqlite_runtime as sr

            original_connect = sr.connect_runtime_sqlite

            def relock_connect(path, **kwargs):
                conn = original_connect(path, **kwargs)
                if Path(path) == db_path:
                    original_reopen_marker["reopened"] = True
                    conn.close()
                    return _RelockingLedgerConn()
                return conn

            sr.connect_runtime_sqlite = relock_connect
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    ledger.mark_terminal("t-3", status="failed", summary="s", error="x")
            finally:
                sr.connect_runtime_sqlite = original_connect
            self.assertIn("task_terminal_write_contention", events)
            record_conn = original_connect(db_path)
            row = record_conn.execute(
                "SELECT status FROM agent_tasks WHERE task_id='t-3'"
            ).fetchone()
            record_conn.close()
            self.assertEqual(row["status"], "running", "the write must NOT be faked")


if __name__ == "__main__":
    unittest.main()
