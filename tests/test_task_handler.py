from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.cli_maintenance import CliMaintenanceResult
from claw_v2.bot_helpers import (
    _evaluate_autonomy_policy,
    _infer_session_mode,
    _should_use_browser_executor,
)
from claw_v2.f2_recovery import (
    F2ExternalEffectBlocker,
    F2RecoveryPlan,
    F2RecoveryStatus,
)
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import RuntimeDatabaseError, RuntimeDb
from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger


class _BlockingCoordinator:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(
        self,
        task_id,
        objective,
        research_tasks,
        implementation_tasks=None,
        verification_tasks=None,
        lane_overrides=None,
        **kwargs,
    ):
        self.started.set()
        self.release.wait(timeout=2)
        return CoordinatorResult(
            task_id=task_id,
            phase_results={
                "verification": [
                    WorkerResult(
                        task_name="verify_change",
                        content="Verification Status: passed",
                        duration_seconds=0.1,
                    )
                ]
            },
            synthesis="done",
        )


class TaskHandlerTests(unittest.TestCase):
    def test_passed_verification_is_rejected_when_result_says_not_verified(self) -> None:
        self.assertTrue(
            TaskHandler._response_contradicts_passed_verification(
                (
                    "Estado actual: **no verificado**. La evidencia disponible "
                    "no incluye PID, launchd, logs, DB ni evento agent_startup_context."
                ),
                {"summary": "Estado actual: no verificado"},
            )
        )
        self.assertTrue(
            TaskHandler._response_contradicts_passed_verification(
                "LIMITACIÓN CRÍTICA - No puedo ejecutar esta tarea.",
                {"summary": "Soy un agente de navegador y esto está fuera de mi scope."},
            )
        )
        self.assertTrue(
            TaskHandler._response_contradicts_passed_verification(
                "No puedo completar esta tarea tal como está especificada.",
                {"summary": "No tengo capacidad para ejecutar pkill."},
            )
        )

        self.assertFalse(
            TaskHandler._response_contradicts_passed_verification(
                "Verificado: passed. El evento agent_startup_context existe.",
                {"summary": "Verificado con evidencia"},
            )
        )

    def test_record_blocked_task_persists_contract_and_blocker_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            handler = TaskHandler(
                observe=observe,
                task_ledger=ledger,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=root,
                telemetry_root=root / "telemetry",
            )

            task_id = handler.record_blocked_task(
                "s1",
                "Regenera el lock del PR QTS",
                source_text="Hazlo",
                mode="coding",
                task_kind="qts_lock_regeneration",
                risk_tier="tier_2",
                plan=["preflight", "regenerate lock"],
                verification_requirement="poetry.lock regenerated or blocker evidence",
                blockers=["command_not_found:poetry"],
                preflight={"allowed": False, "blockers": ["command_not_found:poetry"]},
            )

            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.verification_status, "blocked")
            self.assertEqual(record.metadata["goal"], "Regenera el lock del PR QTS")
            self.assertEqual(record.metadata["source_message"], "Hazlo")
            self.assertEqual(record.metadata["risk_tier"], "tier_2")
            self.assertEqual(record.metadata["current_step"], "capability_preflight")
            self.assertEqual(record.metadata["blockers"], ["command_not_found:poetry"])
            self.assertIn("preflight", record.artifacts)
            state = memory.get_session_state("s1")
            self.assertEqual(state["verification_status"], "blocked")
            self.assertEqual(state["active_object"]["active_task"]["status"], "blocked")
            events = [event["event_type"] for event in observe.recent_events(limit=20)]
            self.assertIn("task_blocked_with_evidence", events)

    def test_precheck_worktree_does_not_autostash_memory_only_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
            subprocess.run(["git", "-C", str(workspace), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(workspace), "config", "user.name", "t"], check=True)
            (workspace / "MEMORY.md").write_text("# MEMORY.md\n\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(workspace), "add", "MEMORY.md"], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "init"], check=True)
            (workspace / "MEMORY.md").write_text(
                "# MEMORY.md\n\n- durable note\n", encoding="utf-8"
            )
            (workspace / "memory").mkdir()
            (workspace / "memory" / "2026-06-04.md").write_text("# 2026-06-04\n", encoding="utf-8")
            observe = ObserveStream(root / "observe.db")
            memory = MemoryStore(root / "claw.db")
            handler = TaskHandler(
                observe=observe,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=workspace,
            )

            handler._precheck_worktree(task_id="task-1", mode="coding")

            stash_list = subprocess.run(
                ["git", "-C", str(workspace), "stash", "list"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertNotIn("claw:autostash:task-1", stash_list.stdout)
            self.assertIn("durable note", (workspace / "MEMORY.md").read_text(encoding="utf-8"))
            self.assertTrue((workspace / "memory" / "2026-06-04.md").exists())
            events = [event["event_type"] for event in observe.recent_events(limit=10)]
            self.assertIn("worktree_autostash_skipped_protected_memory", events)

    def test_precheck_worktree_stashes_code_but_preserves_memory_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workspace, check=True)
            subprocess.run(["git", "-C", str(workspace), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(workspace), "config", "user.name", "t"], check=True)
            (workspace / "README.md").write_text("clean\n", encoding="utf-8")
            (workspace / "MEMORY.md").write_text("# MEMORY.md\n\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(workspace), "add", "README.md", "MEMORY.md"], check=True
            )
            subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "init"], check=True)
            (workspace / "README.md").write_text("dirty code\n", encoding="utf-8")
            (workspace / "MEMORY.md").write_text(
                "# MEMORY.md\n\n- durable note\n", encoding="utf-8"
            )
            (workspace / "memory").mkdir()
            (workspace / "memory" / "2026-06-04.md").write_text("# 2026-06-04\n", encoding="utf-8")
            observe = ObserveStream(root / "observe.db")
            memory = MemoryStore(root / "claw.db")
            handler = TaskHandler(
                observe=observe,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=workspace,
            )

            handler._precheck_worktree(task_id="task-2", mode="coding")

            stash_list = subprocess.run(
                ["git", "-C", str(workspace), "stash", "list"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn("claw:autostash:task-2", stash_list.stdout)
            self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "clean\n")
            self.assertIn("durable note", (workspace / "MEMORY.md").read_text(encoding="utf-8"))
            self.assertTrue((workspace / "memory" / "2026-06-04.md").exists())
            status = subprocess.run(
                ["git", "-C", str(workspace), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn(" M MEMORY.md", status.stdout)
            self.assertIn("?? memory/", status.stdout)

    def test_start_autonomous_task_backpressures_when_worker_limit_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            coordinator = _BlockingCoordinator()
            handler = TaskHandler(
                coordinator=coordinator,
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=root,
                max_autonomous_workers=1,
            )

            first = handler.start_autonomous_task("s1", "implementa el fix uno", mode="coding")
            first_task_id = first.split("`", 2)[1]
            self.assertIn("Tarea autónoma iniciada", first)
            self.assertTrue(coordinator.started.wait(timeout=1))

            second = handler.start_autonomous_task("s1", "implementa el fix dos", mode="coding")

            self.assertIn("Tarea autónoma en cola", second)
            state = memory.get_session_state("s1")
            second_task_id = state["active_object"]["active_task"]["task_id"]
            self.assertEqual(state["active_object"]["active_task"]["status"], "queued")
            record = ledger.get(second_task_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "running")
            self.assertEqual(record.verification_status, "queued")
            self.assertEqual(jobs.get(record.metadata["generic_job_id"]).status, "queued")
            events = [event["event_type"] for event in observe.recent_events(limit=20)]
            self.assertIn("autonomous_task_backpressure", events)

            coordinator.release.set()
            self.assertTrue(handler.wait_for_task(first_task_id, timeout=2))

    def test_pending_verification_stalls_to_failed_after_max_deferrals(self) -> None:
        # F1.1 (2026-06-11): a task whose verification never resolves must
        # reach terminal "failed" (verification_stalled) in ≤ N deferrals
        # instead of re-running forever.
        from claw_v2.task_handler import _MAX_VERIFICATION_DEFERRALS

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)

            class _PendingCoordinator:
                def run(
                    self,
                    task_id,
                    objective,
                    research_tasks,
                    implementation_tasks=None,
                    verification_tasks=None,
                    lane_overrides=None,
                    **kwargs,
                ):
                    return CoordinatorResult(
                        task_id=task_id,
                        phase_results={
                            "verification": [
                                WorkerResult(
                                    task_name="verify_change",
                                    content="Verification Status: pending",
                                    duration_seconds=0.1,
                                )
                            ]
                        },
                        synthesis="todavía falta",
                    )

            handler = TaskHandler(
                coordinator=_PendingCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=root,
            )

            ack = handler.start_autonomous_task("s1", "implementa el fix pendiente", mode="coding")
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=5))

            state = memory.get_session_state("s1")
            active_task = state["active_object"]["active_task"]
            self.assertEqual(active_task["status"], "pending")
            self.assertEqual(active_task["verification_deferrals"], 1)

            # Re-run the same task as the daemon job runner would.
            for _ in range(_MAX_VERIFICATION_DEFERRALS):
                handler._run_autonomous_task("s1", task_id, "implementa el fix pendiente", "coding")

            state = memory.get_session_state("s1")
            self.assertEqual(state["verification_status"], "failed")
            self.assertEqual(state["active_object"]["active_task"]["status"], "failed")
            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "failed")
            events = [event["event_type"] for event in observe.recent_events(limit=300)]
            self.assertIn("autonomous_task_verification_stalled", events)
            self.assertIn("autonomous_task_failed", events)

    def test_autonomous_task_lifts_contract_tool_artifact_before_promote_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)

            class _LyingContractToolCoordinator:
                def run(
                    self,
                    task_id,
                    objective,
                    research_tasks,
                    implementation_tasks=None,
                    verification_tasks=None,
                    lane_overrides=None,
                    **kwargs,
                ):
                    from claw_v2.tools import ToolDefinition, ToolRegistry
                    from claw_v2.verification.local_tool_contracts import (
                        LOCAL_TOOL_SUCCESS_CONDITIONS,
                    )

                    def _claims_write_without_writing(args):
                        return {
                            "ok": True,
                            "path": args["path"],
                            "bytes_written": len(args["content"]),
                        }

                    registry = ToolRegistry(workspace_root=root)
                    registry.register(
                        ToolDefinition(
                            name="Write",
                            description="fake contracted write",
                            allowed_agent_classes=("operator",),
                            handler=_claims_write_without_writing,
                            mutates_state=True,
                            tier=2,
                            success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Write"],
                        )
                    )
                    registry.register(
                        ToolDefinition(
                            name="Bash",
                            description="fake contracted bash",
                            allowed_agent_classes=("operator",),
                            handler=lambda args: {
                                "ok": True,
                                "exit_code": int(args.get("_fake_exit_code", 0)),
                                "stdout": "tests passed",
                            },
                            mutates_state=True,
                            tier=2,
                            success_condition=LOCAL_TOOL_SUCCESS_CONDITIONS["Bash"],
                        )
                    )
                    registry.execute(
                        "Write",
                        {"path": str(root / "claimed.txt"), "content": "not actually written"},
                        agent_class="operator",
                    )
                    registry.execute(
                        "Bash",
                        {"command": "pytest -q", "_fake_exit_code": 0},
                        agent_class="operator",
                    )
                    return CoordinatorResult(
                        task_id=task_id,
                        phase_results={
                            "implementation": [
                                WorkerResult(
                                    task_name="write_file",
                                    content="Write tool reported bytes_written for claimed.txt",
                                    duration_seconds=0.1,
                                )
                            ],
                            "verification": [
                                WorkerResult(
                                    task_name="verify_change",
                                    content="Verification Status: passed",
                                    duration_seconds=0.1,
                                )
                            ],
                        },
                        synthesis="claimed write complete",
                    )

            handler = TaskHandler(
                coordinator=_LyingContractToolCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=root,
            )

            ack = handler.start_autonomous_task(
                "s1", "write the contracted artifact", mode="coding"
            )
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=2))

            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.verification_status, "failed")
            state = memory.get_session_state("s1")
            self.assertEqual(state["verification_status"], "failed")
            checkpoint = state["last_checkpoint"]
            self.assertEqual(checkpoint["verification_status"], "failed")
            self.assertEqual(checkpoint["promote_gate_reason"], "multi_artifact_failed")
            self.assertIn("success_condition_artifact", checkpoint)
            self.assertEqual(len(checkpoint["success_condition_artifacts"]), 2)
            self.assertEqual(len(checkpoint["promote_gate_envelopes"]), 2)
            self.assertEqual(state["active_object"]["active_task"]["status"], "failed")
            events = [event["event_type"] for event in observe.recent_events(limit=80)]
            self.assertIn("promote_gate_degraded", events)
            self.assertIn("autonomous_task_failed", events)

    def test_start_autonomous_task_ops_mode_dispatches_implementation_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            recorded: dict[str, object] = {}

            class _RecordingCoordinator:
                def run(
                    self,
                    task_id,
                    objective,
                    research_tasks,
                    implementation_tasks=None,
                    verification_tasks=None,
                    lane_overrides=None,
                    **kwargs,
                ):
                    recorded["implementation_tasks"] = implementation_tasks
                    recorded["research_tasks"] = research_tasks
                    return CoordinatorResult(
                        task_id=task_id,
                        phase_results={
                            "verification": [
                                WorkerResult(
                                    task_name="verify_operation",
                                    content="Verification Status: passed",
                                    duration_seconds=0.1,
                                )
                            ]
                        },
                        synthesis="done",
                    )

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=root,
            )

            ack = handler.start_autonomous_task(
                "tg-1",
                "Publica el hero del grid en la cuenta del estudio",
                mode="ops",
                source_text="Publica el hero del grid en la cuenta del estudio",
                delegation_metadata={"origin": "brain_delegate_tool", "reason": "long job"},
            )
            self.assertIn("Tarea autónoma iniciada", ack)
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=2))

            implementation = recorded["implementation_tasks"]
            self.assertIsNotNone(implementation)
            self.assertEqual(len(implementation), 1)
            self.assertEqual(implementation[0].lane, "worker")
            self.assertEqual(implementation[0].name, "execute_operation")

            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.mode, "ops")

            state = memory.get_session_state("tg-1")
            active_task = state["active_object"]["active_task"]
            self.assertEqual(active_task["delegation_metadata"]["origin"], "brain_delegate_tool")
            events = [event["event_type"] for event in observe.recent_events(limit=50)]
            self.assertIn("autonomous_task_started", events)


