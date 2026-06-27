from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claw_v2.coordinator import CoordinatorResult
from claw_v2.f2_durability_store import (
    ExternalEffectRecord,
    F2DurabilityStore,
    compute_external_effect_idempotency_key,
)
from claw_v2.f2_recovery import F2RecoveryStatus, plan_f2_recovery
from claw_v2.memory import MemoryStore
from claw_v2.sqlite_runtime import RuntimeDb
from claw_v2.task_handler import TaskHandler


TASK_ID = "stage2c3-synthetic-effect-task"
RUN_ID = TASK_ID
PHASE = "implementation"
TARGET = "synthetic://external-effect/a3"
CONTENT_HASH = "sha256:a3-synthetic-content"


@dataclass(frozen=True, slots=True)
class _SyntheticExecutionResult:
    effect: ExternalEffectRecord
    executed: bool
    reused_existing: bool


class _FakeExternalEffect:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(request))
        return {"provider_effect_id": f"fake-effect-{len(self.calls)}"}


class _SyntheticExternalEffectExecutor:
    """Durable-intent-first fake executor used only for A3 synthetic coverage."""

    def __init__(
        self,
        store: F2DurabilityStore,
        fake_effect: _FakeExternalEffect,
    ) -> None:
        self.store = store
        self.fake_effect = fake_effect

    def execute_once(
        self,
        *,
        task_id: str = TASK_ID,
        run_id: str = RUN_ID,
        phase: str = PHASE,
        idempotency_key: str,
        external_effect_id: str,
        request: dict[str, Any] | None = None,
    ) -> _SyntheticExecutionResult:
        existing = self.store.get_external_effect_by_idempotency_key(idempotency_key)
        if existing is not None:
            return _SyntheticExecutionResult(
                effect=existing,
                executed=False,
                reused_existing=True,
            )

        effect = self.store.record_external_effect(
            external_effect_id=external_effect_id,
            idempotency_key=idempotency_key,
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            effect_kind="synthetic_external_effect",
            target=TARGET,
            content_hash=CONTENT_HASH,
            request=request or {"action": "synthetic_apply"},
            status="intent_recorded",
        )
        self.store.append_checkpoint_write(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="external_effect_intent",
            write_key=f"external-effect:{effect.external_effect_id}",
            payload={"external_effect_id": effect.external_effect_id},
            external_effect_id=effect.external_effect_id,
        )
        in_progress = self.store.update_external_effect_status(
            effect.external_effect_id,
            status="apply_in_progress",
            increment_attempt_count=True,
        )
        assert in_progress is not None
        fake_result = self.fake_effect(request or {"action": "synthetic_apply"})
        verified = self.store.update_external_effect_status(
            in_progress.external_effect_id,
            status="verified_applied",
            result=fake_result,
            verification={"status": "verified_applied", "source": "synthetic"},
            verifier_kind="synthetic",
        )
        assert verified is not None
        return _SyntheticExecutionResult(
            effect=verified,
            executed=True,
            reused_existing=False,
        )


