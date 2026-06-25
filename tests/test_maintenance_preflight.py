from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.maintenance_preflight import collect_maintenance_preflight


def _env(*, maintenance: bool, f2: bool, no_job_claim: bool = False) -> dict[str, str]:
    return {
        "CLAW_MAINTENANCE_MODE": "1" if maintenance else "0",
        "CLAW_NO_JOB_CLAIM": "1" if no_job_claim else "0",
        "CLAW_F2_DURABILITY_ENABLED": "1" if f2 else "0",
        "F2_DURABILITY_ENABLED": "1" if f2 else "0",
        "CLAW_PENDING_VERIFICATION_DRAIN_APPLY": "1",
    }


class MaintenancePreflightTests(unittest.TestCase):
    def test_preflight_passes_with_maintenance_on_and_f2_off(self) -> None:
        report = collect_maintenance_preflight(env=_env(maintenance=True, f2=False))

        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["claim_path"], "PASS")
        self.assertEqual(report["scheduler_path"], "PASS")
        self.assertEqual(report["drain_path"], "PASS")
        self.assertTrue(report["maintenance_mode_active"])
        self.assertFalse(report["f2_enabled"])
        self.assertEqual(report["db_path_checked"], "temp")
        self.assertIn("maintenance_preflight_passed", report["reasons"])

    def test_preflight_passes_with_maintenance_on_and_f2_on(self) -> None:
        report = collect_maintenance_preflight(env=_env(maintenance=True, f2=True))

        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["claim_path"], "PASS")
        self.assertEqual(report["scheduler_path"], "PASS")
        self.assertEqual(report["drain_path"], "PASS")
        self.assertTrue(report["f2_enabled"])

    def test_preflight_fails_when_maintenance_is_off(self) -> None:
        report = collect_maintenance_preflight(env=_env(maintenance=False, f2=False))

        self.assertEqual(report["overall_status"], "FAIL")
        self.assertFalse(report["maintenance_mode_active"])
        self.assertIn("maintenance_mode_inactive", report["reasons"])
        self.assertEqual(report["claim_path"], "FAIL")
        self.assertEqual(report["scheduler_path"], "FAIL")
        self.assertEqual(report["drain_path"], "FAIL")

    def test_claim_path_fails_if_runtime_claim_gates_are_inactive(self) -> None:
        with patch("claw_v2.jobs.job_claim_block_reason", return_value=None):
            report = collect_maintenance_preflight(env=_env(maintenance=True, f2=False))

        self.assertEqual(report["overall_status"], "FAIL")
        self.assertEqual(report["claim_path"], "FAIL")
        claim_reasons = report["checks"]["claim_path"]["reasons"]
        self.assertIn("claim_transitioned_to_running", claim_reasons)
        self.assertIn("claim_next_transitioned_to_running", claim_reasons)

    def test_scheduler_path_fails_if_scheduled_work_would_enqueue(self) -> None:
        with patch("claw_v2.maintenance_preflight.scheduler_work_block_reason", return_value=None):
            report = collect_maintenance_preflight(env=_env(maintenance=True, f2=False))

        self.assertEqual(report["overall_status"], "FAIL")
        self.assertEqual(report["scheduler_path"], "FAIL")
        scheduler_reasons = report["checks"]["scheduler_path"]["reasons"]
        self.assertIn("scheduler_gate_inactive", scheduler_reasons)
        self.assertIn("scheduled_work_would_enqueue", scheduler_reasons)
        scheduler_details = report["checks"]["scheduler_path"]["details"]
        self.assertTrue(scheduler_details["approval_sweep_enqueued"])
        self.assertTrue(scheduler_details["pipeline_poll_merges_enqueued"])

    def test_drain_path_fails_if_apply_would_run(self) -> None:
        with patch("claw_v2.daemon.drain_apply_block_reason", return_value=None):
            report = collect_maintenance_preflight(env=_env(maintenance=True, f2=False))

        self.assertEqual(report["overall_status"], "FAIL")
        self.assertEqual(report["drain_path"], "FAIL")
        drain_reasons = report["checks"]["drain_path"]["reasons"]
        self.assertIn("drain_reconcilable_unverified_apply_called", drain_reasons)
        self.assertIn("drain_apply_not_disabled", drain_reasons)
        drain_details = report["checks"]["drain_path"]["details"]
        self.assertTrue(drain_details["observe_only_reconciliation_ran"])
        self.assertEqual(drain_details["drain_apply_calls"], 1)

    def test_output_is_structured_with_path_level_reasons(self) -> None:
        report = collect_maintenance_preflight(env=_env(maintenance=True, f2=False))

        for field in (
            "overall_status",
            "claim_path",
            "scheduler_path",
            "drain_path",
            "maintenance_mode_active",
            "no_job_claim_active",
            "f2_enabled",
            "db_path_checked",
            "reasons",
            "checks",
        ):
            self.assertIn(field, report)
        for path in ("claim_path", "scheduler_path", "drain_path"):
            self.assertIn("status", report["checks"][path])
            self.assertIn("reasons", report["checks"][path])
            self.assertIn("details", report["checks"][path])
            self.assertTrue(report["checks"][path]["reasons"])

    def test_cli_smoke_outputs_json_pass_with_temp_state(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "claw_v2.maintenance_preflight",
                "--maintenance-mode",
                "on",
                "--f2",
                "off",
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        report = json.loads(result.stdout)
        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["claim_path"], "PASS")
        self.assertEqual(report["scheduler_path"], "PASS")
        self.assertEqual(report["drain_path"], "PASS")
        self.assertFalse(report["f2_enabled"])

    def test_supplied_db_path_is_opened_read_only_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "read-only-check.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()

            report = collect_maintenance_preflight(
                db_path=db_path,
                env=_env(maintenance=True, f2=False),
            )

        self.assertEqual(report["overall_status"], "PASS")
        self.assertEqual(report["db_path_checked"], str(db_path))
        self.assertEqual(report["checks"]["db_path"]["status"], "PASS")
        self.assertIn(
            "db_opened_read_only_immutable",
            report["checks"]["db_path"]["reasons"],
        )
