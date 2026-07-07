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

    def test_prune_old_backups_keeps_newest_n(self) -> None:
        # Hygiene (blind-spot pass finding #8): restart backups grew unbounded
        # (~2.6G / 54 copies, no rotation). Keep the newest N, drop older.
        module = _load_preflight_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = Path(tmpdir)
            names = [f"claw-2026070{d}-120000.db" for d in range(1, 8)]  # 7 chronological
            for name in names:
                (backup_dir / name).write_bytes(b"x")

            removed = module.prune_old_backups(backup_dir, "claw", keep=3)

            self.assertEqual(len(removed), 4)
            survivors = sorted(p.name for p in backup_dir.glob("claw-*.db"))
            self.assertEqual(survivors, names[-3:])  # newest 3 kept

    def test_prune_zero_keep_disables(self) -> None:
        module = _load_preflight_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = Path(tmpdir)
            (backup_dir / "claw-20260701-120000.db").write_bytes(b"x")
            self.assertEqual(module.prune_old_backups(backup_dir, "claw", keep=0), [])
            self.assertEqual(len(list(backup_dir.glob("claw-*.db"))), 1)

    def test_prune_under_limit_removes_nothing(self) -> None:
        module = _load_preflight_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_dir = Path(tmpdir)
            for d in range(1, 4):
                (backup_dir / f"claw-2026070{d}-120000.db").write_bytes(b"x")
            self.assertEqual(module.prune_old_backups(backup_dir, "claw", keep=15), [])
            self.assertEqual(len(list(backup_dir.glob("claw-*.db"))), 3)

    def test_preflight_prunes_after_backup(self) -> None:
        module = _load_preflight_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "claw.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE t(id INTEGER)")
                conn.commit()
            backup_dir = root / "backups"
            backup_dir.mkdir()
            # Seed 5 old backups; keep=2 → after the new backup, keep newest 2.
            for d in range(1, 6):
                (backup_dir / f"claw-2026070{d}-000000.db").write_bytes(b"x")

            rc = module.main(
                ["--db", str(db_path), "--backup-dir", str(backup_dir), "--keep-backups", "2"]
            )

            self.assertEqual(rc, 0)
            self.assertLessEqual(len(list(backup_dir.glob("claw-*.db"))), 2)

    def test_restart_script_runs_db_preflight_before_launchctl_kickstart(self) -> None:
        source = (REPO_ROOT / "scripts" / "restart.sh").read_text(encoding="utf-8")

        self.assertIn("scripts/runtime_db_preflight.py", source)
        # Slice 2a: the preflight's exit code is captured and a failure aborts
        # the restart BEFORE launchctl kickstart (restarting onto a corrupt DB
        # just re-enters the crash-boot loop).
        preflight_call = source.index("run_runtime_db_preflight\npreflight_rc=$?")
        abort_check = source.index('if [ "$preflight_rc" -ne 0 ]')
        kickstart = source.index("launchctl kickstart")
        self.assertLess(preflight_call, abort_check)
        self.assertLess(abort_check, kickstart)
