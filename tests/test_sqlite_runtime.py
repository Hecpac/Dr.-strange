from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.sqlite_runtime import (
    SQLITE_BUSY_TIMEOUT_MS,
    RuntimeDatabaseError,
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
                self.assertIsNotNone(check.execute("SELECT name FROM sqlite_master WHERE name = 'smoke'").fetchone())

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


if __name__ == "__main__":
    unittest.main()
