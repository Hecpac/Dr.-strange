from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.f2_recovery import (
    F2_COORDINATOR_PHASES,
    F2RecoveryStatus,
    plan_f2_recovery,
)
from claw_v2.sqlite_runtime import RuntimeDb


class F2RecoveryPlannerTests(unittest.TestCase):
    def _runtime_db(self, tmpdir: str) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "claw.db")
        self.addCleanup(db.close)
        return db

    def _store(self, tmpdir: str) -> F2DurabilityStore:
        return F2DurabilityStore(self._runtime_db(tmpdir))

    def _phase_checkpoint(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = "task-1",
        run_id: str = "task-1",
        phase: str = "research",
        status: str = "succeeded",
    ):
        start_write = store.append_checkpoint_write(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_started",
            payload={"event": "phase_started", "phase": phase},
        )
        store.create_phase_checkpoint(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            phase_version=1,
            status="started",
            last_write_order=start_write.write_order,
            payload={"event": "phase_started", "phase": phase},
        )
        if status == "started":
            return store.list_phase_checkpoints(
                task_id=task_id,
                run_id=run_id,
                phase=phase,
                order="phase_version_desc",
                limit=1,
            )[0]
        finish_write = store.append_checkpoint_write(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_return" if status == "succeeded" else "phase_error",
            payload={"event": f"phase_{status}", "phase": phase},
        )
        return store.create_phase_checkpoint(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            phase_version=2,
            status=status,
            last_write_order=finish_write.write_order,
            payload={"event": f"phase_{status}", "phase": phase},
        )

    def _linked_effect(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = "task-1",
        run_id: str = "task-1",
        phase: str = "implementation",
        status: str = "intent_recorded",
        linked: bool = True,
    ):
        effect = store.record_external_effect(
            external_effect_id=f"effect-{status}",
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            effect_kind="github_pr",
            target="Hecpac/repo#draft",
            request={"title": "Draft PR"},
        )
        if status != "intent_recorded":
            effect = store.update_external_effect_status(
                effect.external_effect_id,
                status=status,
                verification={"status": status},
                result={"ok": status == "verified_applied"},
            )
        if linked:
            store.append_checkpoint_write(
                task_id=task_id,
                run_id=run_id,
                phase=phase,
                write_kind="external_effect_intent",
                write_key=f"external-effect:{effect.external_effect_id}",
                payload={"external_effect_id": effect.external_effect_id},
                external_effect_id=effect.external_effect_id,
            )
        return effect

    def _decision(self, plan, phase: str):
        return next(item for item in plan.phase_decisions if item.phase == phase)

    def test_no_f2_store_returns_disabled_noop_plan(self) -> None:
        plan = plan_f2_recovery(None, task_id="task-1")

        self.assertFalse(plan.enabled)
        self.assertEqual(plan.task_id, "task-1")
        self.assertEqual(plan.run_id, "task-1")
        self.assertIs(plan.status, F2RecoveryStatus.DISABLED)
        self.assertEqual(plan.phase_decisions, ())
        self.assertIsNone(plan.cursor_before)
        self.assertIsNone(plan.cursor_after)
        self.assertFalse(plan.will_replay_external_effects)

    def test_explicit_run_id_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, run_id="run-explicit", phase="research")

            plan = plan_f2_recovery(store, task_id="task-1", run_id="run-explicit")

            self.assertEqual(plan.run_id, "run-explicit")
            self.assertIs(self._decision(plan, "research").status, F2RecoveryStatus.COMPLETE)

    def test_absent_run_id_defaults_to_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, task_id="task-as-run", run_id="task-as-run")

            plan = plan_f2_recovery(store, task_id="task-as-run")

            self.assertEqual(plan.run_id, "task-as-run")
            self.assertIs(self._decision(plan, "research").status, F2RecoveryStatus.COMPLETE)

    def test_completed_phase_without_effect_blockers_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            checkpoint = self._phase_checkpoint(store, phase="research")

            plan = plan_f2_recovery(store, task_id="task-1")
            decision = self._decision(plan, "research")

            self.assertIs(decision.status, F2RecoveryStatus.COMPLETE)
            self.assertEqual(decision.latest_checkpoint_id, checkpoint.checkpoint_id)
            self.assertEqual(decision.last_write_order, checkpoint.last_write_order)

    def test_incomplete_phase_without_effect_blockers_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="research", status="started")

            plan = plan_f2_recovery(store, task_id="task-1")
            decision = self._decision(plan, "research")

            self.assertIs(decision.status, F2RecoveryStatus.RETRYABLE)
            self.assertEqual(decision.external_effect_blockers, ())
            self.assertFalse(plan.will_replay_external_effects)

    def test_intent_recorded_external_effect_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="implementation", status="started")
            effect = self._linked_effect(store, status="intent_recorded")

            plan = plan_f2_recovery(store, task_id="task-1")

            self.assertIs(plan.status, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
            blocker = self._decision(plan, "implementation").external_effect_blockers[0]
            self.assertEqual(blocker.external_effect_id, effect.external_effect_id)
            self.assertEqual(blocker.status, "intent_recorded")
            self.assertEqual(blocker.reason, "unsafe_external_effect_status")

    def test_apply_in_progress_external_effect_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="implementation", status="started")
            self._linked_effect(store, status="apply_in_progress")

            plan = plan_f2_recovery(store, task_id="task-1")

            self.assertIs(plan.status, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
            blocker = self._decision(plan, "implementation").external_effect_blockers[0]
            self.assertEqual(blocker.status, "apply_in_progress")

    def test_applied_but_unverified_external_effect_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="implementation", status="started")
            self._linked_effect(store, status="applied")

            plan = plan_f2_recovery(store, task_id="task-1")

            self.assertIs(plan.status, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
            blocker = self._decision(plan, "implementation").external_effect_blockers[0]
            self.assertEqual(blocker.status, "applied")

    def test_verified_applied_external_effect_is_treated_as_already_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="implementation", status="succeeded")
            effect = self._linked_effect(store, status="verified_applied")
            latest_write = store.list_checkpoint_writes(
                task_id="task-1",
                run_id="task-1",
                phase="implementation",
                order="write_order_desc",
                limit=1,
            )[0]
            store.create_phase_checkpoint(
                task_id="task-1",
                run_id="task-1",
                phase="implementation",
                phase_version=3,
                status="succeeded",
                last_write_order=latest_write.write_order,
                payload={"event": "phase_succeeded", "phase": "implementation"},
            )

            plan = plan_f2_recovery(store, task_id="task-1")
            decision = self._decision(plan, "implementation")

            self.assertIs(decision.status, F2RecoveryStatus.COMPLETE)
            self.assertEqual(decision.external_effect_blockers, ())
            self.assertEqual(decision.verified_applied_effect_ids, (effect.external_effect_id,))

    def test_verified_absent_external_effect_does_not_execute_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="implementation", status="started")
            effect = self._linked_effect(store, status="verified_absent")

            plan = plan_f2_recovery(store, task_id="task-1")
            decision = self._decision(plan, "implementation")

            self.assertIs(decision.status, F2RecoveryStatus.RETRYABLE)
            self.assertFalse(plan.will_replay_external_effects)
            self.assertEqual(
                plan.external_effects_requiring_future_execution,
                (effect.external_effect_id,),
            )
            self.assertEqual(decision.verified_absent_effect_ids, (effect.external_effect_id,))

    def test_orphaned_external_effect_without_checkpoint_write_requires_manual_review(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            self._phase_checkpoint(store, phase="implementation", status="started")
            effect = self._linked_effect(store, status="verified_applied", linked=False)

            plan = plan_f2_recovery(store, task_id="task-1")

            self.assertIs(plan.status, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
            blocker = self._decision(plan, "implementation").external_effect_blockers[0]
            self.assertEqual(blocker.external_effect_id, effect.external_effect_id)
            self.assertEqual(blocker.reason, "orphaned_external_effect")

    def test_recovery_cursor_is_read_and_advances_after_durable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            checkpoint = self._phase_checkpoint(store, phase="research", status="succeeded")
            before = store.upsert_recovery_cursor(
                recovery_cursor_id="cursor-1",
                task_id="task-1",
                run_id="task-1",
                phase="research",
                cursor_status="ready_to_resume_phase",
                last_checkpoint_id=checkpoint.checkpoint_id,
                last_write_order=checkpoint.last_write_order,
                resume_payload={"phase": "research"},
            )

            plan = plan_f2_recovery(store, task_id="task-1", persist_cursor=True)

            self.assertEqual(plan.cursor_before, before)
            self.assertIsNotNone(plan.cursor_after)
            self.assertEqual(plan.cursor_after.recovery_cursor_id, before.recovery_cursor_id)
            self.assertEqual(plan.cursor_after.phase, "synthesis")
            self.assertEqual(plan.cursor_after.cursor_status, "ready_to_start_phase")
            self.assertEqual(plan.cursor_after.last_checkpoint_id, checkpoint.checkpoint_id)

    def test_recovery_cursor_does_not_move_backward(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)
            checkpoint = self._phase_checkpoint(store, phase="research", status="succeeded")
            before = store.upsert_recovery_cursor(
                recovery_cursor_id="cursor-1",
                task_id="task-1",
                run_id="task-1",
                phase="implementation",
                cursor_status="ready_to_start_phase",
                last_checkpoint_id=checkpoint.checkpoint_id,
                last_write_order=0,
                resume_payload={"phase": "implementation"},
            )

            plan = plan_f2_recovery(store, task_id="task-1", persist_cursor=True)

            self.assertIs(plan.status, F2RecoveryStatus.BLOCKED)
            self.assertEqual(plan.cursor_after, before)
            self.assertIn("cursor_ahead_of_durable_evidence", plan.reasons)

    def test_planner_uses_fixed_coordinator_phase_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = self._store(tmpdir)

            plan = plan_f2_recovery(store, task_id="task-1")

            self.assertEqual(
                tuple(decision.phase for decision in plan.phase_decisions),
                F2_COORDINATOR_PHASES,
            )


if __name__ == "__main__":
    unittest.main()
