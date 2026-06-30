from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.sqlite_runtime import RuntimeDatabaseError, check_runtime_sqlite_health


REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = REPO_ROOT / "scripts" / "runtime_db_preflight.py"


def _load_preflight_module():
    spec = importlib.util.spec_from_file_location("runtime_db_preflight", PREFLIGHT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeDbPreflightTests(unittest.TestCase):
    def test_create_verified_backup_copies_runtime_db(self) -> None:
        module = _load_preflight_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "claw.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE smoke(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
                conn.execute("INSERT INTO smoke(value) VALUES ('ok')")
                conn.commit()

            backup = module.create_verified_backup(
                db_path,
                root / "backups",
                now=1_782_828_000,
            )

            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue(backup.exists())
            check_runtime_sqlite_health(backup, thorough=True)
            with sqlite3.connect(backup) as conn:
                value = conn.execute("SELECT value FROM smoke").fetchone()[0]
            self.assertEqual(value, "ok")

    def test_create_verified_backup_rejects_corrupt_db_without_backup(self) -> None:
        module = _load_preflight_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "claw.db"
            db_path.write_bytes(b"not sqlite")
            backup_dir = root / "backups"

            with self.assertRaises(RuntimeDatabaseError):
                module.create_verified_backup(db_path, backup_dir, now=1_782_828_000)

            self.assertFalse(list(backup_dir.glob("*")) if backup_dir.exists() else False)

    def test_restart_script_runs_db_preflight_before_launchctl_kickstart(self) -> None:
        source = (REPO_ROOT / "scripts" / "restart.sh").read_text(encoding="utf-8")

        self.assertIn("scripts/runtime_db_preflight.py", source)
        preflight_call = source.index("run_runtime_db_preflight\n\nif launchctl")
        kickstart = source.index("launchctl kickstart")
        self.assertLess(preflight_call, kickstart)
