from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claw_v2 import liveness
from claw_v2.diagnostics import acknowledge_events, collect_diagnostics, format_text
from claw_v2.jobs import JobService
from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger
from claw_v2.watchdog import is_restartable


def _completed(
    args: list[str], returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def _healthy_runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if args[:2] == ["launchctl", "list"]:
        return _completed(args, 0, "123\t0\tcom.pachano.claw\n")
    if args[:2] == ["launchctl", "print"]:
        return _completed(args, 0, "state = running\n")
    if args[:2] == ["pgrep", "-fl"]:
        return _completed(args, 0, "123 .venv/bin/python -m claw_v2.main\n")
    if args and args[0] == "lsof":
        return _completed(args, 0, "Python 123 user 3u IPv4 TCP 127.0.0.1:8765 (LISTEN)\n")
    return _completed(args, 1, "", "unknown command")


def _sandboxed_process_runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if args[:2] == ["launchctl", "list"]:
        return _completed(args, 1, "", "")
    if args[:2] == ["launchctl", "print"]:
        return _completed(args, 0, "state = running\npid = 123\n")
    if args[:2] == ["pgrep", "-fl"]:
        return _completed(
            args,
            3,
            "",
            "sysmon request failed with error: sysmond service not found\npgrep: Cannot get process list",
        )
    if args and args[0] == "lsof":
        return _completed(args, 0, "Python 123 user 3u IPv4 TCP 127.0.0.1:8765 (LISTEN)\n")
    return _completed(args, 1, "", "unknown command")


def _port_probe_failure_runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if args[:2] == ["launchctl", "list"]:
        return _completed(args, 0, "123\t0\tcom.pachano.claw\n")
    if args[:2] == ["launchctl", "print"]:
        return _completed(args, 0, "state = running\n")
    if args[:2] == ["pgrep", "-fl"]:
        return _completed(args, 0, "123 .venv/bin/python -m claw_v2.main\n")
    if args and args[0] == "lsof":
        return _completed(args, 1, "", "")
    return _completed(args, 1, "", "unknown command")


def _leaky_runner(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if args[:2] == ["launchctl", "list"]:
        return _completed(args, 0, "OPENAI_API_KEY=sk-test-very-secret-token-123456\n")
    if args[:2] == ["launchctl", "print"]:
        return _completed(args, 0, "state = running\n")
    if args[:2] == ["pgrep", "-fl"]:
        return _completed(args, 0, "123 .venv/bin/python -m claw_v2.main\n")
    if args and args[0] == "lsof":
        return _completed(args, 0, "Python 123 user 3u IPv4 TCP 127.0.0.1:8765 (LISTEN)\n")
    return _completed(args, 1, "", "ANTHROPIC_API_KEY=sk-ant-api03-secret-token-123456789\n")


def _insert_event_at(
    db_path: Path,
    event_type: str,
    timestamp_modifier: str,
    payload: dict[str, object],
) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            INSERT INTO observe_stream (timestamp, event_type, payload)
            VALUES (datetime('now', ?), ?, ?)
            RETURNING id
            """,
            (timestamp_modifier, event_type, json.dumps(payload)),
        ).fetchone()
        conn.commit()
    assert row is not None
    return int(row[0])


def _set_event_timestamp(db_path: Path, event_id: int, timestamp: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE observe_stream SET timestamp = ? WHERE id = ?",
            (timestamp, event_id),
        )
        conn.commit()


def _seed_current_daemon_window(
    db_path: Path,
    *,
    pid: int = 123,
    boot_id: str = "boot-current",
    code_version: str = "test-code",
    startup_modifier: str = "-10 minutes",
    sink_ts: float | None = None,
    web_transport_serving: bool = True,
) -> None:
    ObserveStream(db_path)
    _insert_event_at(
        db_path,
        "agent_startup_context",
        startup_modifier,
        {"pid": pid, "boot_id": boot_id, "code_version": code_version},
    )
    liveness.write_liveness(
        liveness.liveness_sink_path(db_path.parent),
        {
            "pid": pid,
            "boot_id": boot_id,
            "ts": time.time() if sink_ts is None else sink_ts,
            "web_transport_serving": web_transport_serving,
            "source": "test",
        },
    )


class DiagnosticsTests(unittest.TestCase):
    def test_collects_process_port_database_jobs_tasks_and_actionable_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            _seed_current_daemon_window(db_path)
            observe.emit(
                "llm_circuit_open",
                lane="brain",
                provider="openai",
                model="gpt-5.4-mini",
                trace_id="trace-1",
                job_id="job-1",
                artifact_id="artifact-1",
                payload={"reason": "rate_limited"},
            )
            JobService(db_path).enqueue(
                kind="notebooklm.research", payload={"topic": "ai"}, job_id="job-1"
            )
            TaskLedger(db_path).create(
                task_id="task-1",
                session_id="telegram-1",
                objective="audit agent",
                runtime="coordinator",
                provider="codex",
                model="gpt-5.3-codex",
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS cron_state (job_name TEXT PRIMARY KEY, last_run_at REAL NOT NULL DEFAULT 0.0, runs INTEGER NOT NULL DEFAULT 0)"
                )
                conn.execute(
                    "INSERT INTO cron_state (job_name, last_run_at, runs) VALUES (?, ?, ?)",
                    ("rook.health", 123.0, 2),
                )
                conn.commit()

            report = collect_diagnostics(
                db_path=db_path, port=8765, runner=_healthy_runner, limit=5
            )

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertTrue(report["checks"]["launchd_loaded"])
            self.assertTrue(report["checks"]["process_running"])
            self.assertTrue(report["checks"]["port_listening"])
            self.assertTrue(report["checks"]["database_readable"])
            self.assertEqual(report["checks"]["active_jobs"], 1)
            self.assertEqual(report["checks"]["active_tasks"], 1)
            self.assertEqual(report["checks"]["recent_error_events"], 1)
            self.assertEqual(report["database"]["jobs"]["counts"], {"queued": 1})
            self.assertEqual(report["database"]["jobs"]["active"][0]["job_id"], "job-1")
            self.assertEqual(report["database"]["tasks"]["counts"], {"queued": 1})
            self.assertEqual(report["database"]["tasks"]["active"][0]["task_id"], "task-1")
            self.assertEqual(report["database"]["cron"][0]["job_name"], "rook.health")
            self.assertEqual(
                report["database"]["observe"]["latest_errors"][0]["payload"]["reason"],
                "rate_limited",
            )
            self.assertIn("Dr. Strange diagnostics: attention", format_text(report))
            self.assertIn("llm_circuit_open", format_text(report))

    def test_missing_database_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertFalse(db_path.exists())
            self.assertEqual(report["checks"]["status"], "critical")
            self.assertFalse(report["checks"]["database_readable"])
            self.assertEqual(report["database"], {"present": False, "error": "database not found"})

    def test_malformed_database_reports_error_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            db_path.write_text("not sqlite", encoding="utf-8")

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "critical")
            self.assertFalse(report["checks"]["database_readable"])
            self.assertTrue(report["database"]["present"])
            self.assertIn("file is not a database", report["database"]["error"])

    def test_old_actionable_events_do_not_keep_status_in_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit("scheduled_job_error", payload={"job": "old", "error": "timeout"})
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE observe_stream SET timestamp = datetime('now', '-3 days')")
                conn.commit()

            report = collect_diagnostics(
                db_path=db_path, port=8765, runner=_healthy_runner, limit=5
            )

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["database"]["observe"]["latest_errors"], [])

    def test_process_probe_failure_uses_launchd_and_port_as_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path).emit(
                "daemon_heartbeat",
                payload={"pid": 123, "ts": time.time(), "web_transport_serving": True},
            )

            report = collect_diagnostics(
                db_path=db_path, port=8765, runner=_sandboxed_process_runner
            )

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertTrue(report["checks"]["process_running"])
            self.assertTrue(report["checks"]["launchd_loaded"])
            self.assertTrue(report["checks"]["port_listening"])

    def test_acknowledged_events_do_not_keep_status_in_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ack_path = Path(tmpdir) / "acks.json"
            observe = ObserveStream(db_path)
            _seed_current_daemon_window(db_path)
            observe.emit("firecrawl_paused", payload={"reason": "insufficient_credits"})

            attention = collect_diagnostics(
                db_path=db_path, ack_path=ack_path, port=8765, runner=_healthy_runner
            )
            event_id = attention["database"]["observe"]["latest_errors"][0]["id"]
            acknowledge_events(
                [event_id], ack_path=ack_path, hours=2, reason="known external credits"
            )
            report = collect_diagnostics(
                db_path=db_path, ack_path=ack_path, port=8765, runner=_healthy_runner
            )

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["acknowledged_error_events"], 1)
            self.assertEqual(report["database"]["observe"]["latest_errors"], [])
            self.assertEqual(
                report["database"]["observe"]["acknowledged_errors"][0]["acknowledgement"][
                    "reason"
                ],
                "known external credits",
            )

    def test_report_redacts_sensitive_command_output_and_event_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "llm_circuit_open",
                payload={
                    "reason": (
                        "policy block: https://claude.com/form/cyber-use-case"
                        "?token=secret-token-123456789"
                    )
                },
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_leaky_runner)
            rendered = json.dumps(report)
            text = format_text(report)

            self.assertNotIn("sk-test-very-secret-token", rendered)
            self.assertNotIn("secret-token-123456789", rendered)
            self.assertNotIn("sk-test-very-secret-token", text)
            self.assertNotIn("secret-token-123456789", text)
            self.assertIn("[REDACTED]", rendered)


class HeartbeatTests(unittest.TestCase):
    def test_fresh_heartbeat_keeps_status_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 99, "ts": time.time(), "web_transport_serving": True},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertTrue(report["checks"]["heartbeat_present"])
            self.assertFalse(report["checks"]["heartbeat_stale"])
            self.assertEqual(report["checks"]["web_transport_serving"], True)
            self.assertEqual(report["database"]["heartbeat"]["web_transport_serving"], True)

    def test_stale_heartbeat_flags_critical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 99, "ts": time.time() - 600, "web_transport_serving": True},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "critical")
            self.assertTrue(report["checks"]["heartbeat_stale"])

    def test_web_transport_thread_dead_flags_critical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 99, "ts": time.time(), "web_transport_serving": False},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "critical")
            self.assertEqual(report["checks"]["web_transport_serving"], False)

    def test_fresh_heartbeat_prevents_transient_port_probe_from_restartable_critical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 99, "ts": time.time(), "web_transport_serving": True},
            )

            report = collect_diagnostics(
                db_path=db_path,
                port=8765,
                runner=_port_probe_failure_runner,
            )

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertFalse(report["checks"]["port_listening"])
            self.assertTrue(report["checks"]["heartbeat_present"])
            self.assertFalse(report["checks"]["heartbeat_stale"])

    def test_fresh_liveness_heartbeat_without_web_state_prevents_port_probe_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 99, "ts": time.time(), "source": "daemon_liveness_loop"},
            )

            report = collect_diagnostics(
                db_path=db_path,
                port=8765,
                runner=_port_probe_failure_runner,
            )

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertFalse(report["checks"]["port_listening"])
            self.assertIsNone(report["checks"]["web_transport_serving"])

    def test_missing_heartbeat_does_not_penalize_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit("nlm_research_degraded", payload={"reason": "rate"})
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE observe_stream SET timestamp = datetime('now', '-3 days')")
                conn.commit()

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertFalse(report["checks"]["heartbeat_present"])
            self.assertFalse(report["checks"]["heartbeat_stale"])


class HeartbeatSinkTests(unittest.TestCase):
    """F0.3: diagnostics reads the daemon liveness signal from the atomic JSON
    sink (single source of truth) when present, and only falls back to the
    observe_stream query when the sink is missing/unreadable."""

    def _write_sink(self, db_path: Path, payload: dict[str, object]) -> None:
        from claw_v2 import liveness

        liveness.write_liveness(liveness.liveness_sink_path(db_path.parent), payload)

    def test_fresh_sink_keeps_status_healthy_from_sink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            # observe_stream must exist for the heartbeat block to run.
            ObserveStream(db_path).emit("noop_marker", payload={})
            self._write_sink(
                db_path,
                {
                    "pid": 99,
                    "ts": time.time(),
                    "boot_id": "b1",
                    "web_transport_serving": True,
                    "source": "lifecycle",
                },
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertTrue(report["checks"]["heartbeat_present"])
            self.assertFalse(report["checks"]["heartbeat_stale"])
            self.assertEqual(report["checks"]["web_transport_serving"], True)
            self.assertEqual(report["database"]["heartbeat"]["source"], "liveness_sink")

    def test_stale_sink_flags_critical_from_sink(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path).emit("noop_marker", payload={})
            self._write_sink(
                db_path,
                {
                    "pid": 99,
                    "ts": time.time() - 600,
                    "boot_id": "b1",
                    "web_transport_serving": True,
                    "source": "lifecycle",
                },
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "critical")
            self.assertTrue(report["checks"]["heartbeat_stale"])
            self.assertEqual(report["database"]["heartbeat"]["source"], "liveness_sink")

    def test_web_thread_dead_from_sink_flags_critical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path).emit("noop_marker", payload={})
            self._write_sink(
                db_path,
                {
                    "pid": 99,
                    "ts": time.time(),
                    "boot_id": "b1",
                    "web_transport_serving": False,
                    "source": "lifecycle",
                },
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "critical")
            self.assertEqual(report["checks"]["web_transport_serving"], False)

    def test_sink_is_authoritative_over_observe_stream(self) -> None:
        # observe_stream carries a STALE daemon_heartbeat, but a fresh sink must
        # win: do not combine ts-from-sink with web-from-observe.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 1, "ts": time.time() - 9000, "web_transport_serving": False},
            )
            self._write_sink(
                db_path,
                {
                    "pid": 99,
                    "ts": time.time(),
                    "boot_id": "b1",
                    "web_transport_serving": True,
                    "source": "lifecycle",
                },
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertFalse(report["checks"]["heartbeat_stale"])
            self.assertEqual(report["checks"]["web_transport_serving"], True)
            self.assertEqual(report["database"]["heartbeat"]["source"], "liveness_sink")

    def test_missing_sink_falls_back_to_observe_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit(
                "daemon_heartbeat",
                payload={"pid": 1, "ts": time.time(), "web_transport_serving": True},
            )
            # No sink file written → fallback path.
            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertTrue(report["checks"]["heartbeat_present"])
            self.assertFalse(report["checks"]["heartbeat_stale"])
            self.assertEqual(report["database"]["heartbeat"]["source"], "observe_stream")

    def test_no_sink_no_heartbeat_does_not_penalize_status(self) -> None:
        # Criterion 5 (first-boot safety): NO sink file and no heartbeat row →
        # present False → not stale.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit("nlm_research_degraded", payload={"reason": "rate"})
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE observe_stream SET timestamp = datetime('now', '-3 days')")
                conn.commit()

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertFalse(report["checks"]["heartbeat_present"])
            self.assertFalse(report["checks"]["heartbeat_stale"])


class CurrentDaemonWindowErrorFilteringTests(unittest.TestCase):
    def test_stale_pre_boot_database_lock_is_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-20 minutes",
                {"error": "database is locked", "pid": 123, "boot_id": "boot-current"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["stale_error_events"], 1)
            self.assertFalse(is_restartable(report["checks"]))
            self.assertEqual(report["database"]["observe"]["latest_errors"], [])
            self.assertEqual(
                report["database"]["observe"]["stale_historical_errors"][0]["event_type"],
                "daemon_tick_error",
            )

    def test_stale_pre_boot_traceback_is_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            _insert_event_at(
                db_path,
                "scheduled_job_error",
                "-30 minutes",
                {"traceback": "Traceback: RuntimeError before this daemon boot"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["stale_error_events"], 1)
            self.assertFalse(is_restartable(report["checks"]))

    def test_post_boot_database_lock_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "database is locked", "pid": 123, "boot_id": "boot-current"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertEqual(report["checks"]["recent_error_events"], 1)
            self.assertEqual(report["checks"]["stale_error_events"], 0)
            self.assertEqual(
                report["database"]["observe"]["latest_errors"][0]["payload"]["error"],
                "database is locked",
            )

    def test_post_boot_daemon_lifecycle_error_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "tick failed", "traceback": "Traceback: RuntimeError"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertEqual(report["checks"]["recent_error_events"], 1)
            self.assertEqual(
                report["database"]["observe"]["latest_errors"][0]["event_type"],
                "daemon_tick_error",
            )

    def test_llm_narrative_database_lock_match_is_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            _insert_event_at(
                db_path,
                "llm_response",
                "-5 minutes",
                {"text": "The previous incident said database is locked, but this is prose."},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["narrative_non_error_matches"], 1)
            self.assertEqual(
                report["database"]["observe"]["narrative_non_error_matches"][0]["event_type"],
                "llm_response",
            )

    def test_fresh_liveness_with_stale_historical_errors_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            for index in range(5):
                _insert_event_at(
                    db_path,
                    "daemon_tick_error",
                    "-20 minutes",
                    {"error": "database is locked", "sequence": index},
                )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["stale_error_events"], 5)
            self.assertFalse(report["checks"]["heartbeat_stale"])

    def test_stale_liveness_is_actionable_even_without_observe_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, sink_ts=time.time() - 600)

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "critical")
            self.assertTrue(report["checks"]["heartbeat_stale"])
            self.assertTrue(is_restartable(report["checks"]))

    def test_current_pid_and_boot_reset_the_relevant_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "agent_startup_context",
                "-30 minutes",
                {"pid": 111, "boot_id": "old-boot", "code_version": "old"},
            )
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-20 minutes",
                {"error": "old daemon failed", "pid": 111, "boot_id": "old-boot"},
            )
            _seed_current_daemon_window(
                db_path,
                pid=222,
                boot_id="new-boot",
                code_version="new",
                startup_modifier="-10 minutes",
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertTrue(report["checks"]["current_daemon_window_known"])
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["stale_error_events"], 1)
            self.assertEqual(
                report["database"]["observe"]["current_daemon_window"]["pid"],
                222,
            )

    def test_identity_mismatch_after_boot_is_stale_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(
                db_path,
                pid=222,
                boot_id="new-boot",
                code_version="new",
                startup_modifier="-10 minutes",
            )
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {
                    "error": "previous boot emitted late",
                    "pid": 111,
                    "boot_id": "old-boot",
                    "code_version": "old",
                },
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["stale_error_events"], 1)

    def test_missing_current_window_marks_error_relevance_unknown_not_restartable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "database is locked"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertFalse(report["checks"]["current_daemon_window_known"])
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 1)
            self.assertFalse(is_restartable(report["checks"]))

    def test_corrupt_liveness_sink_marks_error_relevance_unknown_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ObserveStream(db_path)
            liveness.liveness_sink_path(db_path.parent).write_text("{not-json", encoding="utf-8")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "database is locked"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertFalse(report["checks"]["current_daemon_window_known"])
            self.assertEqual(
                report["checks"]["current_daemon_window_missing_reason"],
                "missing_current_daemon_identity",
            )
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 1)
            self.assertFalse(is_restartable(report["checks"]))

    def test_malformed_event_timestamp_is_unknown_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            event_id = _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "database is locked"},
            )
            _set_event_timestamp(db_path, event_id, "not-a-timestamp")

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 1)
            self.assertFalse(is_restartable(report["checks"]))

    def test_future_skewed_event_timestamp_is_unknown_not_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "+1 day",
                {"error": "database is locked"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "attention")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 1)
            self.assertFalse(is_restartable(report["checks"]))

    def test_malformed_current_boot_timestamp_makes_window_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")
            startup_id = 1
            _set_event_timestamp(db_path, startup_id, "bad-startup-timestamp")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "database is locked"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertFalse(report["checks"]["current_daemon_window_known"])
            self.assertEqual(
                report["checks"]["current_daemon_window_missing_reason"],
                "invalid_current_boot_timestamp",
            )
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 1)

    def test_future_current_boot_timestamp_makes_window_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="+1 day")
            _insert_event_at(
                db_path,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "database is locked"},
            )

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertFalse(report["checks"]["current_daemon_window_known"])
            self.assertEqual(
                report["checks"]["current_daemon_window_missing_reason"],
                "future_current_boot_timestamp",
            )
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 1)

    def test_current_startup_event_only_establishes_window_without_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            _seed_current_daemon_window(db_path, startup_modifier="-10 minutes")

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertTrue(report["checks"]["current_daemon_window_known"])
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["stale_error_events"], 0)
            self.assertEqual(report["checks"]["unknown_relevance_error_events"], 0)

    def test_filter_report_separates_actionable_stale_narrative_and_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            known_db = Path(tmpdir) / "known.db"
            _seed_current_daemon_window(known_db, startup_modifier="-10 minutes")
            _insert_event_at(
                known_db,
                "scheduled_job_error",
                "-20 minutes",
                {"error": "stale"},
            )
            _insert_event_at(
                known_db,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "current"},
            )
            _insert_event_at(
                known_db,
                "llm_decision",
                "-5 minutes",
                {"thought": "database is locked appears in narrative"},
            )

            known = collect_diagnostics(
                db_path=known_db, port=8765, runner=_healthy_runner, limit=10
            )

            self.assertEqual(known["checks"]["recent_error_events"], 1)
            self.assertEqual(known["checks"]["stale_error_events"], 1)
            self.assertEqual(known["checks"]["narrative_non_error_matches"], 1)

            unknown_db = Path(tmpdir) / "unknown.db"
            ObserveStream(unknown_db)
            _insert_event_at(
                unknown_db,
                "daemon_tick_error",
                "-5 minutes",
                {"error": "unclassified"},
            )
            unknown = collect_diagnostics(
                db_path=unknown_db, port=8765, runner=_healthy_runner, limit=10
            )

            self.assertEqual(unknown["checks"]["unknown_relevance_error_events"], 1)
            self.assertEqual(unknown["checks"]["recent_error_events"], 0)


class AcknowledgementConcurrencyTests(unittest.TestCase):
    def test_parallel_acknowledge_events_does_not_lose_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ack_path = Path(tmpdir) / "acks.json"
            event_ids = list(range(1, 33))
            barrier = threading.Barrier(len(event_ids))

            def _ack_one(event_id: int) -> None:
                barrier.wait()
                acknowledge_events(
                    [event_id],
                    ack_path=ack_path,
                    hours=24,
                    reason=f"event-{event_id}",
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(event_ids)) as pool:
                list(pool.map(_ack_one, event_ids))

            data = json.loads(ack_path.read_text(encoding="utf-8"))
            recorded = {int(entry["event_id"]) for entry in data["acks"]}
            self.assertEqual(recorded, set(event_ids))

    def test_acknowledge_events_purges_expired_entries_on_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ack_path = Path(tmpdir) / "acks.json"
            past = time.time() - 3600
            acknowledge_events([777], ack_path=ack_path, hours=0.1, reason="stale", now=past)

            acknowledge_events([42], ack_path=ack_path, hours=24, reason="fresh")

            data = json.loads(ack_path.read_text(encoding="utf-8"))
            recorded = {int(entry["event_id"]) for entry in data["acks"]}
            self.assertEqual(recorded, {42})


class DiagnosticsRunbookTests(unittest.TestCase):
    def test_diagnose_wrapper_has_valid_bash_syntax(self) -> None:
        subprocess.run(["bash", "-n", "scripts/diagnose.sh"], check=True)
        subprocess.run(["bash", "-n", "scripts/restart.sh"], check=True)
        subprocess.run(["bash", "-n", "ops/claw-watchdog.sh"], check=True)
        subprocess.run(["bash", "-n", "ops/chrome-cdp-launcher.sh"], check=True)

    def test_runbook_documents_core_operational_commands(self) -> None:
        text = Path("docs/OPERATIONS_RUNBOOK.md").read_text()
        restart = Path("scripts/restart.sh").read_text()
        watchdog = Path("ops/claw-watchdog.sh").read_text()

        for expected in (
            "com.pachano.claw",
            "com.pachano.claw-watchdog",
            "com.claw.chrome-cdp",
            "bash scripts/diagnose.sh",
            "bash scripts/restart.sh",
            "ops/claw-watchdog.sh",
            "ops/chrome-cdp-launcher.sh",
            "127.0.0.1:8765",
            "/status",
            "/jobs",
            "/agent_status perf-optimizer",
            "/pipeline_status",
            "/trace <trace_id> [limit]",
        ):
            self.assertIn(expected, text)
        self.assertIn("--ack-current", text)
        self.assertIn("launchctl kickstart -k", restart)
        # Restart decision is debounced in the testable module (2026-06-13).
        self.assertIn("claw_v2.watchdog", watchdog)
        self.assertIn("restart threshold reached", watchdog)
        # The runbook documents the debounce knobs.
        self.assertIn("CLAW_WATCHDOG_STRIKES", text)
        self.assertIn("CLAW_WATCHDOG_BOOTSTRAP_GRACE_S", text)
        self.assertIn("CLAW_RESTART_PORT_WAIT_S", text)


if __name__ == "__main__":
    unittest.main()