class F2ExternalEffectSyntheticTests(unittest.TestCase):
    def _store(self, root: Path) -> tuple[RuntimeDb, F2DurabilityStore]:
        db = RuntimeDb(root / "claw.db")
        return db, F2DurabilityStore(db)

    def _phase_checkpoint(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = TASK_ID,
        run_id: str = RUN_ID,
        phase: str = PHASE,
        status: str,
    ) -> None:
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
            return
        finish_write = store.append_checkpoint_write(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_return" if status == "succeeded" else "phase_error",
            payload={"event": f"phase_{status}", "phase": phase},
        )
        store.create_phase_checkpoint(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            phase_version=2,
            status=status,
            last_write_order=finish_write.write_order,
            payload={"event": f"phase_{status}", "phase": phase},
        )

    def _idempotency_key(self) -> str:
        return compute_external_effect_idempotency_key(
            task_id=TASK_ID,
            run_id=RUN_ID,
            phase=PHASE,
            effect_kind="synthetic_external_effect",
            target=TARGET,
            content_hash=CONTENT_HASH,
        )

    def _linked_effect(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = TASK_ID,
        run_id: str = RUN_ID,
        phase: str = PHASE,
        status: str,
        linked: bool = True,
    ) -> ExternalEffectRecord:
        effect = store.record_external_effect(
            external_effect_id=f"effect-{status}-{linked}",
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            effect_kind="synthetic_external_effect",
            target=TARGET,
            content_hash=CONTENT_HASH,
            request={"action": "synthetic_apply"},
        )
        if status != "intent_recorded":
            updated = store.update_external_effect_status(
                effect.external_effect_id,
                status=status,
                result={"provider_effect_id": "existing"},
                verification={"status": status},
                verifier_kind="synthetic",
            )
            assert updated is not None
            effect = updated
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

    def test_same_idempotency_key_executes_fake_effect_once_and_reuses_record(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db, store = self._store(root)
            self.addCleanup(db.close)
            self._phase_checkpoint(store, status="started")
            key = self._idempotency_key()
            fake_effect = _FakeExternalEffect()
            executor = _SyntheticExternalEffectExecutor(store, fake_effect)

            first = executor.execute_once(
                idempotency_key=key,
                external_effect_id="effect-a3-idempotent-first",
                request={"action": "synthetic_apply", "value": 1},
            )
            second = executor.execute_once(
                idempotency_key=key,
                external_effect_id="effect-a3-idempotent-second",
                request={"action": "synthetic_apply", "value": 2},
            )
            finish_write = store.append_checkpoint_write(
                task_id=TASK_ID,
                run_id=RUN_ID,
                phase=PHASE,
                write_kind="phase_return",
                payload={"event": "phase_succeeded", "phase": PHASE},
            )
            store.create_phase_checkpoint(
                task_id=TASK_ID,
                run_id=RUN_ID,
                phase=PHASE,
                phase_version=2,
                status="succeeded",
                last_write_order=finish_write.write_order,
                payload={"event": "phase_succeeded", "phase": PHASE},
            )

            plan = plan_f2_recovery(store, task_id=TASK_ID, phases=(PHASE,))
            with db.cursor() as cur:
                count = cur.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM external_effect_records
                    WHERE idempotency_key = ?
                    """,
                    (key,),
                ).fetchone()["count"]

            self.assertTrue(first.executed)
            self.assertFalse(first.reused_existing)
            self.assertFalse(second.executed)
            self.assertTrue(second.reused_existing)
            self.assertEqual(len(fake_effect.calls), 1)
            self.assertEqual(count, 1)
            self.assertEqual(second.effect.external_effect_id, first.effect.external_effect_id)
            self.assertEqual(second.effect.result, first.effect.result)
            self.assertIs(plan.status, F2RecoveryStatus.COMPLETE)
            self.assertFalse(plan.will_replay_external_effects)

    def test_crash_before_ledger_write_is_undetectable_retryable_risk(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db, store = self._store(root)
            self.addCleanup(db.close)
            self._phase_checkpoint(store, status="started")
            fake_effect = _FakeExternalEffect()

            fake_result = fake_effect({"action": "synthetic_apply_before_ledger"})
            plan = plan_f2_recovery(store, task_id=TASK_ID, phases=(PHASE,))
            decision = plan.phase_decisions[0]

            self.assertEqual(fake_result, {"provider_effect_id": "fake-effect-1"})
            self.assertEqual(len(fake_effect.calls), 1)
            self.assertEqual(store.list_external_effects(task_id=TASK_ID), [])
            self.assertIs(plan.status, F2RecoveryStatus.RETRYABLE)
            self.assertEqual(plan.next_phase, PHASE)
            self.assertFalse(plan.will_replay_external_effects)
            self.assertEqual(plan.external_effect_blockers, ())
            self.assertEqual(plan.external_effects_requiring_future_execution, ())
            self.assertEqual(decision.reason, "phase_started_without_terminal_checkpoint")

    def test_effect_then_crash_before_checkpoint_does_not_reexecute_verified_applied(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db, store = self._store(root)
            self.addCleanup(db.close)
            self._phase_checkpoint(store, status="started")
            key = self._idempotency_key()
            fake_effect = _FakeExternalEffect()
            executor = _SyntheticExternalEffectExecutor(store, fake_effect)

            first = executor.execute_once(
                idempotency_key=key,
                external_effect_id="effect-a3-before-checkpoint",
            )
            plan = plan_f2_recovery(store, task_id=TASK_ID, phases=(PHASE,))
            second = executor.execute_once(
                idempotency_key=key,
                external_effect_id="effect-a3-after-recovery",
            )
            decision = plan.phase_decisions[0]

            self.assertTrue(first.executed)
            self.assertFalse(second.executed)
            self.assertEqual(len(fake_effect.calls), 1)
            self.assertIs(plan.status, F2RecoveryStatus.RETRYABLE)
            self.assertEqual(
                decision.verified_applied_effect_ids, (first.effect.external_effect_id,)
            )
            self.assertEqual(decision.external_effect_blockers, ())
            self.assertFalse(plan.will_replay_external_effects)

    def test_orphaned_verified_applied_effect_requires_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db, store = self._store(root)
            self.addCleanup(db.close)
            self._phase_checkpoint(store, status="started")
            effect = self._linked_effect(store, status="verified_applied", linked=False)

            plan = plan_f2_recovery(store, task_id=TASK_ID, phases=(PHASE,))
            blocker = plan.external_effect_blockers[0]

            self.assertIs(plan.status, F2RecoveryStatus.MANUAL_REVIEW_REQUIRED)
            self.assertFalse(plan.will_replay_external_effects)
            self.assertEqual(blocker.external_effect_id, effect.external_effect_id)
            self.assertEqual(blocker.reason, "orphaned_external_effect")

    def test_verified_absent_future_effect_blocks_taskhandler_auto_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db, store = self._store(root)
            self.addCleanup(db.close)
            self._phase_checkpoint(store, phase="research", status="succeeded")
            self._phase_checkpoint(store, phase="synthesis", status="succeeded")
            self._phase_checkpoint(store, status="started")
            effect = self._linked_effect(store, status="verified_absent")
            memory = MemoryStore(root / "session.db")
            recorded: dict[str, Any] = {}

            class _NoRunCoordinator:
                f2_durability_store = store

                def detect_resume_phase(self, task_id: str) -> str:
                    recorded["detect_resume_phase"] = task_id
                    return PHASE

                def run(self, *args: Any, **kwargs: Any) -> CoordinatorResult:
                    recorded["run_called"] = True
                    return CoordinatorResult(task_id=TASK_ID)

            handler = TaskHandler(
                coordinator=_NoRunCoordinator(),
                observe=None,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )

            response = handler._run_coordinated_task(
                "session-1",
                "objective",
                mode="coding",
                forced=False,
                task_id=TASK_ID,
                resumed=True,
            )
            state = handler._get_session_state("session-1")

            self.assertEqual(recorded["detect_resume_phase"], TASK_ID)
            self.assertNotIn("run_called", recorded)
            self.assertEqual(state["verification_status"], "blocked")
            self.assertIn(effect.external_effect_id, response)
            self.assertIn("f2_recovery_retry_requires_future_external_effect", response)


if __name__ == "__main__":
    unittest.main()
