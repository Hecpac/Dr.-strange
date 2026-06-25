from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.f2_recovery import F2RecoveryStatus, plan_f2_recovery
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import RuntimeDb
from claw_v2.task_handler import TaskHandler


class F2ResumeE2EReconstructionTests(unittest.TestCase):
    def _new_store(self, db_path: Path) -> tuple[RuntimeDb, F2DurabilityStore]:
        db = RuntimeDb(db_path)
        return db, F2DurabilityStore(db)

    def _reopen_store(self, db: RuntimeDb, db_path: Path) -> tuple[RuntimeDb, F2DurabilityStore]:
        db.close()
        return self._new_store(db_path)

    def _phase_checkpoint(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = "task-1",
        run_id: str = "task-1",
        phase: str,
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

    def _complete_through_synthesis(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = "task-1",
        run_id: str = "task-1",
    ) -> None:
        self._phase_checkpoint(store, task_id=task_id, run_id=run_id, phase="research")
        self._phase_checkpoint(store, task_id=task_id, run_id=run_id, phase="synthesis")

    def _complete_all_phases(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = "task-1",
        run_id: str = "task-1",
    ) -> None:
        for phase in ("research", "synthesis", "implementation", "verification"):
            self._phase_checkpoint(store, task_id=task_id, run_id=run_id, phase=phase)

    def _linked_effect(
        self,
        store: F2DurabilityStore,
        *,
        task_id: str = "task-1",
        run_id: str = "task-1",
        phase: str = "implementation",
        status: str,
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

    def _handler_with_reopened_store(
        self,
        root: Path,
        store: F2DurabilityStore,
        recorded: dict[str, Any],
        *,
        detected_phase: str = "implementation",
    ) -> TaskHandler:
        memory = MemoryStore(root / "memory.db")
        observe = ObserveStream(root / "observe.db")

        class _RecordingCoordinator:
            f2_durability_store = store

            def detect_resume_phase(self, task_id: str) -> str:
                recorded["detect_task_id"] = task_id
                return detected_phase

            def run(
                self,
                task_id: str,
                objective: str,
                research_tasks: list[Any],
                implementation_tasks: list[Any] | None = None,
                verification_tasks: list[Any] | None = None,
                lane_overrides: dict[str, dict[str, Any]] | None = None,
                **kwargs: Any,
            ) -> CoordinatorResult:
                recorded["run_called"] = True
                recorded["start_phase"] = kwargs.get("start_phase")
                return CoordinatorResult(
                    task_id=task_id,
                    phase_results={
                        "verification": [
                            WorkerResult(
                                task_name="verify_recovery",
                                content="Verification Status: passed",
                                duration_seconds=0.1,
                            )
                        ]
                    },
                    synthesis="recovered safely",
                )

        return TaskHandler(
            coordinator=_RecordingCoordinator(),
            observe=observe,
            get_session_state=memory.get_session_state,
            update_session_state=memory.update_session_state,
        )

    def test_reopened_runtime_db_preserves_retryable_recovery_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            db, store = self._new_store(db_path)
            self._complete_through_synthesis(store, task_id="task-1", run_id="run-1")
            self._phase_checkpoint(
                store,
                task_id="task-1",
                run_id="run-1",
                phase="implementation",
                status="started",
            )

            db, reopened = self._reopen_store(db, db_path)
            self.addCleanup(db.close)

            plan = plan_f2_recovery(reopened, task_id="task-1", run_id="run-1")

            self.assertEqual(plan.task_id, "task-1")
            self.assertEqual(plan.run_id, "run-1")
            self.assertIs(plan.status, F2RecoveryStatus.RETRYABLE)
            self.assertEqual(plan.next_phase, "implementation")
            self.assertIsNone(plan.cursor_after)
            self.assertEqual(plan.cursor_action, "not_requested")
            self.assertIsNone(reopened.get_recovery_cursor(task_id="task-1", run_id="run-1"))

    def test_reopened_runtime_db_manual_review_external_effect_blocks_taskhandler_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "runtime.db"
            db, store = self._new_store(db_path)
            self._complete_through_synthesis(store)
            self._phase_checkpoint(store, phase="implementation", status="started")
            effect = self._linked_effect(store, status="intent_recorded")

            db, reopened = self._reopen_store(db, db_path)
            self.addCleanup(db.close)
            recorded: dict[str, Any] = {}
            handler = self._handler_with_reopened_store(root, reopened, recorded)

            response = handler._run_coordinated_task(
                "session-1",
                "objective",
                mode="coding",
                forced=False,
                task_id="task-1",
                resumed=True,
            )

            state = handler._get_session_state("session-1")
            self.assertEqual(recorded["detect_task_id"], "task-1")
            self.assertNotIn("run_called", recorded)
            self.assertEqual(state["verification_status"], "blocked")
            self.assertEqual(state["last_checkpoint"]["verification_status"], "blocked")
            self.assertIn("f2_recovery_manual_review_required", response)
            self.assertIn(effect.external_effect_id, response)

    def test_reopened_runtime_db_retryable_plan_resumes_only_when_legacy_phase_matches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "runtime.db"
            db, store = self._new_store(db_path)
            self._complete_through_synthesis(store)
            self._phase_checkpoint(store, phase="implementation", status="started")

            db, reopened = self._reopen_store(db, db_path)
            self.addCleanup(db.close)
            matched: dict[str, Any] = {}
            matched_handler = self._handler_with_reopened_store(
                root / "matched",
                reopened,
                matched,
                detected_phase="implementation",
            )

            matched_handler._run_coordinated_task(
                "session-1",
                "objective",
                mode="coding",
                forced=False,
                task_id="task-1",
                resumed=True,
            )

            self.assertTrue(matched["run_called"])
            self.assertEqual(matched["start_phase"], "implementation")

            mismatched: dict[str, Any] = {}
            mismatched_handler = self._handler_with_reopened_store(
                root / "mismatched",
                reopened,
                mismatched,
                detected_phase="synthesis",
            )

            response = mismatched_handler._run_coordinated_task(
                "session-2",
                "objective",
                mode="coding",
                forced=False,
                task_id="task-1",
                resumed=True,
            )

            state = mismatched_handler._get_session_state("session-2")
            self.assertNotIn("run_called", mismatched)
            self.assertEqual(state["verification_status"], "blocked")
            self.assertIn("f2_recovery_retry_not_allowed_by_legacy_resume", response)

    def test_reopened_runtime_db_complete_plan_stays_noop_unknown_without_fabricated_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "runtime.db"
            db, store = self._new_store(db_path)
            self._complete_all_phases(store)

            db, reopened = self._reopen_store(db, db_path)
            self.addCleanup(db.close)
            recorded: dict[str, Any] = {}
            handler = self._handler_with_reopened_store(root, reopened, recorded)

            response = handler._run_coordinated_task(
                "session-1",
                "objective",
                mode="coding",
                forced=False,
                task_id="task-1",
                resumed=True,
            )

            state = handler._get_session_state("session-1")
            self.assertNotIn("run_called", recorded)
            self.assertEqual(state["verification_status"], "unknown")
            self.assertEqual(state["last_checkpoint"]["verification_status"], "unknown")
            self.assertFalse(state["last_checkpoint"]["coordinator_workers_rerun"])
            self.assertIn("f2_recovery_complete_noop", response)
            self.assertNotIn("Verification Status: passed", response)
            self.assertNotIn("Listo. Cerré la tarea", response)

    def test_explicit_run_id_survives_reopened_store_and_does_not_mix_with_task_id_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            db, store = self._new_store(db_path)
            self._phase_checkpoint(
                store,
                task_id="task-1",
                run_id="run-explicit",
                phase="research",
            )

            db, reopened = self._reopen_store(db, db_path)
            self.addCleanup(db.close)

            explicit = plan_f2_recovery(
                reopened,
                task_id="task-1",
                run_id="run-explicit",
            )
            fallback = plan_f2_recovery(reopened, task_id="task-1")

            self.assertEqual(explicit.run_id, "run-explicit")
            self.assertIs(explicit.phase_decisions[0].status, F2RecoveryStatus.COMPLETE)
            self.assertEqual(fallback.run_id, "task-1")
            self.assertIs(fallback.phase_decisions[0].status, F2RecoveryStatus.RETRYABLE)
            self.assertNotEqual(explicit.next_phase, fallback.next_phase)

    def test_reopened_runtime_db_verified_absent_never_replays_external_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "runtime.db"
            db, store = self._new_store(db_path)
            self._complete_through_synthesis(store)
            self._phase_checkpoint(store, phase="implementation", status="started")
            effect = self._linked_effect(store, status="verified_absent")

            db, reopened = self._reopen_store(db, db_path)
            self.addCleanup(db.close)

            plan = plan_f2_recovery(reopened, task_id="task-1")
            self.assertEqual(
                plan.external_effects_requiring_future_execution,
                (effect.external_effect_id,),
            )
            self.assertFalse(plan.will_replay_external_effects)

            recorded: dict[str, Any] = {}
            handler = self._handler_with_reopened_store(root, reopened, recorded)
            response = handler._run_coordinated_task(
                "session-1",
                "objective",
                mode="coding",
                forced=False,
                task_id="task-1",
                resumed=True,
            )

            state = handler._get_session_state("session-1")
            self.assertNotIn("run_called", recorded)
            self.assertEqual(state["verification_status"], "blocked")
            self.assertIn("f2_recovery_retry_requires_future_external_effect", response)


if __name__ == "__main__":
    unittest.main()
