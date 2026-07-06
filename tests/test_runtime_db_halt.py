from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from claw_v2.sqlite_runtime import (
    RUNTIME_DB_HALT_MARKER_NAME,
    RuntimeDatabaseError,
    clear_runtime_db_halt_marker,
    runtime_db_halt_marker_path,
    write_runtime_db_halt_marker,
)

import importlib.util

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "runtime_db_preflight_halt_tests", REPO_ROOT / "scripts" / "runtime_db_preflight.py"
)
assert _spec is not None and _spec.loader is not None
_preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_preflight)
preflight_main = _preflight.main


def _make_healthy_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO t (v) VALUES ('x')")
        conn.commit()
    finally:
        conn.close()


class HaltMarkerHelperTests(unittest.TestCase):
    """Slice 2a (blind-spot pass 2026-07-06 finding #2): a corrupt runtime DB
    must leave a persistent record of why boot refused, so the launcher holds
    instead of letting launchd KeepAlive crash-loop with no trace."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "claw.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_marker_persists_reason_atomically(self) -> None:
        marker = write_runtime_db_halt_marker(
            self.db_path, RuntimeDatabaseError("integrity failed"), source="preflight"
        )

        self.assertEqual(marker, runtime_db_halt_marker_path(self.db_path))
        self.assertEqual(marker.name, RUNTIME_DB_HALT_MARKER_NAME)
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(payload["reason"], "runtime_db_corruption")
        self.assertIn("integrity failed", payload["error"])
        self.assertEqual(payload["source"], "preflight")
        self.assertIn("created_at", payload)
        # No tmp residue from the atomic write.
        self.assertEqual(list(marker.parent.glob("*.tmp")), [])

    def test_clear_requires_verified_healthy(self) -> None:
        write_runtime_db_halt_marker(self.db_path, "boom", source="preflight")
        with self.assertRaises(ValueError):
            clear_runtime_db_halt_marker(self.db_path, verified_healthy=False)
        self.assertTrue(runtime_db_halt_marker_path(self.db_path).exists())

    def test_clear_renames_for_audit_never_deletes(self) -> None:
        write_runtime_db_halt_marker(self.db_path, "boom", source="preflight")

        cleared = clear_runtime_db_halt_marker(self.db_path, verified_healthy=True)

        assert cleared is not None
        self.assertFalse(runtime_db_halt_marker_path(self.db_path).exists())
        self.assertTrue(cleared.exists())
        self.assertIn(".cleared-", cleared.name)

    def test_clear_without_marker_is_noop(self) -> None:
        self.assertIsNone(clear_runtime_db_halt_marker(self.db_path, verified_healthy=True))


class PreflightHaltMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "claw.db"
        self.backup_dir = self.root / "backups"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self) -> int:
        return preflight_main(["--db", str(self.db_path), "--backup-dir", str(self.backup_dir)])

    def test_corrupt_db_exits_1_and_writes_marker(self) -> None:
        self.db_path.write_bytes(b"this is not a sqlite database at all........")

        rc = self._run()

        self.assertEqual(rc, 1)
        marker = runtime_db_halt_marker_path(self.db_path)
        self.assertTrue(marker.exists())
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(payload["source"], "preflight")

    def test_healthy_db_exits_0_and_auto_clears_marker(self) -> None:
        _make_healthy_db(self.db_path)
        write_runtime_db_halt_marker(self.db_path, "stale halt", source="preflight")

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertFalse(runtime_db_halt_marker_path(self.db_path).exists())
        cleared = list(self.db_path.parent.glob(f"{RUNTIME_DB_HALT_MARKER_NAME}.cleared-*"))
        self.assertEqual(len(cleared), 1)

    def test_missing_db_exits_0_without_marker(self) -> None:
        # Slice 2b territory: a missing DB is still treated as a valid
        # first-boot state by the preflight — 2a must not change that.
        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertFalse(runtime_db_halt_marker_path(self.db_path).exists())

    def test_missing_db_never_clears_an_existing_halt_marker(self) -> None:
        # Deleting the corrupt file must NOT unlock a fresh-schema boot: the
        # hold persists until a real DB passes the thorough check.
        write_runtime_db_halt_marker(self.db_path, "corrupt", source="preflight")

        rc = self._run()

        self.assertEqual(rc, 0)
        self.assertTrue(runtime_db_halt_marker_path(self.db_path).exists())


class BootHealthHaltTests(unittest.TestCase):
    def test_boot_health_failure_writes_marker_and_reraises(self) -> None:
        from claw_v2.main import _ensure_runtime_db_boot_health

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            db_path.write_bytes(b"garbage header not sqlite..................")
            config = SimpleNamespace(
                db_path=db_path,
                telegram_bot_token=None,
                telegram_allowed_user_id=None,
            )

            with self.assertRaises(RuntimeDatabaseError):
                _ensure_runtime_db_boot_health(config)

            marker = runtime_db_halt_marker_path(db_path)
            self.assertTrue(marker.exists())
            payload = json.loads(marker.read_text(encoding="utf-8"))
            self.assertEqual(payload["source"], "build_runtime")

    def test_boot_health_failure_alerts_owner_best_effort(self) -> None:
        from claw_v2 import main as main_module

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            db_path.write_bytes(b"garbage header not sqlite..................")
            config = SimpleNamespace(
                db_path=db_path,
                telegram_bot_token="token",
                telegram_allowed_user_id="chat",
            )
            sent: list[tuple[str, str, str]] = []

            with patch.object(
                main_module,
                "send_telegram_message",
                side_effect=lambda token, chat, text: sent.append((token, chat, text)),
            ):
                with self.assertRaises(RuntimeDatabaseError):
                    main_module._ensure_runtime_db_boot_health(config)

            self.assertEqual(len(sent), 1)
            self.assertIn("runtime_db_halt", sent[0][2])

    def test_boot_health_passes_healthy_db_without_marker(self) -> None:
        from claw_v2.main import _ensure_runtime_db_boot_health

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _make_healthy_db(db_path)
            config = SimpleNamespace(
                db_path=db_path,
                telegram_bot_token=None,
                telegram_allowed_user_id=None,
            )

            _ensure_runtime_db_boot_health(config)

            self.assertFalse(runtime_db_halt_marker_path(db_path).exists())


if __name__ == "__main__":
    unittest.main()
