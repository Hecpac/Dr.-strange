from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from claw_v2.diagnostics import acknowledge_events, collect_diagnostics, format_text
from claw_v2.jobs import JobService
from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger


def _completed(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


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


class DiagnosticsTests(unittest.TestCase):
    def test_collects_process_port_database_jobs_tasks_and_actionable_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
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
            JobService(db_path).enqueue(kind="notebooklm.research", payload={"topic": "ai"}, job_id="job-1")
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
                conn.execute("INSERT INTO cron_state (job_name, last_run_at, runs) VALUES (?, ?, ?)", ("rook.health", 123.0, 2))
                conn.commit()

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner, limit=5)

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
            self.assertEqual(report["database"]["observe"]["latest_errors"][0]["payload"]["reason"], "rate_limited")
            self.assertIn("Claw diagnostics: attention", format_text(report))
            self.assertIn("llm_circuit_open", format_text(report))

    def test_missing_database_does_not_create_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner)

            self.assertFalse(db_path.exists())
            self.assertEqual(report["checks"]["status"], "critical")
            self.assertFalse(report["checks"]["database_readable"])
            self.assertEqual(report["database"], {"present": False, "error": "database not found"})

    def test_old_actionable_events_do_not_keep_status_in_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            observe = ObserveStream(db_path)
            observe.emit("scheduled_job_error", payload={"job": "old", "error": "timeout"})
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE observe_stream SET timestamp = datetime('now', '-3 days')")
                conn.commit()

            report = collect_diagnostics(db_path=db_path, port=8765, runner=_healthy_runner, limit=5)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["database"]["observe"]["latest_errors"], [])

    def test_acknowledged_events_do_not_keep_status_in_attention(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "claw.db"
            ack_path = Path(tmpdir) / "acks.json"
            observe = ObserveStream(db_path)
            observe.emit("firecrawl_paused", payload={"reason": "insufficient_credits"})

            attention = collect_diagnostics(db_path=db_path, ack_path=ack_path, port=8765, runner=_healthy_runner)
            event_id = attention["database"]["observe"]["latest_errors"][0]["id"]
            acknowledge_events([event_id], ack_path=ack_path, hours=2, reason="known external credits")
            report = collect_diagnostics(db_path=db_path, ack_path=ack_path, port=8765, runner=_healthy_runner)

            self.assertEqual(report["checks"]["status"], "healthy")
            self.assertEqual(report["checks"]["recent_error_events"], 0)
            self.assertEqual(report["checks"]["acknowledged_error_events"], 1)
            self.assertEqual(report["database"]["observe"]["latest_errors"], [])
            self.assertEqual(
                report["database"]["observe"]["acknowledged_errors"][0]["acknowledgement"]["reason"],
                "known external credits",
            )


class DiagnosticsRunbookTests(unittest.TestCase):
    def test_diagnose_wrapper_has_valid_bash_syntax(self) -> None:
        subprocess.run(["bash", "-n", "scripts/diagnose.sh"], check=True)
        subprocess.run(["bash", "-n", "scripts/restart.sh"], check=True)

    def test_runbook_documents_core_operational_commands(self) -> None:
        text = Path("docs/OPERATIONS_RUNBOOK.md").read_text()
        restart = Path("scripts/restart.sh").read_text()

        for expected in (
            "com.pachano.claw",
            "bash scripts/diagnose.sh",
            "bash scripts/restart.sh",
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


if __name__ == "__main__":
    unittest.main()
