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

from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import (
    RuntimeDatabaseError,
    connect_runtime_sqlite,
    heal_orphaned_wal,
    make_store_wal_heal,
    register_wal_heal,
    runtime_db_degraded_error,
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


class _DiskIoOnceConn:
    """Delegates to a real connection after one disk I/O failure."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.failures = 1

    def execute(self, *args, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise sqlite3.OperationalError("disk I/O error")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _TrackingLock:
    def __init__(self) -> None:
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        return False


class _RollbackProbeConn:
    def __init__(self, lock: _TrackingLock) -> None:
        self._lock = lock
        self.rollback_seen_locked = False

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    def rollback(self) -> None:
        self.rollback_seen_locked = self._lock.depth > 0


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

    def test_heal_is_serialized_per_db_path(self) -> None:
        from claw_v2.sqlite_runtime import _WAL_GENERATION_INODES, _registry_key

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            close_calls = 0
            close_lock = threading.Lock()
            close_started = threading.Event()
            release_close = threading.Event()

            class _SlowCloseConn(_AlwaysLockedConn):
                def close(self) -> None:
                    nonlocal close_calls
                    with close_lock:
                        close_calls += 1
                    close_started.set()
                    release_close.wait(timeout=2)
                    super().close()

            class _Store:
                def __init__(self) -> None:
                    self.db_path = db_path
                    self._conn = _SlowCloseConn()
                    self._lock = threading.Lock()

            store = _Store()
            register_wal_heal(db_path, make_store_wal_heal(store))
            _WAL_GENERATION_INODES[_registry_key(db_path)] = 1
            Path(f"{db_path}-wal").write_bytes(b"")

            results: list[bool] = []
            errors: list[BaseException] = []
            start = threading.Barrier(2)

            def worker() -> None:
                try:
                    start.wait()
                    results.append(heal_orphaned_wal(db_path))
                except BaseException as exc:  # pragma: no cover - assertion path
                    errors.append(exc)

            threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
            for thread in threads:
                thread.start()
            self.assertTrue(close_started.wait(timeout=1))
            release_close.set()
            for thread in threads:
                thread.join(timeout=2)

            self.assertFalse(errors)
            self.assertEqual(close_calls, 1)
            self.assertEqual(results.count(True), 1)
            self.assertEqual(results.count(False), 1)


class SidecarCleanupTests(unittest.TestCase):
    def test_empty_wal_and_stale_shm_are_removed_together(self) -> None:
        from claw_v2.sqlite_runtime import _WAL_GENERATION_INODES, _registry_key

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            key = _registry_key(db_path)
            _WAL_GENERATION_INODES[key] = 1
            wal = Path(f"{db_path}-wal")
            shm = Path(f"{db_path}-shm")
            wal.write_bytes(b"")
            shm.write_bytes(b"stale")

            self.assertTrue(heal_orphaned_wal(db_path))

            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())

    def test_missing_wal_removes_stale_shm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            wal = Path(f"{db_path}-wal")
            shm = Path(f"{db_path}-shm")
            wal.unlink(missing_ok=True)
            shm.write_bytes(b"stale")

            self.assertTrue(heal_orphaned_wal(db_path))

            self.assertFalse(wal.exists())
            self.assertFalse(shm.exists())

    def test_nonempty_wal_is_never_deleted(self) -> None:
        from claw_v2.sqlite_runtime import _WAL_GENERATION_INODES, _registry_key

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            key = _registry_key(db_path)
            _WAL_GENERATION_INODES[key] = 1
            wal = Path(f"{db_path}-wal")
            shm = Path(f"{db_path}-shm")
            wal.write_bytes(b"non-empty wal frames")
            shm.write_bytes(b"stale")

            self.assertTrue(heal_orphaned_wal(db_path))

            self.assertEqual(wal.read_bytes(), b"non-empty wal frames")
            self.assertTrue(shm.exists())

    def test_partial_reopen_fails_explicitly_and_marks_degraded(self) -> None:
        import claw_v2.sqlite_runtime as sr

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)

            class _Store:
                def __init__(self) -> None:
                    self.db_path = db_path
                    self._conn = _AlwaysLockedConn()
                    self._lock = threading.Lock()

            stores = [_Store(), _Store()]
            for store in stores:
                register_wal_heal(db_path, make_store_wal_heal(store))
            Path(f"{db_path}-wal").unlink(missing_ok=True)
            calls = 0
            original_connect = sr.connect_runtime_sqlite

            def flaky_connect(path, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise sqlite3.OperationalError("disk I/O error")
                return original_connect(path, **kwargs)

            sr.connect_runtime_sqlite = flaky_connect
            try:
                with self.assertRaises(RuntimeDatabaseError) as ctx:
                    heal_orphaned_wal(db_path)
            finally:
                sr.connect_runtime_sqlite = original_connect

            self.assertIn("WAL heal failed", str(ctx.exception))
            self.assertIsNotNone(runtime_db_degraded_error(db_path))
            with self.assertRaises(RuntimeDatabaseError) as fresh_ctx:
                connect_runtime_sqlite(db_path)
            self.assertIs(fresh_ctx.exception, runtime_db_degraded_error(db_path))
            for store in stores:
                with self.assertRaises(RuntimeDatabaseError):
                    store._conn.execute("SELECT 1")


class ObservePersistHealTests(unittest.TestCase):
    def test_persist_rolls_back_under_observe_lock(self) -> None:
        import claw_v2.observe as observe_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "obs.db"
            observe = ObserveStream(db_path)
            real_conn = observe._conn
            lock = _TrackingLock()
            probe = _RollbackProbeConn(lock)
            observe._lock = lock
            observe._conn = probe
            original_heal = observe_module.heal_wal_after_disk_io

            def no_heal(path, exc, *, context):
                return False

            observe_module.heal_wal_after_disk_io = no_heal
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    observe._persist_event(
                        "rollback_probe",
                        lane=None,
                        provider=None,
                        model=None,
                        trace_id=None,
                        root_trace_id=None,
                        span_id=None,
                        parent_span_id=None,
                        job_id=None,
                        artifact_id=None,
                        clean_payload={},
                    )
            finally:
                observe_module.heal_wal_after_disk_io = original_heal
                real_conn.close()

            self.assertTrue(probe.rollback_seen_locked)

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

    def test_persist_retries_once_after_disk_io_when_heal_succeeds(self) -> None:
        import claw_v2.observe as observe_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "obs.db"
            observe = ObserveStream(db_path)
            observe._conn = _DiskIoOnceConn(observe._conn)
            heal_calls: list[str] = []
            original_heal = observe_module.heal_wal_after_disk_io

            def fake_heal(path, exc, *, context):
                heal_calls.append(context)
                return True

            observe_module.heal_wal_after_disk_io = fake_heal
            try:
                observe.emit("disk_io_probe", payload={"ok": True})
            finally:
                observe_module.heal_wal_after_disk_io = original_heal

            self.assertEqual(heal_calls, ["ObserveStream._persist_event"])
            events = [e["event_type"] for e in observe.recent_events(limit=5)]
            self.assertIn("disk_io_probe", events)


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

    def test_mark_terminal_retries_once_after_disk_io_when_heal_succeeds(self) -> None:
        import claw_v2.task_ledger as task_ledger_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            ledger = TaskLedger(db_path)
            ledger.create(
                task_id="t-disk",
                session_id="tg-1",
                objective="obj",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="m",
                status="running",
            )
            ledger._conn = _DiskIoOnceConn(ledger._conn)
            heal_calls: list[str] = []
            original_heal = task_ledger_module.heal_wal_after_disk_io

            def fake_heal(path, exc, *, context):
                heal_calls.append(context)
                return True

            task_ledger_module.heal_wal_after_disk_io = fake_heal
            try:
                record = ledger.mark_terminal(
                    "t-disk", status="failed", summary="s", error="boom"
                )
            finally:
                task_ledger_module.heal_wal_after_disk_io = original_heal

            self.assertEqual(heal_calls, ["TaskLedger.mark_terminal"])
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "failed")


class StoreDiskIoRetryTests(unittest.TestCase):
    def test_memory_update_session_state_retries_once_after_disk_io(self) -> None:
        import claw_v2.memory as memory_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            memory = MemoryStore(db_path)
            memory._conn = _DiskIoOnceConn(memory._conn)
            heal_calls: list[str] = []
            original_heal = memory_module.heal_wal_after_disk_io

            def fake_heal(path, exc, *, context):
                heal_calls.append(context)
                return True

            memory_module.heal_wal_after_disk_io = fake_heal
            try:
                state = memory.update_session_state("s1", verification_status="running")
            finally:
                memory_module.heal_wal_after_disk_io = original_heal

            self.assertEqual(heal_calls, ["MemoryStore.update_session_state"])
            self.assertEqual(state["verification_status"], "running")

    def test_job_claim_next_retries_once_after_disk_io(self) -> None:
        import claw_v2.jobs as jobs_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "jobs.db"
            jobs = JobService(db_path)
            jobs.enqueue(kind="pipeline.issue")
            jobs._conn = _DiskIoOnceConn(jobs._conn)
            heal_calls: list[str] = []
            original_heal = jobs_module.heal_wal_after_disk_io

            def fake_heal(path, exc, *, context):
                heal_calls.append(context)
                return True

            jobs_module.heal_wal_after_disk_io = fake_heal
            try:
                claimed = jobs.claim_next(worker_id="worker-1")
            finally:
                jobs_module.heal_wal_after_disk_io = original_heal

            self.assertEqual(heal_calls, ["JobService.claim_next"])
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.status, "running")

    def test_job_terminal_update_retries_once_after_disk_io(self) -> None:
        import claw_v2.jobs as jobs_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "jobs.db"
            jobs = JobService(db_path)
            created = jobs.enqueue(kind="pipeline.issue")
            jobs.claim_next(worker_id="worker-1")
            jobs._conn = _DiskIoOnceConn(jobs._conn)
            heal_calls: list[str] = []
            original_heal = jobs_module.heal_wal_after_disk_io

            def fake_heal(path, exc, *, context):
                heal_calls.append(context)
                return True

            jobs_module.heal_wal_after_disk_io = fake_heal
            try:
                completed = jobs.complete(created.job_id, result={"ok": True})
            finally:
                jobs_module.heal_wal_after_disk_io = original_heal

            self.assertEqual(heal_calls, ["JobService.job_completed"])
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed.status, "completed")

    def test_job_fail_retries_once_after_disk_io(self) -> None:
        import claw_v2.jobs as jobs_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "jobs.db"
            jobs = JobService(db_path)
            created = jobs.enqueue(kind="pipeline.issue", max_attempts=1)
            jobs.claim_next(worker_id="worker-1")
            jobs._conn = _DiskIoOnceConn(jobs._conn)
            heal_calls: list[str] = []
            original_heal = jobs_module.heal_wal_after_disk_io

            def fake_heal(path, exc, *, context):
                heal_calls.append(context)
                return True

            jobs_module.heal_wal_after_disk_io = fake_heal
            try:
                failed = jobs.fail(created.job_id, error="boom", retry=False)
            finally:
                jobs_module.heal_wal_after_disk_io = original_heal

            self.assertEqual(heal_calls, ["JobService.fail"])
            self.assertIsNotNone(failed)
            assert failed is not None
            self.assertEqual(failed.status, "failed")


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


class RegistryHygieneTests(unittest.TestCase):
    def test_register_prunes_dead_handles(self) -> None:
        # PR #98 review (gemini): create/destroy churn must not accumulate
        # dead handles even when no heal ever fires.
        import gc

        from claw_v2.sqlite_runtime import _WAL_HEAL_REGISTRY, _registry_key

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "t.db"
            _seed_db(db_path)
            key = _registry_key(db_path)

            class _Store:
                def __init__(self) -> None:
                    self.db_path = db_path
                    self._conn = _AlwaysLockedConn()
                    self._lock = threading.Lock()

            for _ in range(20):
                store = _Store()
                register_wal_heal(db_path, make_store_wal_heal(store))
                del store
                gc.collect()
            # A fresh registration prunes the dead ones: at most a handful live.
            live = _Store()
            register_wal_heal(db_path, make_store_wal_heal(live))
            alive = [h for h in _WAL_HEAL_REGISTRY.get(key, ()) if h.alive]
            self.assertLessEqual(len(alive), 2, "dead handles must be pruned on register")


class TaskLedgerStampTests(unittest.TestCase):
    def test_terminal_write_stamps_generation(self) -> None:
        # PR #98 review (gemini): task_ledger must also contribute the WAL
        # generation stamp so swap detection works when it writes first.
        from claw_v2.sqlite_runtime import _WAL_GENERATION_INODES, _registry_key

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "ledger.db"
            _WAL_GENERATION_INODES.pop(_registry_key(db_path), None)
            ledger = TaskLedger(db_path)
            ledger.create(
                task_id="t-stamp",
                session_id="tg-1",
                objective="obj",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="m",
                status="running",
            )
            ledger.mark_terminal("t-stamp", status="failed", summary="s", error="x")
            self.assertIn(_registry_key(db_path), _WAL_GENERATION_INODES)


if __name__ == "__main__":
    unittest.main()
