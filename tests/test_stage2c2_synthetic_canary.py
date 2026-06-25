from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2 import stage2c2_synthetic_canary as canary
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.f2_recovery import F2RecoveryStatus
from claw_v2.sqlite_runtime import RuntimeDb

_REQUIRED_JSON_FIELDS = (
    "overall_status",
    "db_path_checked",
    "temp_db_only",
    "synthetic_prefix",
    "phase_checkpoint_path",
    "recovery_planner_path",
    "external_effect_path",
    "counts_before",
    "counts_after",
    "synthetic_ids",
    "reasons",
    "primary_db_touched",
    "non_synthetic_records_created",
    "real_external_effects_executed",
    "does_not_prove",
)


class Stage2C2SyntheticCanaryTests(unittest.TestCase):
    def _store(self, root: Path) -> tuple[RuntimeDb, F2DurabilityStore]:
        db = RuntimeDb(root / "claw.db")
        self.addCleanup(db.close)
        return db, F2DurabilityStore(db)

    # 1. Harness PASS on temp DB.
    def test_harness_passes_on_temp_db(self) -> None:
        report = canary.run_stage2c2_synthetic_canary()
        self.assertEqual(report["overall_status"], canary.PASS)
        self.assertTrue(report["temp_db_only"])
        self.assertFalse(report["primary_db_touched"])
        self.assertEqual(report["phase_checkpoint_path"], canary.PASS)
        self.assertEqual(report["recovery_planner_path"], canary.PASS)
        self.assertEqual(report["external_effect_path"], canary.PASS)

    # 2. Harness refuses a supplied DB path by default and never touches it.
    def test_refuses_supplied_db_path_and_leaves_it_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "primary.db"
            target.write_bytes(b"SENTINEL-NOT-A-REAL-DB")
            before_mtime = target.stat().st_mtime_ns
            before_bytes = target.read_bytes()

            report = canary.run_stage2c2_synthetic_canary(db_path=str(target))

            self.assertEqual(report["overall_status"], canary.FAIL)
            self.assertFalse(report["primary_db_touched"])
            self.assertFalse(report["temp_db_only"])
            self.assertIn(
                "non_temp_db_path_refused_requires_future_operator_authorization",
                report["reasons"],
            )
            # Untouched: not opened as sqlite, bytes and mtime unchanged.
            self.assertEqual(target.read_bytes(), before_bytes)
            self.assertEqual(target.stat().st_mtime_ns, before_mtime)

    # 3. Harness uses only stage2c2-* synthetic IDs and writes no foreign rows.
    def test_only_stage2c2_ids_used(self) -> None:
        report = canary.run_stage2c2_synthetic_canary()
        self.assertTrue(report["synthetic_ids"])
        for synthetic_id in report["synthetic_ids"]:
            self.assertTrue(
                synthetic_id.startswith(canary.SYNTHETIC_PREFIX),
                msg=f"non-synthetic id: {synthetic_id}",
            )
        self.assertFalse(report["non_synthetic_records_created"])
        self.assertEqual(report["synthetic_prefix"], "stage2c2-")

    # 4. COMPLETE / RETRYABLE / BLOCKED / MANUAL_REVIEW_REQUIRED classifications.
    def test_recovery_classifications(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, store = self._store(Path(tmpdir))

            canary._seed_phase(store, task_id="stage2c2-t-complete", terminal_status="succeeded")
            self.assertIs(
                canary._plan(store, "stage2c2-t-complete").status,
                F2RecoveryStatus.COMPLETE,
            )

            canary._seed_phase(store, task_id="stage2c2-t-retryable", terminal_status=None)
            self.assertIs(
                canary._plan(store, "stage2c2-t-retryable").status,
                F2RecoveryStatus.RETRYABLE,
            )

            canary._seed_write_without_checkpoint(store, task_id="stage2c2-t-blocked")
            self.assertIs(
                canary._plan(store, "stage2c2-t-blocked").status,
                F2RecoveryStatus.BLOCKED,
            )

            canary._seed_phase(store, task_id="stage2c2-t-manual", terminal_status=None)
            canary._seed_effect(
                store, task_id="stage2c2-t-manual", status="verified_applied", linked=False
            )
            manual_plan = canary._plan(store, "stage2c2-t-manual")
            self.assertIs(manual_plan.status, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
            self.assertTrue(manual_plan.external_effect_blockers)
            self.assertEqual(
                manual_plan.external_effect_blockers[0].reason, "orphaned_external_effect"
            )

    # 5. verified_absent requires future execution and does not replay.
    def test_verified_absent_requires_future_execution_and_no_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, store = self._store(Path(tmpdir))
            canary._seed_phase(store, task_id="stage2c2-t-absent", terminal_status=None)
            effect = canary._seed_effect(
                store, task_id="stage2c2-t-absent", status="verified_absent", linked=True
            )
            plan = canary._plan(store, "stage2c2-t-absent")
            self.assertIn(
                effect.external_effect_id, plan.external_effects_requiring_future_execution
            )
            self.assertFalse(plan.will_replay_external_effects)

    # 6. verified_applied does not replay.
    def test_verified_applied_does_not_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, store = self._store(Path(tmpdir))
            canary._seed_phase(store, task_id="stage2c2-t-applied", terminal_status=None)
            effect = canary._seed_effect(
                store, task_id="stage2c2-t-applied", status="verified_applied", linked=True
            )
            plan = canary._plan(store, "stage2c2-t-applied")
            decision = plan.phase_decisions[0]
            self.assertIn(effect.external_effect_id, decision.verified_applied_effect_ids)
            self.assertNotIn(
                effect.external_effect_id, plan.external_effects_requiring_future_execution
            )
            self.assertFalse(plan.will_replay_external_effects)
            self.assertEqual(plan.external_effect_blockers, ())

    # 7. Duplicate external-effect idempotency returns the existing (first) row.
    def test_duplicate_idempotency_returns_existing_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _, store = self._store(Path(tmpdir))
            from claw_v2.f2_durability_store import compute_external_effect_idempotency_key

            key = compute_external_effect_idempotency_key(
                task_id="stage2c2-t-idem",
                run_id="stage2c2-t-idem",
                phase=canary._PHASE,
                effect_kind="synthetic_external_effect",
                target="synthetic://stage2c2/idem",
                content_hash="sha256:idem",
            )
            common = dict(
                idempotency_key=key,
                task_id="stage2c2-t-idem",
                run_id="stage2c2-t-idem",
                phase=canary._PHASE,
                effect_kind="synthetic_external_effect",
                target="synthetic://stage2c2/idem",
                content_hash="sha256:idem",
            )
            first = store.record_external_effect(
                external_effect_id="stage2c2-t-idem-first", request={"v": 1}, **common
            )
            second = store.record_external_effect(
                external_effect_id="stage2c2-t-idem-second", request={"v": 2}, **common
            )
            rows = store.list_external_effects(task_id="stage2c2-t-idem")
            self.assertEqual(len(rows), 1)
            self.assertEqual(second.external_effect_id, first.external_effect_id)
            self.assertEqual(second.external_effect_id, "stage2c2-t-idem-first")

    # 8. JSON output contains the required fields.
    def test_json_output_contains_required_fields(self) -> None:
        report = canary.run_stage2c2_synthetic_canary()
        for field_name in _REQUIRED_JSON_FIELDS:
            self.assertIn(field_name, report)
        for table in (
            "phase_checkpoints",
            "phase_checkpoint_writes",
            "external_effect_records",
            "phase_recovery_cursors",
        ):
            self.assertEqual(report["counts_before"][table], 0)
            self.assertIn(table, report["counts_after"])

    # 9. No real job-claim/scheduler/drain/external-effect paths are invoked.
    def test_no_real_work_paths_invoked(self) -> None:
        # Negative proof by construction: the harness module never imports the
        # job/scheduler/drain machinery, and never executes a real effect.
        source = Path(canary.__file__).read_text()
        for forbidden in (
            "from claw_v2.jobs import",
            "JobService",
            "CronScheduler",
            "drain_reconcilable_unverified",
            "reconcile_failed_unverified",
            "subprocess",
            "create_subprocess",
        ):
            self.assertNotIn(forbidden, source, msg=f"unexpected reference: {forbidden}")
        report = canary.run_stage2c2_synthetic_canary()
        self.assertFalse(report["real_external_effects_executed"])

    # Fault injection: a path-check FAIL must flip overall_status to FAIL
    # (proves the canary can actually fail, not only the refusal path).
    def test_overall_status_fails_closed_when_a_path_check_fails(self) -> None:
        from unittest import mock

        def _failing_phase_check(_store):
            return (
                canary._PathResult(status=canary.FAIL, reasons=["injected_failure"], details={}),
                ["stage2c2-injected"],
            )

        with mock.patch.object(
            canary, "_check_phase_checkpoint_path", side_effect=_failing_phase_check
        ):
            report = canary.run_stage2c2_synthetic_canary()
        self.assertEqual(report["overall_status"], canary.FAIL)
        self.assertEqual(report["phase_checkpoint_path"], canary.FAIL)
        self.assertIn("phase:injected_failure", report["reasons"])

    def test_cli_main_returns_zero_on_pass(self) -> None:
        self.assertEqual(canary.main(["--temp-db", "--json"]), 0)

    def test_cli_main_returns_one_on_refused_db_path(self) -> None:
        self.assertEqual(canary.main(["--db-path", "/nonexistent/primary.db", "--json"]), 1)


if __name__ == "__main__":
    unittest.main()
