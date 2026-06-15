from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claw_v2.sqlite_runtime import (
    SQLITE_BUSY_TIMEOUT_MS,
    RuntimeDatabaseError,
    RuntimeDb,
    _registry_key,
    _WAL_HEAL_REGISTRY,
    check_runtime_sqlite_health,
    connect_runtime_sqlite,
)


class RuntimeSqliteTests(unittest.TestCase):
    def test_connect_creates_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"

            conn = connect_runtime_sqlite(path)
            try:
                conn.execute("CREATE TABLE smoke (id INTEGER PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()

            self.assertTrue(path.exists())
            with sqlite3.connect(path) as check:
                self.assertIsNotNone(
                    check.execute("SELECT name FROM sqlite_master WHERE name = 'smoke'").fetchone()
                )

    def test_connect_configures_durable_runtime_pragmas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"

            conn = connect_runtime_sqlite(path)
            try:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
                busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            finally:
                conn.close()

            self.assertEqual(str(journal_mode).lower(), "wal")
            self.assertEqual(synchronous, 2)  # FULL
            self.assertEqual(busy_timeout, SQLITE_BUSY_TIMEOUT_MS)
            self.assertEqual(foreign_keys, 1)

    def test_connect_rejects_non_sqlite_file_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            original = b"not a sqlite database"
            path.write_bytes(original)

            with self.assertRaises(RuntimeDatabaseError):
                connect_runtime_sqlite(path)

            self.assertEqual(path.read_bytes(), original)

    def test_health_check_accepts_valid_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE smoke (id INTEGER PRIMARY KEY)")
                conn.commit()

            check_runtime_sqlite_health(path, thorough=True)

    def test_health_check_rejects_malformed_sqlite_header_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            path.write_bytes(b"SQLite format 3\x00" + (b"\x00" * 128))

            with self.assertRaises(RuntimeDatabaseError):
                check_runtime_sqlite_health(path, thorough=True)


class RuntimeDbTests(unittest.TestCase):
    """F1.1a0: RuntimeDb is the single owner of a runtime SQLite connection +
    a shared re-entrant lock. Exercised in isolation — not wired into any store
    or main.py yet, so it must be fully correct on its own."""

    def _runtime_db(self, tmpdir: str, **kwargs) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "claw.db", **kwargs)
        self.addCleanup(db.close)
        with db.transaction() as cur:
            cur.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        return db

    def test_owns_single_connection_and_reentrant_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            self.assertEqual(db.current_connection_id(), db.current_connection_id())
            self.assertIsInstance(db._current_connection_for_tests(), sqlite3.Connection)
            # RLock: a nested cursor() on the same thread must not deadlock.
            with db.cursor() as outer:
                outer.execute("SELECT 1")
                with db.cursor() as inner:
                    inner.execute("SELECT 1")

    def test_cursor_runs_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            held, release = threading.Event(), threading.Event()

            def hold() -> None:
                with db.cursor():
                    held.set()
                    release.wait(2)

            t = threading.Thread(target=hold)
            t.start()
            try:
                self.assertTrue(held.wait(2))
                with db.try_acquire() as acquired:  # another thread cannot acquire
                    self.assertFalse(acquired)
            finally:
                release.set()
                t.join(2)

    def test_transaction_commits_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            with db.transaction() as cur:
                cur.execute("INSERT INTO t (v) VALUES ('ok')")
            with db.cursor() as cur:
                self.assertEqual(cur.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_transaction_rolls_back_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            with self.assertRaises(RuntimeError):
                with db.transaction() as cur:
                    cur.execute("INSERT INTO t (v) VALUES ('rollback')")
                    raise RuntimeError("boom")
            with db.cursor() as cur:
                self.assertEqual(cur.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_try_acquire_and_try_cursor_return_immediately_when_lock_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            held, release = threading.Event(), threading.Event()

            def hold() -> None:
                with db.cursor():
                    held.set()
                    release.wait(2)

            t = threading.Thread(target=hold)
            t.start()
            try:
                self.assertTrue(held.wait(2))
                start = time.monotonic()
                with db.try_acquire() as acquired:
                    self.assertFalse(acquired)
                with db.try_cursor() as cur:
                    self.assertIsNone(cur)
                self.assertLess(time.monotonic() - start, 1.0)  # did not block
            finally:
                release.set()
                t.join(2)

    def test_row_shape_is_per_cursor_without_global_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)  # default row_factory=True -> Row
            with db.transaction() as cur:
                cur.execute("INSERT INTO t (v) VALUES ('x')")
            with db.cursor() as cur:  # default -> Row (mapping access)
                self.assertEqual(cur.execute("SELECT id, v FROM t").fetchone()["v"], "x")
            with db.cursor(row_factory=None) as cur:  # tuple for this cursor only
                self.assertIsInstance(cur.execute("SELECT id, v FROM t").fetchone(), tuple)
            with db.cursor() as cur:  # shared connection's factory was NOT mutated
                self.assertEqual(cur.execute("SELECT id, v FROM t").fetchone()["v"], "x")

    def test_cursor_inside_transaction_shares_the_transaction(self) -> None:
        # cursor() nested inside transaction() is supported (re-entrant lock,
        # same connection): the inner cursor's writes commit/roll back ATOMICALLY
        # with the enclosing transaction; cursor() never commits on its own.
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            with db.transaction() as tcur:
                tcur.execute("INSERT INTO t (v) VALUES ('A')")
                with db.cursor() as ccur:
                    ccur.execute("INSERT INTO t (v) VALUES ('B')")
            with db.cursor() as cur:
                self.assertEqual(sorted(r["v"] for r in cur.execute("SELECT v FROM t")), ["A", "B"])
            with self.assertRaises(RuntimeError):
                with db.transaction() as tcur:
                    tcur.execute("INSERT INTO t (v) VALUES ('C')")
                    with db.cursor() as ccur:
                        ccur.execute("INSERT INTO t (v) VALUES ('D')")
                    raise RuntimeError("boom")
            with db.cursor() as cur:  # both C and D rolled back with the outer transaction
                self.assertEqual(sorted(r["v"] for r in cur.execute("SELECT v FROM t")), ["A", "B"])

    def test_nested_transaction_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            with self.assertRaises(RuntimeError):
                with db.transaction():
                    with db.transaction():
                        pass

    def test_heal_reopen_swaps_connection_and_later_access_uses_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            old = db._current_connection_for_tests()  # hold ref so id() can't be reused
            old_id = db.current_connection_id()
            db._simulate_wal_heal_reopen_for_tests()
            self.assertNotEqual(old_id, db.current_connection_id())
            self.assertIsNot(db._current_connection_for_tests(), old)
            with db.cursor() as cur:  # works on the NEW connection
                self.assertEqual(cur.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_runtimedb_is_registered_as_heal_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            handles = _WAL_HEAL_REGISTRY.get(_registry_key(db.db_path), [])
            self.assertTrue(any(h is db._heal_handle for h in handles))

    def test_close_unregisters_heal_handle_no_registry_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            db = RuntimeDb(path)
            key = _registry_key(path)
            self.assertIn(key, _WAL_HEAL_REGISTRY)
            handle = db._heal_handle
            db.close()
            self.assertFalse(any(h is handle for h in _WAL_HEAL_REGISTRY.get(key, [])))
            db.close()  # idempotent


if __name__ == "__main__":
    unittest.main()
