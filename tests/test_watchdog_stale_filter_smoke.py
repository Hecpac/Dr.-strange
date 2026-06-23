from __future__ import annotations

import ast
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.observe import ObserveStream
from scripts.audit.watchdog_stale_filter_smoke import (
    DEFAULT_EXPECTED_CODE_VERSION,
    collect_smoke_report,
    main,
)


def _insert_event_at(
    db_path: Path,
    event_type: str,
    timestamp_modifier: str,
    payload: dict[str, object],
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO observe_stream (timestamp, event_type, payload)
                VALUES (datetime('now', ?), ?, ?)
                """,
                (timestamp_modifier, event_type, json.dumps(payload)),
            )
            row_id = cursor.lastrowid
        assert row_id is not None
        return int(row_id)
    finally:
        conn.close()


def _row_count(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM observe_stream").fetchone()[0])
    finally:
        conn.close()


class WatchdogStaleFilterSmokeTests(unittest.TestCase):
    def test_pre_startup_errors_are_stale_and_reload_safe_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-20 minutes",
                {
                    "error": "database is locked",
                    "pid": 111,
                    "boot_id": "old-boot",
                    "code_version": "old",
                },
            )
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-10 minutes",
                {
                    "pid": 222,
                    "boot_id": "current-boot",
                    "code_version": DEFAULT_EXPECTED_CODE_VERSION,
                },
            )

            before = _row_count(db_path)
            report = collect_smoke_report(db_path=db_path)
            after = _row_count(db_path)

            self.assertEqual(report["status"], "safe_candidate")
            self.assertEqual(report["recommendation"], "PASS")
            self.assertTrue(report["reload_safe_candidate"])
            self.assertTrue(report["code_version_matches"])
            self.assertTrue(report["stale_filter_exercised"])
            self.assertEqual(report["stale_historical_event_count"], 1)
            self.assertEqual(report["actionable_event_count"], 0)
            self.assertEqual(report["unknown_relevance_event_count"], 0)
            self.assertEqual(after, before)

    def test_expected_version_mismatch_is_not_reload_safe_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-10 minutes",
                {"pid": 222, "boot_id": "current-boot", "code_version": "old"},
            )

            report = collect_smoke_report(db_path=db_path)

            self.assertEqual(report["status"], "version_mismatch")
            self.assertEqual(report["recommendation"], "FAIL")
            self.assertFalse(report["reload_safe_candidate"])
            self.assertFalse(report["code_version_matches"])

    def test_post_startup_error_remains_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-10 minutes",
                {
                    "pid": 222,
                    "boot_id": "current-boot",
                    "code_version": DEFAULT_EXPECTED_CODE_VERSION,
                },
            )
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {
                    "error": "database is locked",
                    "pid": 222,
                    "boot_id": "current-boot",
                    "code_version": DEFAULT_EXPECTED_CODE_VERSION,
                },
            )

            report = collect_smoke_report(db_path=db_path)

            self.assertEqual(report["status"], "actionable_events_present")
            self.assertEqual(report["recommendation"], "REVIEW")
            self.assertFalse(report["reload_safe_candidate"])
            self.assertEqual(report["actionable_event_count"], 1)
            self.assertEqual(report["stale_historical_event_count"], 0)

    def test_missing_db_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"

            report = collect_smoke_report(db_path=db_path)

            self.assertEqual(report["status"], "missing_db")
            self.assertEqual(report["recommendation"], "FAIL")
            self.assertFalse(report["reload_safe_candidate"])
            self.assertFalse(db_path.exists())

    def test_collect_opens_existing_db_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-10 minutes",
                {
                    "pid": 222,
                    "boot_id": "current-boot",
                    "code_version": DEFAULT_EXPECTED_CODE_VERSION,
                },
            )
            calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
            real_connect = sqlite3.connect

            def _connect(*args, **kwargs):
                calls.append((args, kwargs))
                return real_connect(*args, **kwargs)

            with patch("scripts.audit.watchdog_stale_filter_smoke.sqlite3.connect", _connect):
                report = collect_smoke_report(db_path=db_path)

            self.assertEqual(report["status"], "safe_candidate")
            self.assertTrue(calls)
            self.assertIn("?mode=ro", str(calls[0][0][0]))
            self.assertTrue(calls[0][1].get("uri"))

    def test_cli_json_defaults_to_dry_run_and_prints_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-10 minutes",
                {
                    "pid": 222,
                    "boot_id": "current-boot",
                    "code_version": DEFAULT_EXPECTED_CODE_VERSION,
                },
            )
            stdout = io.StringIO()

            rc = main(["--db", str(db_path), "--json"], stdout=stdout)

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["status"], "safe_candidate")
            self.assertEqual(payload["recommendation"], "PASS")
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["side_effects"]["launchd"], "not_touched")
            self.assertIn("next_manual_step", payload)
            self.assertGreater(len(payload["not_executed_commands"]), 0)

    def test_cli_json_redacts_sensitive_payload_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-20 minutes",
                {
                    "error": "database is locked",
                    "api_key": "sk-secret-test-value",
                    "token": "secret-token-value",
                    "pid": 111,
                    "boot_id": "old-boot",
                    "code_version": "old",
                },
            )
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-10 minutes",
                {
                    "pid": 222,
                    "boot_id": "current-boot",
                    "code_version": DEFAULT_EXPECTED_CODE_VERSION,
                },
            )
            stdout = io.StringIO()

            rc = main(["--db", str(db_path), "--json"], stdout=stdout)

            self.assertEqual(rc, 0)
            rendered = stdout.getvalue()
            self.assertNotIn("sk-secret-test-value", rendered)
            self.assertNotIn("secret-token-value", rendered)
            self.assertIn("REDACTED", rendered)

    def test_script_has_no_restart_or_reload_execution_calls(self) -> None:
        source = Path("scripts/audit/watchdog_stale_filter_smoke.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        forbidden_calls: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "subprocess":
                    forbidden_calls.append(f"subprocess.{func.attr}")
                if func.value.id == "os" and func.attr in {"system", "popen", "spawnv"}:
                    forbidden_calls.append(f"os.{func.attr}")

        self.assertEqual(forbidden_calls, [])

    def test_runbook_documents_dry_run_smoke_boundary(self) -> None:
        text = Path("docs/OPERATIONS_RUNBOOK.md").read_text(encoding="utf-8")

        self.assertIn("scripts/audit/watchdog_stale_filter_smoke.py", text)
        self.assertIn("agent_startup_context", text)
        self.assertIn(DEFAULT_EXPECTED_CODE_VERSION, text)
        self.assertIn("does not reload", text)
        self.assertIn("not_executed_commands", text)
        self.assertIn("manually after reviewing the report", text)


if __name__ == "__main__":
    unittest.main()
