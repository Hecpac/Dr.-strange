from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.memory import MemoryStore


class _DiskIoOnceConn:
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


class _ShortReadOnceConn:
    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.failures = 1

    def execute(self, *args, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            exc = sqlite3.OperationalError("disk I/O error")
            exc.sqlite_errorcode = 522
            exc.sqlite_errorname = "SQLITE_IOERR_SHORT_READ"
            raise exc
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class MemoryStoreDiskIoRetryTests(unittest.TestCase):
    def test_update_session_state_retries_once_after_disk_io_when_heal_succeeds(self) -> None:
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

    def test_update_session_state_recovers_from_short_read_with_sidecars_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.db"
            memory = MemoryStore(db_path)
            wal = Path(f"{db_path}-wal")
            shm = Path(f"{db_path}-shm")
            memory._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            wal.touch(exist_ok=True)
            shm.touch(exist_ok=True)
            self.assertTrue(wal.exists())
            self.assertTrue(shm.exists())

            failing_conn = _ShortReadOnceConn(memory._conn)
            memory._conn = failing_conn

            state = memory.update_session_state("s-short", verification_status="running")

            self.assertEqual(state["verification_status"], "running")
            self.assertIsNot(memory._conn, failing_conn)


if __name__ == "__main__":
    unittest.main()