class ResumeWiringTests(unittest.TestCase):
    """F3.1 — a resumed coordinated task passes the detected start_phase."""

    def _handler_with_recording_coordinator(
        self,
        root: Path,
        recorded: dict,
        *,
        f2_store: object | None = None,
        detected_phase: str = "implementation",
    ):
        memory = MemoryStore(root / "claw.db")
        observe = ObserveStream(root / "observe.db")

        class _RecordingCoordinator:
            f2_durability_store = f2_store

            def detect_resume_phase(self, task_id):
                recorded["detect_task_id"] = task_id
                return detected_phase

            def run(
                self,
                task_id,
                objective,
                research_tasks,
                implementation_tasks=None,
                verification_tasks=None,
                lane_overrides=None,
                **kwargs,
            ):
                recorded["run_called"] = True
                recorded["start_phase"] = kwargs.get("start_phase")
                recorded["should_abort"] = kwargs.get("should_abort")
                return CoordinatorResult(
                    task_id=task_id,
                    phase_results={
                        "verification": [
                            WorkerResult(
                                task_name="verify_change",
                                content="Verification Status: passed",
                                duration_seconds=0.1,
                            )
                        ]
                    },
                    synthesis="done",
                )

        handler = TaskHandler(
            coordinator=_RecordingCoordinator(),
            observe=observe,
            get_session_state=memory.get_session_state,
            update_session_state=memory.update_session_state,
        )
        return handler

    def _retryable_plan(
        self,
        *,
        task_id: str = "t-99",
        run_id: str = "t-99",
        next_phase: str | None = "implementation",
        external_effect_blockers: tuple[F2ExternalEffectBlocker, ...] = (),
        external_effects_requiring_future_execution: tuple[str, ...] = (),
        will_replay_external_effects: bool = False,
    ) -> F2RecoveryPlan:
        return F2RecoveryPlan(
            task_id=task_id,
            run_id=run_id,
            enabled=True,
            status=F2RecoveryStatus.RETRYABLE,
            phase_decisions=(),
            next_phase=next_phase,
            cursor_before=None,
            cursor_after=None,
            cursor_action="not_requested",
            external_effect_blockers=external_effect_blockers,
            external_effects_requiring_future_execution=(
                external_effects_requiring_future_execution
            ),
            will_replay_external_effects=will_replay_external_effects,
        )

    def _terminal_plan(
        self,
        *,
        status: F2RecoveryStatus,
        task_id: str = "t-99",
        run_id: str = "t-99",
        blockers: tuple[F2ExternalEffectBlocker, ...] = (),
    ) -> F2RecoveryPlan:
        return F2RecoveryPlan(
            task_id=task_id,
            run_id=run_id,
            enabled=True,
            status=status,
            phase_decisions=(),
            next_phase=None,
            cursor_before=None,
            cursor_after=None,
            cursor_action="not_requested",
            reasons=(status.value,),
            external_effect_blockers=blockers,
            will_replay_external_effects=False,
        )

    def test_startup_recovery_is_seeded_from_running_agent_tasks_not_phase_checkpoints(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_db = RuntimeDb(root / "claw.db")
            self.addCleanup(runtime_db.close)
            f2_store = F2DurabilityStore(runtime_db)
            ledger = TaskLedger(root / "claw.db", runtime_db=runtime_db)
            ghost_task_id = "stage2c1-ghost-checkpoint-only"
            checkpoint = f2_store.create_phase_checkpoint(
                checkpoint_id="checkpoint-stage2c1-ghost-checkpoint-only",
                task_id=ghost_task_id,
                run_id=ghost_task_id,
                phase="implementation",
                phase_version=1,
                status="started",
                last_write_order=0,
                payload={
                    "synthetic": True,
                    "source": "stage2c1",
                    "task_id": ghost_task_id,
                },
            )
            recorded: dict[str, object] = {}

            class _ForbiddenStartupF2Store:
                def _fail(self, operation: str):
                    recorded["forbidden_f2_operation"] = operation
                    raise AssertionError(
                        f"startup recovery must not enumerate F2 evidence: {operation}"
                    )

                def get_recovery_cursor(self, *args, **kwargs):
                    return self._fail("get_recovery_cursor")

                def list_phase_checkpoints(self, *args, **kwargs):
                    return self._fail("list_phase_checkpoints")

                def list_checkpoint_writes(self, *args, **kwargs):
                    return self._fail("list_checkpoint_writes")

                def list_external_effects(self, *args, **kwargs):
                    return self._fail("list_external_effects")

                def upsert_recovery_cursor(self, *args, **kwargs):
                    return self._fail("upsert_recovery_cursor")

                def record_external_effect(self, *args, **kwargs):
                    return self._fail("record_external_effect")

                def update_external_effect_status(self, *args, **kwargs):
                    return self._fail("update_external_effect_status")

            class _NoRunCoordinator:
                f2_durability_store = _ForbiddenStartupF2Store()

                def detect_resume_phase(self, task_id):
                    recorded["detect_resume_phase"] = task_id
                    raise AssertionError("orphan checkpoint must not seed resume")

                def run(self, *args, **kwargs):
                    recorded["run_called"] = True
                    raise AssertionError("orphan checkpoint must not rerun coordinator")

            handler = TaskHandler(
                coordinator=_NoRunCoordinator(),
                observe=None,
                task_ledger=ledger,
                get_session_state=lambda _session_id: {},
                update_session_state=lambda **_kwargs: None,
            )

            with (
                patch(
                    "claw_v2.task_handler.plan_f2_recovery",
                    side_effect=AssertionError(
                        "orphan checkpoint must not invoke F2 recovery planning"
                    ),
                ) as planner,
                patch.object(ledger, "list", wraps=ledger.list) as list_tasks,
            ):
                resumed = handler.resume_interrupted_autonomous_tasks()

            self.assertEqual(resumed, 0)
            list_tasks.assert_called_once_with(statuses=("running",), limit=20)
            planner.assert_not_called()
            self.assertEqual(recorded, {})
            self.assertIsNone(ledger.get(ghost_task_id))
            with runtime_db.cursor() as cur:
                task_count = cur.execute("SELECT COUNT(*) AS count FROM agent_tasks").fetchone()
            self.assertEqual(task_count["count"], 0)
            self.assertEqual(f2_store.get_phase_checkpoint(checkpoint.checkpoint_id), checkpoint)
            self.assertEqual(
                [
                    item.checkpoint_id
                    for item in f2_store.list_phase_checkpoints(task_id=ghost_task_id)
                ],
                [checkpoint.checkpoint_id],
            )
            self.assertEqual(f2_store.list_external_effects(task_id=ghost_task_id), [])

    def test_resumed_run_passes_detected_start_phase_and_abort_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(Path(tmpdir), recorded)
            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                side_effect=AssertionError("planner must not run without an F2 store"),
            ):
                handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )
            self.assertEqual(recorded["detect_task_id"], "t-99")
            self.assertEqual(recorded["start_phase"], "implementation")
            self.assertTrue(callable(recorded["should_abort"]))
            self.assertFalse(recorded["should_abort"]())
            with handler._task_lock:
                handler._cancelled_tasks.add("t-99")
            self.assertTrue(recorded["should_abort"]())

    def test_resumed_run_ignores_unconfigured_mock_f2_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryStore(Path(tmpdir) / "claw.db")
            coordinator = MagicMock()
            coordinator.f2_durability_store = None
            coordinator.detect_resume_phase.return_value = "verification"
            coordinator.run.return_value = CoordinatorResult(
                task_id="t-99",
                phase_results={
                    "verification": [
                        WorkerResult(
                            task_name="verify_change",
                            content="Verification Status: passed",
                            duration_seconds=0.1,
                        )
                    ]
                },
                synthesis="done",
            )
            handler = TaskHandler(
                coordinator=coordinator,
                observe=None,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                side_effect=AssertionError(
                    "an unconfigured (None) F2 store must not invoke the planner"
                ),
            ) as planner:
                handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            planner.assert_not_called()
            coordinator.detect_resume_phase.assert_called_once_with("t-99")
            self.assertEqual(coordinator.run.call_args.kwargs["start_phase"], "verification")

    def test_resumed_run_with_configured_f2_store_fails_closed_on_planner_error(self) -> None:
        """A coordinator with a *configured* F2 store (even a test double) must drive
        the F2 recovery planner on resume; a planner exception fails closed — the
        blocked no-run result is used and coordinator.run is NOT re-invoked. Guards
        the a89096e fail-closed path, which production must reach for any configured
        store rather than special-casing test doubles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryStore(Path(tmpdir) / "claw.db")
            coordinator = MagicMock()
            coordinator.f2_durability_store = MagicMock()
            coordinator.detect_resume_phase.return_value = "verification"
            coordinator.run.return_value = CoordinatorResult(
                task_id="t-fc",
                phase_results={
                    "verification": [
                        WorkerResult(
                            task_name="verify_change",
                            content="Verification Status: passed",
                            duration_seconds=0.1,
                        )
                    ]
                },
                synthesis="done",
            )
            handler = TaskHandler(
                coordinator=coordinator,
                observe=None,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                side_effect=RuntimeError("planner boom"),
            ) as planner:
                handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-fc",
                    resumed=True,
                )

            planner.assert_called_once()
            coordinator.run.assert_not_called()

    def test_fresh_run_passes_no_start_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(Path(tmpdir), recorded)
            handler._run_coordinated_task(
                "tg-1", "objetivo", mode="coding", forced=False, task_id="t-100"
            )
            self.assertIsNone(recorded["start_phase"])
            self.assertNotIn("detect_task_id", recorded)

    def test_f2_retryable_safe_plan_uses_next_phase_with_explicit_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="implementation",
            )
            plan = self._retryable_plan(run_id="run-42", next_phase="implementation")

            with patch("claw_v2.task_handler.plan_f2_recovery", return_value=plan) as planner:
                handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    run_id="run-42",
                    resumed=True,
                )

            planner.assert_called_once_with(
                f2_store,
                task_id="t-99",
                run_id="run-42",
                persist_cursor=False,
            )
            self.assertEqual(recorded["detect_task_id"], "t-99")
            self.assertTrue(recorded["run_called"])
            self.assertEqual(recorded["start_phase"], "implementation")

    def test_f2_retryable_plan_falls_back_to_task_id_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="implementation",
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                return_value=self._retryable_plan(next_phase="implementation"),
            ) as planner:
                handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            planner.assert_called_once_with(
                f2_store,
                task_id="t-99",
                run_id="t-99",
                persist_cursor=False,
            )

    def test_f2_recovery_planner_exception_fails_closed_without_coordinator_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="implementation",
            )
            secret_detail = "token=sk-secret api_key=secret-key cookie=session-secret"

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                side_effect=RuntimeError(secret_detail),
            ) as planner:
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            planner.assert_called_once_with(
                f2_store,
                task_id="t-99",
                run_id="t-99",
                persist_cursor=False,
            )
            self.assertEqual(recorded["detect_task_id"], "t-99")
            self.assertNotIn("run_called", recorded)
            state = handler._get_session_state("tg-1")
            checkpoint = state["last_checkpoint"]
            self.assertEqual(state["verification_status"], "blocked")
            self.assertEqual(checkpoint["verification_status"], "blocked")
            self.assertEqual(
                checkpoint["f2_recovery_reason"],
                "f2_recovery_planner_exception:RuntimeError",
            )
            self.assertEqual(checkpoint["f2_recovery_exception_type"], "RuntimeError")
            self.assertFalse(checkpoint["coordinator_workers_rerun"])
            self.assertIn("f2_recovery_planner_exception:RuntimeError", response)
            checkpoint_text = repr(checkpoint)
            self.assertNotIn(secret_detail, response)
            self.assertNotIn(secret_detail, checkpoint_text)
            self.assertNotIn("sk-secret", response)
            self.assertNotIn("sk-secret", checkpoint_text)

    def test_f2_retryable_future_effect_execution_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="implementation",
            )
            plan = self._retryable_plan(
                next_phase="implementation",
                external_effects_requiring_future_execution=("effect-1",),
            )

            with patch("claw_v2.task_handler.plan_f2_recovery", return_value=plan):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            state = handler._get_session_state("tg-1")
            self.assertEqual(state["verification_status"], "blocked")
            self.assertIn("f2_recovery_retry_requires_future_external_effect", response)

    def test_f2_retryable_replay_flag_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="implementation",
            )
            plan = self._retryable_plan(
                next_phase="implementation",
                will_replay_external_effects=True,
            )

            with patch("claw_v2.task_handler.plan_f2_recovery", return_value=plan):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            self.assertIn("f2_recovery_retry_would_replay_external_effects", response)

    def test_f2_retryable_external_effect_blocker_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="implementation",
            )
            blocker = F2ExternalEffectBlocker(
                external_effect_id="effect-1",
                phase="implementation",
                status="intent_recorded",
                reason="unsafe_external_effect_status",
            )
            plan = self._retryable_plan(
                next_phase="implementation",
                external_effect_blockers=(blocker,),
            )

            with patch("claw_v2.task_handler.plan_f2_recovery", return_value=plan):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            self.assertIn("f2_recovery_retry_has_external_effect_blockers", response)

    def test_f2_retryable_legacy_policy_mismatch_blocks_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
                detected_phase="synthesis",
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                return_value=self._retryable_plan(next_phase="implementation"),
            ):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            self.assertIn("f2_recovery_retry_not_allowed_by_legacy_resume", response)

    def test_f2_blocked_plan_refuses_coordinator_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                return_value=self._terminal_plan(status=F2RecoveryStatus.BLOCKED),
            ):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            self.assertIn("f2_recovery_blocked", response)
            self.assertEqual(handler._get_session_state("tg-1")["verification_status"], "blocked")

    def test_f2_manual_review_plan_refuses_coordinator_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
            )
            blocker = F2ExternalEffectBlocker(
                external_effect_id="effect-1",
                phase="implementation",
                status="applied",
                reason="unsafe_external_effect_status",
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                return_value=self._terminal_plan(
                    status=F2RecoveryStatus.MANUAL_REVIEW_REQUIRED,
                    blockers=(blocker,),
                ),
            ):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            self.assertIn("f2_recovery_manual_review_required", response)
            self.assertIn("effect-1", response)

    def test_f2_complete_plan_does_not_rerun_or_fabricate_passed_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f2_store = object()
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(
                Path(tmpdir),
                recorded,
                f2_store=f2_store,
            )

            with patch(
                "claw_v2.task_handler.plan_f2_recovery",
                return_value=self._terminal_plan(status=F2RecoveryStatus.COMPLETE),
            ):
                response = handler._run_coordinated_task(
                    "tg-1",
                    "objetivo",
                    mode="coding",
                    forced=False,
                    task_id="t-99",
                    resumed=True,
                )

            self.assertNotIn("run_called", recorded)
            state = handler._get_session_state("tg-1")
            self.assertEqual(state["verification_status"], "unknown")
            self.assertNotIn("Listo. Cerré la tarea", response)
            self.assertIn("f2_recovery_complete_noop", response)


class BrowserExecutorRoutingTests(unittest.TestCase):
    """Option (b), 2026-06-13: CDP/browser objectives route to the in-process
    browser executor instead of the network-denied Codex coordinator."""

    def test_x_sweep_objective_infers_browse_mode(self) -> None:
        self.assertEqual(_infer_session_mode("Haz un repaso por X"), "browse")
        self.assertEqual(_infer_session_mode("lee X por CDP"), "browse")
        self.assertEqual(_infer_session_mode("Abre Instagram"), "browse")
        self.assertEqual(_infer_session_mode("revisa el perfil de Instagram"), "browse")
        self.assertEqual(_infer_session_mode("Revisa el repo de browser use"), "coding")
        self.assertTrue(_should_use_browser_executor("ops", "Haz un repaso por X"))
        self.assertTrue(_should_use_browser_executor("research", "revisa el perfil de Instagram"))
        self.assertFalse(_should_use_browser_executor("research", "analiza x variable"))
        self.assertFalse(
            _should_use_browser_executor(
                _infer_session_mode("Revisa el repo de browser use"),
                "Revisa el repo de browser use",
            )
        )

    def test_autonomy_policy_allows_safe_browser_work_only(self) -> None:
        allowed = _evaluate_autonomy_policy(
            "Abre Instagram y verifica que el perfil quede visible",
            mode="browse",
            forced=False,
            autonomy_mode="autonomous",
        )
        self.assertTrue(allowed["allowed"])

        inactivity_context = _evaluate_autonomy_policy(
            "Abre Instagram hay varios dias sin postear nada",
            mode="browse",
            forced=False,
            autonomy_mode="autonomous",
        )
        self.assertTrue(inactivity_context["allowed"])

        for planning_text in (
            "Tenemos que postear estamos atrasados",
            "Hay que crear material para publicar",
            "Necesitamos ideas para publicar esta semana",
        ):
            self.assertNotEqual(_infer_session_mode(planning_text), "publish")
            planning_policy = _evaluate_autonomy_policy(
                planning_text,
                mode=_infer_session_mode(planning_text),
                forced=False,
                autonomy_mode="autonomous",
            )
            self.assertNotEqual(planning_policy["reason"], "sensitive_action", planning_text)

        still_block_publish = _evaluate_autonomy_policy(
            "Abre Instagram hay varios dias sin postear nada; publica esto",
            mode="browse",
            forced=False,
            autonomy_mode="autonomous",
        )
        self.assertFalse(still_block_publish["allowed"])
        self.assertEqual(still_block_publish["reason"], "sensitive_action")

        ops_allowed = _evaluate_autonomy_policy(
            "Revisa Chrome por CDP y toma screenshot",
            mode="ops",
            forced=False,
            autonomy_mode="autonomous",
        )
        self.assertTrue(ops_allowed["allowed"])

        generic_ops = _evaluate_autonomy_policy(
            "corre el script de backup",
            mode="ops",
            forced=False,
            autonomy_mode="autonomous",
        )
        self.assertFalse(generic_ops["allowed"])
        self.assertEqual(generic_ops["reason"], "unsupported_mode")

        for text in (
            "publica esto en Instagram",
            "postea el reel en Instagram",
            "sube esto a Instagram",
            "hay que publicar esto en Instagram",
            "haz merge del PR",
            "envia un DM por Instagram",
            "compra el plan premium",
            "borra la base de datos",
        ):
            blocked = _evaluate_autonomy_policy(
                text,
                mode=_infer_session_mode(text),
                forced=False,
                autonomy_mode="autonomous",
            )
            self.assertFalse(blocked["allowed"], text)
            self.assertEqual(blocked["reason"], "sensitive_action", text)

    def test_research_bare_platform_mention_stays_on_coordinator(self) -> None:
        # A research task that merely names a platform must NOT hijack to the
        # browser executor; only an explicit browse action does.
        self.assertFalse(
            _should_use_browser_executor("research", "investiga la historia de Twitter")
        )
        self.assertTrue(_should_use_browser_executor("research", "Haz un repaso por X"))

    def test_blocked_profile_gate_message_classifies_as_failure(self) -> None:
        # A named-profile gate that blocks (needs_login / challenge) returns a
        # human status; it must terminate as failed, never as a false "passed".
        from claw_v2.browser_profiles import (
            BROWSER_PROFILES,
            BrowserProfileHealth,
            human_message,
        )
        from claw_v2.task_handler import _browser_output_indicates_failure

        x = BROWSER_PROFILES["x"]
        self.assertTrue(
            _browser_output_indicates_failure(human_message(x, BrowserProfileHealth.NEEDS_LOGIN))
        )
        self.assertTrue(
            _browser_output_indicates_failure(
                human_message(x, BrowserProfileHealth.BLOCKED_BY_CHALLENGE)
            )
        )
        self.assertTrue(
            _browser_output_indicates_failure("LIMITACIÓN CRÍTICA - No puedo ejecutar esta tarea")
        )
        self.assertTrue(
            _browser_output_indicates_failure(
                "No puedo completar esta tarea tal como está especificada."
            )
        )
        self.assertFalse(_browser_output_indicates_failure("Capturé 32 posts del timeline"))

    def _handler(self, root: Path, recorded: dict, *, browser_executor):
        memory = MemoryStore(root / "claw.db")
        observe = ObserveStream(root / "observe.db")

        class _RecordingCoordinator:
            def run(self, task_id, objective, research_tasks, **kwargs):
                recorded["coordinator_ran"] = True
                return CoordinatorResult(task_id=task_id, phase_results={}, synthesis="coord")

        return TaskHandler(
            coordinator=_RecordingCoordinator(),
            observe=observe,
            browser_executor=browser_executor,
            get_session_state=memory.get_session_state,
            update_session_state=memory.update_session_state,
        )

    def test_browse_objective_uses_browser_executor_not_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}

            def fake_exec(objective, *, task_id, mode):
                recorded["executor"] = (objective, task_id, mode)
                return "feed capturado: 30 posts"

            handler = self._handler(Path(tmpdir), recorded, browser_executor=fake_exec)
            out = handler._run_coordinated_task(
                "tg-1", "repaso por X", mode="browse", forced=False, task_id="t-1"
            )
            self.assertEqual(recorded["executor"], ("repaso por X", "t-1", "browse"))
            self.assertNotIn("coordinator_ran", recorded)
            self.assertIn("feed capturado", out)

    def test_autonomous_browse_message_starts_browser_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            recorded: dict = {}

            class _RecordingCoordinator:
                def run(self, task_id, objective, research_tasks, **kwargs):
                    recorded["coordinator_ran"] = True
                    return CoordinatorResult(task_id=task_id, phase_results={}, synthesis="coord")

            def fake_exec(objective, *, task_id, mode):
                recorded["executor"] = (objective, task_id, mode)
                return "Instagram visible y verificado"

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                browser_executor=fake_exec,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )
            memory.update_session_state("tg-1", autonomy_mode="autonomous", step_budget=8)

            reply = handler.maybe_run_coordinated_task("tg-1", "Abre Instagram")

            self.assertIsNotNone(reply)
            self.assertIn("Tarea autónoma iniciada", reply or "")
            task_id = (reply or "").split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=2))
            self.assertEqual(recorded["executor"], ("Abre Instagram", task_id, "browse"))
            self.assertNotIn("coordinator_ran", recorded)
            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.mode, "browse")
            self.assertEqual(record.status, "succeeded")

    def test_publish_planning_language_falls_through_to_brain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            recorded: dict = {}

            class _RecordingCoordinator:
                def run(self, task_id, objective, research_tasks, **kwargs):
                    recorded["coordinator_ran"] = True
                    return CoordinatorResult(task_id=task_id, phase_results={}, synthesis="coord")

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )
            memory.update_session_state("tg-1", autonomy_mode="autonomous", step_budget=8)

            for text in (
                "Tenemos que postear estamos atrasados",
                "Hay que crear material para publicar",
            ):
                self.assertIsNone(handler.maybe_run_coordinated_task("tg-1", text), text)

            state = memory.get_session_state("tg-1")
            self.assertNotEqual(state.get("verification_status"), "blocked")
            self.assertNotIn("coordinator_ran", recorded)

    def test_publish_command_after_negation_is_still_blocked(self) -> None:
        # Regression (audit M1): a real imperative publish command must not slip
        # past the block by riding a leading negation that the context-only strip
        # would otherwise consume ("no publiques … pero igual publica esto").
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            recorded: dict = {}

            class _RecordingCoordinator:
                def run(self, task_id, objective, research_tasks, **kwargs):
                    recorded["coordinator_ran"] = True
                    return CoordinatorResult(task_id=task_id, phase_results={}, synthesis="coord")

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )
            memory.update_session_state("tg-1", autonomy_mode="autonomous", step_budget=8)

            result = handler.maybe_run_coordinated_task(
                "tg-1", "no publiques todavía pero igual publica esto ahora en instagram"
            )
            # Blocked: returns the policy block and never runs the coordinator.
            self.assertIsNotNone(result)
            self.assertEqual(memory.get_session_state("tg-1").get("verification_status"), "blocked")
            self.assertNotIn("coordinator_ran", recorded)

    def test_browser_use_repo_review_uses_coordinator_not_browser_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            recorded: dict = {}

            class _RecordingCoordinator:
                def run(self, task_id, objective, research_tasks, **kwargs):
                    recorded["coordinator"] = (objective, kwargs.get("implementation_tasks"))
                    return CoordinatorResult(
                        task_id=task_id,
                        phase_results={
                            "verification": [
                                WorkerResult(
                                    task_name="verify_repo_review",
                                    content="Verification Status: passed",
                                    duration_seconds=0.0,
                                )
                            ]
                        },
                        synthesis="repo revisado",
                    )

            def browser_executor(objective, *, task_id, mode):
                recorded["browser_executor"] = (objective, task_id, mode)
                return "should not run"

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                browser_executor=browser_executor,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )
            memory.update_session_state("tg-1", autonomy_mode="autonomous", step_budget=8)

            reply = handler.maybe_run_coordinated_task("tg-1", "Revisa el repo de browser use")

            self.assertIsNotNone(reply)
            self.assertIn("Tarea autónoma iniciada", reply or "")
            task_id = (reply or "").split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=2))
            self.assertIn("coordinator", recorded)
            self.assertNotIn("browser_executor", recorded)
            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.mode, "coding")
            self.assertEqual(memory.get_session_state("tg-1").get("mode"), "coding")

    def test_ops_x_sweep_uses_browser_executor_not_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}

            def fake_exec(objective, *, task_id, mode):
                recorded["executor"] = (objective, task_id, mode)
                return "feed capturado: 30 posts"

            handler = self._handler(Path(tmpdir), recorded, browser_executor=fake_exec)
            out = handler._run_coordinated_task(
                "tg-1", "Haz un repaso por X", mode="ops", forced=False, task_id="t-x"
            )
            self.assertEqual(recorded["executor"], ("Haz un repaso por X", "t-x", "ops"))
            self.assertNotIn("coordinator_ran", recorded)
            self.assertIn("feed capturado", out)

    def test_ops_without_cdp_signal_uses_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}

            def fake_exec(objective, *, task_id, mode):
                recorded["executor_called"] = True
                return "x"

            handler = self._handler(Path(tmpdir), recorded, browser_executor=fake_exec)
            handler._run_coordinated_task(
                "tg-1", "corre el script de backup", mode="ops", forced=False, task_id="t-2"
            )
            self.assertTrue(recorded["coordinator_ran"])
            self.assertNotIn("executor_called", recorded)

    def test_browser_executor_failure_is_contained(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}

            def boom(objective, *, task_id, mode):
                raise RuntimeError("cdp blew up")

            handler = self._handler(Path(tmpdir), recorded, browser_executor=boom)
            out = handler._run_coordinated_task(
                "tg-1", "abre la web", mode="browse", forced=False, task_id="t-3"
            )
            self.assertIn("No pude completar", out)
            self.assertNotIn("coordinator_ran", recorded)

    def test_browser_task_session_state_db_error_is_not_classified_as_cdp_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            task_id = "t-db"
            job = jobs.enqueue(
                kind="coordinator.autonomous_task",
                payload={
                    "task_id": task_id,
                    "session_id": "tg-1",
                    "objective": "repaso por X",
                    "mode": "browse",
                },
            )
            ledger.create(
                task_id=task_id,
                session_id="tg-1",
                objective="repaso por X",
                mode="browse",
                runtime="coordinator",
                provider="codex",
                model="gpt",
                status="running",
            )
            calls = {"executor": 0, "failed_update": 0}

            def fake_exec(objective, *, task_id, mode):
                calls["executor"] += 1
                return "feed capturado: 30 posts"

            def flaky_update(session_id, **kwargs):
                if kwargs.get("verification_status") == "passed" and calls["failed_update"] == 0:
                    calls["failed_update"] += 1
                    raise RuntimeDatabaseError("Runtime database WAL heal failed for claw.db")
                return memory.update_session_state(session_id, **kwargs)

            handler = TaskHandler(
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                browser_executor=fake_exec,
                get_session_state=memory.get_session_state,
                update_session_state=flaky_update,
            )

            handler._run_autonomous_task(
                "tg-1",
                task_id,
                "repaso por X",
                "browse",
                job_id=job.job_id,
            )

            self.assertEqual(calls, {"executor": 1, "failed_update": 1})
            events = observe.recent_events(limit=50)
            event_types = [event["event_type"] for event in events]
            self.assertIn("browser_executor_started", event_types)
            self.assertNotIn("browser_executor_failed", event_types)
            failures = [
                event for event in events if event["event_type"] == "autonomous_task_failed"
            ]
            self.assertTrue(failures)
            error = failures[-1]["payload"].get("error", "")
            self.assertIn("RuntimeDatabaseError", error)
            self.assertIn("WAL heal failed", error)
            self.assertNotIn("All connection attempts failed", error)

    def test_no_executor_falls_back_to_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}
            handler = self._handler(Path(tmpdir), recorded, browser_executor=None)
            handler._run_coordinated_task(
                "tg-1", "repaso por X", mode="browse", forced=False, task_id="t-4"
            )
            self.assertTrue(recorded["coordinator_ran"])

    def test_autonomous_cli_maintenance_uses_cli_runner_not_coordinator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            recorded: dict = {}

            class _RecordingCoordinator:
                def run(self, task_id, objective, research_tasks, **kwargs):
                    recorded["coordinator_ran"] = True
                    return CoordinatorResult(task_id=task_id, phase_results={}, synthesis="coord")

            def cli_runner(**kwargs):
                recorded["cli_runner"] = kwargs
                return CliMaintenanceResult(
                    verification_status="passed",
                    summary="Codex CLI updated to 0.142.4; Claude Code already current.",
                    tool_versions={
                        "codex": {
                            "installed": "0.142.3",
                            "latest": "0.142.4",
                            "verified": "0.142.4",
                            "action": "updated",
                        },
                        "claude": {
                            "installed": "2.1.195",
                            "latest": "2.1.195",
                            "verified": "2.1.195",
                            "action": "already_current",
                        },
                    },
                    commands_run=(
                        ("codex", "--version"),
                        ("npm", "view", "@openai/codex", "version"),
                    ),
                    installed_packages=("@openai/codex@0.142.4",),
                )

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                cli_maintenance_runner=cli_runner,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                workspace_root=root,
            )
            memory.update_session_state("tg-1", autonomy_mode="autonomous", step_budget=8)
            objective = "Actualiza los cli de Claude code y codex"

            reply = handler.start_autonomous_task(
                "tg-1",
                objective,
                mode="ops",
                task_kind="maintenance_update_tools",
            )

            self.assertIn("Tarea autónoma iniciada", reply)
            task_id = reply.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=2))
            self.assertIn("cli_runner", recorded)
            self.assertEqual(recorded["cli_runner"]["cwd"], root)
            self.assertNotIn("coordinator_ran", recorded)
            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "succeeded")
            self.assertEqual(record.verification_status, "passed")
            checkpoint = memory.get_session_state("tg-1").get("last_checkpoint") or {}
            self.assertEqual(checkpoint.get("operation"), "maintenance_update_tools")
            self.assertEqual(checkpoint.get("verification_status"), "passed")
            queue = memory.get_session_state("tg-1").get("task_queue") or []
            queue_item = next(item for item in queue if item["summary"] == objective)
            self.assertEqual(queue_item["status"], "done")

    def test_autonomous_job_claim_blocked_fails_closed_without_running_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            recorded: dict = {}

            class _RecordingCoordinator:
                def run(self, task_id, objective, research_tasks, **kwargs):
                    recorded["coordinator_ran"] = True
                    return CoordinatorResult(task_id=task_id, phase_results={}, synthesis="coord")

            task_id = "tg-1:t-claim-blocked"
            objective = "Actualiza los cli de Claude code y codex"
            job = jobs.enqueue(
                kind="coordinator.autonomous_task",
                payload={
                    "task_id": task_id,
                    "session_id": "tg-1",
                    "objective": objective,
                    "mode": "ops",
                },
                resume_key=TaskHandler._resume_key_for_task(task_id),
            )
            ledger.create(
                task_id=task_id,
                session_id="tg-1",
                objective=objective,
                mode="ops",
                runtime="coordinator",
                provider="codex",
                model="gpt",
                status="running",
            )
            memory.update_session_state(
                "tg-1",
                active_object={
                    "active_task": {
                        "task_id": task_id,
                        "objective": objective,
                        "mode": "ops",
                        "status": "running",
                    }
                },
                task_queue=TaskHandler.upsert_task_queue_entry(
                    [],
                    summary=objective,
                    mode="ops",
                    status="in_progress",
                    source="coordinator",
                    priority=0,
                ),
            )
            jobs.set_safe_mode_reason("branch_integrity_violation")

            handler = TaskHandler(
                coordinator=_RecordingCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )

            handler._run_autonomous_task("tg-1", task_id, objective, "ops", job_id=job.job_id)

            self.assertNotIn("coordinator_ran", recorded)
            record = ledger.get(task_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.verification_status, "blocked")
            failed_job = jobs.get(job.job_id)
            self.assertIsNotNone(failed_job)
            self.assertEqual(failed_job.status, "failed")
            self.assertIn("job_claim_blocked", failed_job.error)
            checkpoint = memory.get_session_state("tg-1").get("last_checkpoint") or {}
            self.assertEqual(checkpoint.get("verification_status"), "blocked")
            self.assertEqual(checkpoint.get("reason"), "job_claim_blocked")
            queue = memory.get_session_state("tg-1").get("task_queue") or []
            queue_item = next(item for item in queue if item["summary"] == objective)
            self.assertEqual(queue_item["status"], "blocked")
            events = [event["event_type"] for event in observe.recent_events(limit=50)]
            self.assertIn("job_claim_blocked", events)
            self.assertIn("autonomous_task_failed", events)

    def _autonomous_handler(self, root: Path, *, browser_executor):
        memory = MemoryStore(root / "claw.db")
        observe = ObserveStream(root / "observe.db")
        ledger = TaskLedger(root / "claw.db", observe=observe)
        jobs = JobService(root / "claw.db", observe=observe)
        handler = TaskHandler(
            observe=observe,
            task_ledger=ledger,
            job_service=jobs,
            browser_executor=browser_executor,
            get_session_state=memory.get_session_state,
            update_session_state=memory.update_session_state,
        )
        return handler, ledger, jobs

    def _run_autonomous_browser(self, handler, ledger, jobs, *, task_id, objective):
        job = jobs.enqueue(
            kind="coordinator.autonomous_task",
            payload={
                "task_id": task_id,
                "session_id": "tg-1",
                "objective": objective,
                "mode": "browse",
            },
        )
        ledger.create(
            task_id=task_id,
            session_id="tg-1",
            objective=objective,
            mode="browse",
            runtime="coordinator",
            provider="codex",
            model="gpt",
            status="running",
        )
        handler._run_autonomous_task("tg-1", task_id, objective, "browse", job_id=job.job_id)

    def test_browser_executor_no_result_terminates_failed_not_pending(self) -> None:
        # Regression: a "(no result)" report (e.g. planning LLM rate-limited) used
        # to land verification_status="pending", which never terminates and the
        # lifecycle watchdog resumes the task forever. It must terminate as failed.
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, ledger, jobs = self._autonomous_handler(
                Path(tmpdir), browser_executor=lambda o, *, task_id, mode: "(no result)"
            )
            self._run_autonomous_browser(
                handler, ledger, jobs, task_id="t-nores", objective="repaso por X"
            )
            event_types = [e["event_type"] for e in handler.observe.recent_events(limit=50)]
            self.assertIn("autonomous_task_failed", event_types)
            self.assertNotIn("autonomous_task_pending", event_types)
            record = ledger.get("t-nores")
            self.assertEqual(record.status, "failed")

    def test_browser_executor_refusal_text_terminates_failed_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, ledger, jobs = self._autonomous_handler(
                Path(tmpdir),
                browser_executor=lambda o, *, task_id, mode: (
                    "No puedo completar esta tarea tal como está especificada. "
                    "No tengo capacidad para ejecutar pkill."
                ),
            )
            self._run_autonomous_browser(
                handler, ledger, jobs, task_id="t-refusal", objective="Mata y relanza"
            )
            event_types = [e["event_type"] for e in handler.observe.recent_events(limit=50)]
            self.assertIn("autonomous_task_failed", event_types)
            self.assertNotIn("autonomous_task_completed", event_types)
            record = ledger.get("t-refusal")
            self.assertEqual(record.status, "failed")

    def test_browser_executor_real_result_terminates_succeeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler, ledger, jobs = self._autonomous_handler(
                Path(tmpdir),
                browser_executor=lambda o, *, task_id, mode: "Capturé 32 posts del timeline: ...",
            )
            self._run_autonomous_browser(
                handler, ledger, jobs, task_id="t-ok", objective="repaso por X"
            )
            event_types = [e["event_type"] for e in handler.observe.recent_events(limit=50)]
            self.assertIn("autonomous_task_completed", event_types)
            self.assertNotIn("autonomous_task_pending", event_types)
            record = ledger.get("t-ok")
            self.assertEqual(record.status, "succeeded")


class StaleMessageTests(unittest.TestCase):
    """AM-STALEMSG — the terminal message is formatted AFTER the gates."""

    def test_gate_downgraded_task_notifies_failure_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            jobs = JobService(root / "claw.db", observe=observe)
            stored: list[tuple[str, str, str]] = []

            class _BlockedCoordinator:
                def run(
                    self,
                    task_id,
                    objective,
                    research_tasks,
                    implementation_tasks=None,
                    verification_tasks=None,
                    lane_overrides=None,
                    **kwargs,
                ):
                    return CoordinatorResult(
                        task_id=task_id,
                        phase_results={
                            "verification": [
                                WorkerResult(
                                    task_name="verify_change",
                                    content=(
                                        "Verification Status: pending\n"
                                        "Siguiente paso: solicitar al usuario el enlace del documento"
                                    ),
                                    duration_seconds=0.1,
                                )
                            ]
                        },
                        synthesis="plan listo",
                    )

            handler = TaskHandler(
                coordinator=_BlockedCoordinator(),
                observe=observe,
                task_ledger=ledger,
                job_service=jobs,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
                store_message=lambda sid, role, text: stored.append((sid, role, text)),
                workspace_root=root,
            )
            ack = handler.start_autonomous_task("tg-1", "implementa el informe", mode="coding")
            task_id = ack.split("`", 2)[1]
            self.assertTrue(handler.wait_for_task(task_id, timeout=5))

            events = observe.recent_events(limit=200)
            failed = next(e for e in events if e["event_type"] == "autonomous_task_failed")
            self.assertIn("No pude cerrar bien la tarea", failed["payload"]["response"])
            self.assertNotIn("Listo. Cerr", failed["payload"]["response"])
            # AM-NOTIFY: terminal events carry the attempt for per-attempt dedupe.
            self.assertIn("attempt", failed["payload"])
            assistant_texts = [text for _sid, role, text in stored if role == "assistant"]
            self.assertTrue(assistant_texts, "assistant message must be stored")
            self.assertTrue(
                assistant_texts[-1].startswith("No pude cerrar bien la tarea"),
                assistant_texts[-1],
            )


class TaskQueueVocabularyTests(unittest.TestCase):
    """AM-VOCAB — every queue write goes through the single status map."""

    def test_upsert_normalizes_cross_vocabulary_statuses(self) -> None:
        for raw, expected in (
            ("passed", "done"),
            ("succeeded", "done"),
            ("failed", "blocked"),
            ("awaiting_approval", "blocked"),
            ("unknown", "pending"),
            ("running", "in_progress"),
            ("deferred", "deferred"),
        ):
            queue = TaskHandler.upsert_task_queue_entry(
                [],
                summary=f"tarea {raw}",
                mode="coding",
                status=raw,
                source="coordinator",
                priority=0,
            )
            self.assertEqual(queue[0]["status"], expected, raw)

    def test_set_task_queue_status_normalizes(self) -> None:
        queue = [{"task_id": "x", "summary": "s", "status": "pending", "priority": 1}]
        updated = TaskHandler.set_task_queue_status(queue, task_id="x", to_status="succeeded")
        self.assertEqual(updated[0]["status"], "done")
        updated = TaskHandler.set_task_queue_status(queue, task_id="x", to_status="deferred")
        self.assertEqual(updated[0]["status"], "deferred")

    def test_task_attempt_reads_ledger_resume_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            ledger = TaskLedger(root / "claw.db", observe=observe)
            handler = TaskHandler(
                coordinator=None,
                observe=observe,
                task_ledger=ledger,
                get_session_state=memory.get_session_state,
                update_session_state=memory.update_session_state,
            )
            self.assertEqual(handler._task_attempt("missing"), 0)
            ledger.create(
                task_id="t-attempt",
                session_id="tg-1",
                objective="obj",
                mode="coding",
                runtime="coordinator",
                provider="anthropic",
                model="m",
                status="running",
                metadata={"resume_count": 2},
            )
            self.assertEqual(handler._task_attempt("t-attempt"), 2)


if __name__ == "__main__":
    unittest.main()
