from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.sqlite_runtime import RuntimeDatabaseError, connect_runtime_sqlite


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

    def test_connect_rejects_non_sqlite_file_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            original = b"not a sqlite database"
            path.write_bytes(original)

            with self.assertRaises(RuntimeDatabaseError):
                connect_runtime_sqlite(path)

            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
