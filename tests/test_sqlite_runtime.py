from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path

from claw_v2.sqlite_runtime import (
    SQLITE_BUSY_TIMEOUT_MS,
    RuntimeDatabaseError,
    RuntimeDb,
    StoreWalHealHandle,
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
    """RuntimeDb is the single production owner of the runtime SQLite
    connection plus shared re-entrant lock. F1.2/F1.3 retires the WAL-heal
    registry from this production path; reconnect is explicit owner lifecycle,
    not a registered conservative-heal callback."""

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

    def test_direct_reconnect_swaps_connection_and_later_access_uses_new(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            old = db._current_connection_for_tests()  # hold ref so id() can't be reused
            old_id = db.current_connection_id()
            db._reconnect_for_tests()
            self.assertNotEqual(old_id, db.current_connection_id())
            self.assertIsNot(db._current_connection_for_tests(), old)
            with db.cursor() as cur:  # works on the NEW connection
                self.assertEqual(cur.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_runtimedb_does_not_register_wal_heal_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            handles = _WAL_HEAL_REGISTRY.get(_registry_key(db.db_path), [])
            self.assertEqual([h for h in handles if h.alive], [])
            self.assertFalse(hasattr(db, "_heal_handle"))

    def test_close_does_not_touch_wal_heal_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            db = RuntimeDb(path)
            key = _registry_key(path)
            self.assertEqual([h for h in _WAL_HEAL_REGISTRY.get(key, []) if h.alive], [])
            db.close()
            self.assertEqual([h for h in _WAL_HEAL_REGISTRY.get(key, []) if h.alive], [])
            db.close()  # idempotent


class RuntimeDbHandleTests(unittest.TestCase):
    """F1.1a1: RuntimeDb exposes the shared RLock (`.lock`) and a non-owning
    dynamic connection handle (`connection_handle`) that lets stores keep
    `self._conn.execute(...)` while the real connection lives in RuntimeDb."""

    def _db(self, tmpdir: str, **kwargs) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "claw.db", **kwargs)
        self.addCleanup(db.close)
        return db

    def test_lock_property_is_stable_reentrant_and_the_shared_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._db(tmpdir)
            self.assertIs(db.lock, db.lock)  # same object each call
            with db.lock:  # re-entrant (RLock): nested acquire on same thread
                with db.lock:
                    pass
            # It is THE lock try_acquire/cursor use: holding it in another thread
            # makes try_acquire fail.
            held, release = threading.Event(), threading.Event()

            def hold() -> None:
                with db.lock:
                    held.set()
                    release.wait(2)

            t = threading.Thread(target=hold)
            t.start()
            try:
                self.assertTrue(held.wait(2))
                with db.try_acquire() as acquired:
                    self.assertFalse(acquired)
            finally:
                release.set()
                t.join(2)

    def test_connection_handle_delegates_execute_script_commit_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._db(tmpdir)
            h = db.connection_handle()
            with db.lock:
                h.executescript("CREATE TABLE t (v TEXT)")
                h.execute("INSERT INTO t (v) VALUES ('x')")
                h.commit()
                rows = h.execute("SELECT v FROM t").fetchall()
            self.assertEqual([r["v"] for r in rows], ["x"])
            with db.lock:  # rollback delegates too
                h.execute("INSERT INTO t (v) VALUES ('y')")
                h.rollback()
                self.assertEqual(h.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_handle_row_factory_is_per_handle_on_one_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._db(tmpdir)
            row_h = db.connection_handle(row_factory=True)
            tuple_h = db.connection_handle(row_factory=False)
            with db.lock:
                row_h.executescript("CREATE TABLE t (v TEXT)")
                row_h.execute("INSERT INTO t (v) VALUES ('x')")
                row_h.commit()
                self.assertEqual(row_h.execute("SELECT v FROM t").fetchone()["v"], "x")  # Row
                self.assertIsInstance(tuple_h.execute("SELECT v FROM t").fetchone(), tuple)  # tuple

    def test_handle_uses_new_connection_after_owner_reconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._db(tmpdir)
            h = db.connection_handle()
            with db.lock:
                h.executescript("CREATE TABLE t (v TEXT)")
                h.execute("INSERT INTO t (v) VALUES ('a')")
                h.commit()
            old = db._current_connection_for_tests()
            old_id = db.current_connection_id()
            db._reconnect_for_tests()
            self.assertNotEqual(old_id, db.current_connection_id())
            self.assertIsNot(db._current_connection_for_tests(), old)
            with db.lock:  # handle delegates to the NEW connection; data persisted in file
                self.assertEqual([r["v"] for r in h.execute("SELECT v FROM t").fetchall()], ["a"])


class RuntimeDbConcurrencyTests(unittest.TestCase):
    """F1.1b: the whole point of the single-writer collapse (RAÍZ #1) is that
    the five core stores share ONE connection serialized by ONE lock, so SQLite
    never sees concurrent access. Under a barrier-synchronized 20-thread mix of
    reads and writes across all five stores, the shared wiring must serialize
    cleanly: ZERO 'database is locked' errors, ZERO WAL-heal reopens, no hung
    thread, the single connection identity unchanged, and exact final row counts.

    This proves the CURRENT wiring is correct and deadlock/heal-free; it is the
    dynamic backstop for lock-discipline gaps the AST tripwire can't see
    (aliasing, lazy cursor consumption). It does NOT by itself reproduce the
    pre-F1 7-connection storm — SQLite's busy_timeout absorbs contention at this
    scale. The detector's teeth are proven separately by removing serialization
    entirely (no-op lock), which surfaces sqlite3 errors immediately.

    Deterministic: every write uses a unique key/id, so all writes land and the
    final row counts are exact regardless of thread interleaving."""

    THREADS = 20
    ITERATIONS = 15

    def _build_shared_stores(self, db: RuntimeDb):
        from claw_v2.jobs import JobService
        from claw_v2.memory import MemoryStore
        from claw_v2.observe import ObserveStream
        from claw_v2.orchestration import OrchestrationStore
        from claw_v2.task_ledger import TaskLedger

        memory = MemoryStore(db.db_path, runtime_db=db)
        observe = ObserveStream(db.db_path, runtime_db=db)
        jobs = JobService(db.db_path, observe=observe, runtime_db=db)
        orchestration = OrchestrationStore(db.db_path, observe=observe, runtime_db=db)
        task_ledger = TaskLedger(db.db_path, observe=observe, runtime_db=db)
        return memory, observe, jobs, orchestration, task_ledger

    def test_20_threads_across_stores_zero_locked_errors_zero_heals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = RuntimeDb(Path(tmpdir) / "claw.db")
            self.addCleanup(db.close)

            # Count every real heal (connection swap / degrade) at the class-level
            # chokepoint, regardless of how stores import the heal helpers.
            heal_calls: list[str] = []
            orig_reopen = StoreWalHealHandle.reopen
            orig_degrade = StoreWalHealHandle.mark_degraded

            def _counting_reopen(handle: StoreWalHealHandle) -> None:
                heal_calls.append("reopen")
                return orig_reopen(handle)

            def _counting_degrade(handle: StoreWalHealHandle, error) -> None:
                heal_calls.append("mark_degraded")
                return orig_degrade(handle, error)

            StoreWalHealHandle.reopen = _counting_reopen  # type: ignore[assignment]
            StoreWalHealHandle.mark_degraded = _counting_degrade  # type: ignore[assignment]
            self.addCleanup(setattr, StoreWalHealHandle, "reopen", orig_reopen)
            self.addCleanup(setattr, StoreWalHealHandle, "mark_degraded", orig_degrade)

            stores = self._build_shared_stores(db)
            memory, observe, jobs, orchestration, task_ledger = stores

            # All five stores resolve to the ONE shared connection + lock.
            for store in stores:
                self.assertIs(store._conn._db, db)
                self.assertIs(store._lock, db.lock)

            conn_id_before = db.current_connection_id()
            barrier = threading.Barrier(self.THREADS)
            errors: list[tuple[int, str, str]] = []
            errors_lock = threading.Lock()

            def worker(t: int) -> None:
                try:
                    barrier.wait()
                    for i in range(self.ITERATIONS):
                        # Writes — one per store (blocking lock; observe drops on
                        # contention by design, never errors).
                        memory.store_fact(key=f"k-{t}-{i}", value="v", source="stress")
                        observe.emit("stress_event", payload={"t": t, "i": i})
                        jobs.enqueue(kind="stress", payload={"t": t, "i": i})
                        orchestration.begin_run(task_id=f"task-{t}-{i}", objective="stress")
                        task_ledger.create(
                            task_id=f"tl-{t}-{i}",
                            session_id=f"s-{t}",
                            objective="stress",
                            runtime="test",
                        )
                        # Reads — concurrent with the writes above.
                        memory.search_facts("k-", limit=3)
                        observe.recent_events(limit=3)
                        jobs.get(f"missing-{t}-{i}")
                        orchestration.get_run(f"run:task-{t}-{i}")
                        task_ledger.get(f"tl-{t}-{i}")
                except BaseException as exc:  # capture EVERYTHING per thread
                    with errors_lock:
                        errors.append((t, repr(exc), traceback.format_exc()))

            threads = [threading.Thread(target=worker, args=(t,)) for t in range(self.THREADS)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(60)

            # 1. No thread raised — in particular, no 'database is locked'.
            self.assertEqual(errors, [], f"worker threads raised: {errors}")
            self.assertFalse(any(th.is_alive() for th in threads), "a worker thread hung")

            # 2. Zero WAL-heal: no reopen/degrade fired, and the single
            #    connection identity is unchanged across the whole run.
            self.assertEqual(heal_calls, [], f"WAL-heal fired under single-writer: {heal_calls}")
            self.assertEqual(db.current_connection_id(), conn_id_before)

            # 3. Determinism: every unique-keyed blocking write landed exactly once.
            expected = self.THREADS * self.ITERATIONS
            with db.cursor() as cur:
                self.assertEqual(cur.execute("SELECT COUNT(*) FROM facts").fetchone()[0], expected)
                self.assertEqual(
                    cur.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0], expected
                )
                self.assertEqual(
                    cur.execute("SELECT COUNT(*) FROM orchestration_runs").fetchone()[0],
                    expected,
                )
                self.assertEqual(
                    cur.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0], expected
                )


if __name__ == "__main__":
    unittest.main()
