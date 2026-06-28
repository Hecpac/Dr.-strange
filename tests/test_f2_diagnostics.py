from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from claw_v2.diagnostics import collect_f2_recovery_report, main as diagnostics_main
from claw_v2.f2_durability_schema import F2_DURABILITY_TABLES
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb


class F2DiagnosticsTests(unittest.TestCase):
    def _runtime_db(self, tmpdir: str) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "runtime.db")
        self.addCleanup(db.close)
        return db

    def _store(self, tmpdir: str) -> tuple[F2DurabilityStore, RuntimeDb]:
        db = self._runtime_db(tmpdir)
        return F2DurabilityStore(db), db

    def _run_cli_json(self, *args: str) -> dict[str, object]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = diagnostics_main([*args, "--json"])
        self.assertEqual(exit_code, 0)
        return json.loads(stdout.getvalue())

    def _linked_effect(
        self,
        store: F2DurabilityStore,
        *,
        external_effect_id: str,
        status: str,
        task_id: str = "task-1",
        run_id: str = "run-1",
    ):
        effect = store.record_external_effect(
            external_effect_id=external_effect_id,
            task_id=task_id,
            run_id=run_id,
            phase="implementation",
            effect_kind="github_pr",
            target="Hecpac/repo#draft",
            request={"title": f"Draft PR {external_effect_id}"},
            created_at="2026-06-24T00:00:00Z",
        )
        if status != "intent_recorded":
            effect = store.update_external_effect_status(
                external_effect_id,
                status=status,
                verification={"status": status},
                result={"ok": status == "verified_applied"},
                updated_at="2026-06-24T00:01:00Z",
            )
        store.append_checkpoint_write(
            task_id=task_id,
            run_id=run_id,
            phase="implementation",
            write_kind="external_effect_intent",
            write_key=f"external-effect:{external_effect_id}",
            payload={"external_effect_id": external_effect_id},
            external_effect_id=external_effect_id,
            created_at="2026-06-24T00:02:00Z",
        )
        return effect

    def test_missing_db_path_returns_disabled_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"

            report = collect_f2_recovery_report(db_path)

            self.assertEqual(report["status"], "disabled")
            self.assertFalse(report["enabled"])
            self.assertEqual(report["reason"], "db_missing")
            self.assertFalse(db_path.exists())

    def test_db_without_f2_tables_returns_disabled_without_creating_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE app_data (id TEXT PRIMARY KEY)")

            report = collect_f2_recovery_report(db_path)

            self.assertEqual(report["status"], "disabled")
            self.assertFalse(report["enabled"])
            self.assertTrue(all(not value for value in report["tables_present"].values()))
            with sqlite3.connect(db_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertFalse(set(F2_DURABILITY_TABLES) & tables)
            self.assertEqual(tables, {"app_data"})

    def test_f2_report_counts_and_recent_records_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            write = store.append_checkpoint_write(
                write_id="write-1",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                write_kind="phase_started",
                payload={"secret": "sk-rawcheckpointsecret12345678901234567890"},
                created_at="2026-06-24T00:00:00Z",
            )
            checkpoint = store.create_phase_checkpoint(
                checkpoint_id="checkpoint-1",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                phase_version=1,
                status="started",
                last_write_order=write.write_order,
                payload={"secret": "sk-rawcheckpointsecret12345678901234567890"},
                created_at="2026-06-24T00:00:01Z",
            )
            effect = self._linked_effect(
                store,
                external_effect_id="effect-verified",
                status="verified_applied",
            )
            store.upsert_recovery_cursor(
                recovery_cursor_id="cursor-1",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                cursor_status="ready_to_resume_phase",
                last_checkpoint_id=checkpoint.checkpoint_id,
                last_write_order=write.write_order,
                resume_payload={"secret": "sk-resumesecret12345678901234567890"},
                updated_at="2026-06-24T00:03:00Z",
            )
            db.close()

            report = collect_f2_recovery_report(Path(tmpdir) / "runtime.db", limit=5)

            self.assertEqual(report["status"], "ok")
            self.assertTrue(report["enabled"])
            self.assertEqual(report["counts"]["phase_checkpoints"]["by_status"]["started"], 1)
            self.assertEqual(
                report["counts"]["phase_checkpoint_writes"]["by_write_kind"]["phase_started"],
                1,
            )
            self.assertEqual(
                report["counts"]["external_effect_records"]["by_status"]["verified_applied"],
                1,
            )
            self.assertEqual(
                report["counts"]["phase_recovery_cursors"]["by_cursor_status"][
                    "ready_to_resume_phase"
                ],
                1,
            )
            checkpoint_row = report["recent_records"]["phase_checkpoints"][0]
            self.assertEqual(checkpoint_row["checkpoint_id"], "checkpoint-1")
            self.assertEqual(checkpoint_row["payload_sha256"], checkpoint.payload_sha256)
            effect_row = report["recent_records"]["external_effect_records"][0]
            self.assertEqual(effect_row["external_effect_id"], effect.external_effect_id)
            self.assertEqual(effect_row["request_sha256"], effect.request_sha256)

            serialized = json.dumps(report, sort_keys=True)
            self.assertNotIn("payload_json", serialized)
            self.assertNotIn("request_json", serialized)
            self.assertNotIn("verification_json", serialized)
            self.assertNotIn("result_json", serialized)
            self.assertNotIn("resume_payload_json", serialized)
            self.assertNotIn("rawcheckpointsecret", serialized)
            self.assertNotIn("resumesecret", serialized)

    def test_f2_report_detects_orphaned_external_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            store.record_external_effect(
                external_effect_id="effect-orphaned",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="github_pr",
                target="Hecpac/repo#draft",
                request={"title": "Draft PR"},
            )
            db.close()

            report = collect_f2_recovery_report(Path(tmpdir) / "runtime.db")

            orphaned = report["external_effects"]["orphaned"]
            self.assertEqual(len(orphaned), 1)
            self.assertEqual(orphaned[0]["external_effect_id"], "effect-orphaned")
            self.assertEqual(orphaned[0]["reason"], "orphaned_external_effect")

    def test_f2_report_reports_manual_review_and_applied_unverified_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            unsafe_statuses = (
                "intent_recorded",
                "apply_in_progress",
                "applied",
                "failed",
                "verification_required",
                "blocked_manual_review",
            )
            for status in unsafe_statuses:
                self._linked_effect(
                    store,
                    external_effect_id=f"effect-{status}",
                    status=status,
                )
            db.close()

            report = collect_f2_recovery_report(Path(tmpdir) / "runtime.db")

            blockers = report["external_effects"]["manual_review_required"]
            blocker_statuses = {item["status"] for item in blockers}
            self.assertEqual(blocker_statuses, set(unsafe_statuses))
            applied = next(item for item in blockers if item["status"] == "applied")
            self.assertEqual(applied["reason"], "unsafe_external_effect_status")

    def test_f2_report_verified_absent_is_future_execution_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            self._linked_effect(
                store,
                external_effect_id="effect-absent",
                status="verified_absent",
            )
            db.close()

            report = collect_f2_recovery_report(Path(tmpdir) / "runtime.db")

            future = report["external_effects"]["verified_absent_requires_future_execution"]
            self.assertEqual(len(future), 1)
            self.assertEqual(future[0]["external_effect_id"], "effect-absent")
            self.assertEqual(future[0]["reason"], "verified_absent_future_execution_required")
            serialized = json.dumps(report, sort_keys=True)
            self.assertNotIn('"replay": true', serialized)
            self.assertNotIn("replay_allowed", serialized)

    def test_f2_report_redacts_sensitive_values_even_with_include_payload_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            store.append_checkpoint_write(
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                write_kind="phase_started",
                payload={
                    "authorization": "Bearer checkpointsecret12345678901234567890",
                    "cookie": "sid=checkpoint-cookie-secret",
                },
            )
            effect = store.record_external_effect(
                external_effect_id="effect-secret",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="telegram_send",
                target="chat:owner",
                request={
                    "api_key": "sk-requestsecret12345678901234567890",
                    "headers": {
                        "authorization": "Bearer requestsecret12345678901234567890",
                        "cookie": "sid=request-cookie-secret",
                    },
                },
                verification={
                    "access_token": "verify-secret-token",
                    "status": "pending",
                },
                result={
                    "cookie": "sid=result-cookie-secret",
                    "message_id": "42",
                },
                error="failed with Bearer errorsecret12345678901234567890",
            )
            store.update_external_effect_status(
                effect.external_effect_id,
                status="failed",
                result={"authorization": "Bearer resultsecret12345678901234567890"},
                error="failed with api_key=resultsecret12345678901234567890",
            )
            db.close()

            report = collect_f2_recovery_report(
                Path(tmpdir) / "runtime.db",
                include_payload=True,
            )

            self.assertFalse(report["payload_policy"]["raw_payloads_included"])
            serialized = json.dumps(report, sort_keys=True)
            for secret in (
                "checkpointsecret12345678901234567890",
                "checkpoint-cookie-secret",
                "sk-requestsecret",
                "requestsecret12345678901234567890",
                "request-cookie-secret",
                "verify-secret-token",
                "result-cookie-secret",
                "resultsecret12345678901234567890",
                "errorsecret12345678901234567890",
            ):
                self.assertNotIn(secret, serialized)
            self.assertNotIn("payload_json", serialized)
            self.assertNotIn("request_json", serialized)
            self.assertNotIn("verification_json", serialized)
            self.assertNotIn("result_json", serialized)

    def test_f2_report_readiness_checks_are_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            self._linked_effect(
                store,
                external_effect_id="effect-absent",
                status="verified_absent",
            )
            db.close()

            report = collect_f2_recovery_report(Path(tmpdir) / "runtime.db")

            codes = {item["code"]: item for item in report["readiness"]["checks"]}
            self.assertIn("f2_flags_disabled_by_default", codes)
            self.assertEqual(codes["f2_flags_disabled_by_default"]["status"], "pass")
            self.assertEqual(codes["diagnostics_read_only"]["status"], "pass")
            self.assertEqual(codes["no_diagnostics_migration_or_write_path"]["status"], "pass")
            self.assertEqual(codes["no_replay_or_execution_behavior"]["status"], "pass")
            self.assertEqual(
                codes["verified_absent_future_execution_count"]["status"],
                "warning",
            )
            self.assertNotIn("production_ready", report["readiness"])

    def test_f2_report_limit_is_clamped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            for index in range(105):
                store.create_phase_checkpoint(
                    checkpoint_id=f"checkpoint-{index}",
                    task_id="task-1",
                    run_id="run-1",
                    phase="research",
                    phase_version=index + 1,
                    status="started",
                    payload={"index": index},
                    created_at=f"2026-06-24T00:{index // 60:02d}:{index % 60:02d}Z",
                )
            db.close()

            report = collect_f2_recovery_report(Path(tmpdir) / "runtime.db", limit=1000)

            self.assertEqual(report["limit"], 100)
            self.assertEqual(report["requested_limit"], 1000)
            self.assertEqual(len(report["recent_records"]["phase_checkpoints"]), 100)

    def test_f2_cli_requires_explicit_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr = io.StringIO()

            with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                diagnostics_main(["--f2-recovery-report", "--json"])

            self.assertEqual(raised.exception.code, 2)
            self.assertIn("--f2-db", stderr.getvalue())
            self.assertEqual(list(Path(tmpdir).iterdir()), [])

    def test_f2_cli_missing_db_returns_disabled_without_creating_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "missing.db"

            output = self._run_cli_json(
                "--f2-recovery-report",
                "--f2-db",
                str(db_path),
            )

            self.assertIn("f2_recovery", output)
            report = output["f2_recovery"]
            self.assertEqual(report["status"], "disabled")
            self.assertFalse(report["enabled"])
            self.assertEqual(report["reason"], "db_missing")
            self.assertFalse(db_path.exists())

    def test_f2_cli_db_without_tables_does_not_create_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE app_data (id TEXT PRIMARY KEY)")

            output = self._run_cli_json(
                "--f2-recovery-report",
                "--f2-db",
                str(db_path),
            )

            report = output["f2_recovery"]
            self.assertEqual(report["status"], "disabled")
            with sqlite3.connect(db_path) as conn:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
            self.assertEqual(tables, {"app_data"})
            self.assertFalse(set(F2_DURABILITY_TABLES) & tables)

    def test_f2_cli_json_output_contains_readiness_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            write = store.append_checkpoint_write(
                task_id="task-1",
                run_id="run-1",
                phase="research",
                write_kind="phase_started",
                payload={"phase": "research"},
                created_at="2026-06-24T00:00:00Z",
            )
            store.create_phase_checkpoint(
                checkpoint_id="checkpoint-1",
                task_id="task-1",
                run_id="run-1",
                phase="research",
                phase_version=1,
                status="started",
                last_write_order=write.write_order,
                payload={"phase": "research"},
                created_at="2026-06-24T00:00:01Z",
            )
            self._linked_effect(
                store,
                external_effect_id="effect-absent",
                status="verified_absent",
            )
            db.close()

            output = self._run_cli_json(
                "--f2-recovery-report",
                "--f2-db",
                str(Path(tmpdir) / "runtime.db"),
            )

            report = output["f2_recovery"]
            codes = {item["code"] for item in report["readiness"]["checks"]}
            self.assertIn("f2_flags_disabled_by_default", codes)
            self.assertIn("diagnostics_read_only", codes)
            self.assertIn("no_diagnostics_migration_or_write_path", codes)
            self.assertIn("no_replay_or_execution_behavior", codes)
            self.assertIn("counts", report)
            self.assertIn("recent_records", report)
            self.assertIn("external_effects", report)

    def test_f2_cli_include_payload_still_redacts_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            store.append_checkpoint_write(
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                write_kind="phase_started",
                payload={
                    "api_key": "sk-cli-checkpointsecret12345678901234567890",
                    "cookie": "sid=cli-checkpoint-cookie-secret",
                },
            )
            store.record_external_effect(
                external_effect_id="effect-cli-secret",
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                effect_kind="telegram_send",
                target="chat:owner",
                request={
                    "authorization": "Bearer cli-requestsecret12345678901234567890",
                    "cookie": "sid=cli-request-cookie-secret",
                },
                result={"api_key": "sk-cli-resultsecret12345678901234567890"},
                error="failed with token=cli-errorsecret12345678901234567890",
            )
            db.close()

            output = self._run_cli_json(
                "--f2-recovery-report",
                "--f2-db",
                str(Path(tmpdir) / "runtime.db"),
                "--include-payload",
            )

            self.assertFalse(output["f2_recovery"]["payload_policy"]["raw_payloads_included"])
            serialized = json.dumps(output, sort_keys=True)
            for secret in (
                "cli-checkpointsecret12345678901234567890",
                "cli-checkpoint-cookie-secret",
                "cli-requestsecret12345678901234567890",
                "cli-request-cookie-secret",
                "cli-resultsecret12345678901234567890",
                "cli-errorsecret12345678901234567890",
            ):
                self.assertNotIn(secret, serialized)
            for raw_field in (
                "payload_json",
                "request_json",
                "verification_json",
                "result_json",
                "resume_payload_json",
            ):
                self.assertNotIn(raw_field, serialized)

    def test_f2_cli_task_run_filters_are_passed_to_collector(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store, db = self._store(tmpdir)
            store.create_phase_checkpoint(
                checkpoint_id="checkpoint-explicit",
                task_id="task-1",
                run_id="run-explicit",
                phase="research",
                phase_version=1,
                status="started",
                payload={"scope": "explicit"},
            )
            store.create_phase_checkpoint(
                checkpoint_id="checkpoint-fallback",
                task_id="task-1",
                run_id="task-1",
                phase="research",
                phase_version=1,
                status="started",
                payload={"scope": "fallback"},
            )
            db.close()

            output = self._run_cli_json(
                "--f2-recovery-report",
                "--f2-db",
                str(Path(tmpdir) / "runtime.db"),
                "--task-id",
                "task-1",
                "--run-id",
                "run-explicit",
            )

            report = output["f2_recovery"]
            self.assertEqual(report["counts"]["phase_checkpoints"]["total"], 1)
            checkpoints = report["recent_records"]["phase_checkpoints"]
            self.assertEqual(len(checkpoints), 1)
            self.assertEqual(checkpoints[0]["checkpoint_id"], "checkpoint-explicit")
            self.assertEqual(checkpoints[0]["run_id"], "run-explicit")

    def test_existing_generic_diagnostics_behavior_is_preserved(self) -> None:
        generic_report = {"label": "test", "checks": {"status": "ok"}}
        with patch(
            "claw_v2.diagnostics.collect_diagnostics",
            return_value=generic_report,
        ) as collect:
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = diagnostics_main(["--db", "local-only.db", "--json"])

        self.assertEqual(exit_code, 0)
        collect.assert_called_once()
        self.assertEqual(json.loads(stdout.getvalue()), generic_report)
        self.assertNotIn("f2_recovery", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
