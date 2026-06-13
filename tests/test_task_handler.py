from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.bot_helpers import _infer_session_mode, _should_use_browser_executor
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.sqlite_runtime import RuntimeDatabaseError
from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger


class _BlockingCoordinator:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, task_id, objective, research_tasks, implementation_tasks=None, verification_tasks=None, lane_overrides=None, **kwargs):
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
            (workspace / "MEMORY.md").write_text("# MEMORY.md\n\n- durable note\n", encoding="utf-8")
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
            subprocess.run(["git", "-C", str(workspace), "add", "README.md", "MEMORY.md"], check=True)
            subprocess.run(["git", "-C", str(workspace), "commit", "-q", "-m", "init"], check=True)
            (workspace / "README.md").write_text("dirty code\n", encoding="utf-8")
            (workspace / "MEMORY.md").write_text("# MEMORY.md\n\n- durable note\n", encoding="utf-8")
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
            self.assertEqual(
                active_task["delegation_metadata"]["origin"], "brain_delegate_tool"
            )
            events = [event["event_type"] for event in observe.recent_events(limit=50)]
            self.assertIn("autonomous_task_started", events)


class ResumeWiringTests(unittest.TestCase):
    """F3.1 — a resumed coordinated task passes the detected start_phase."""

    def _handler_with_recording_coordinator(self, root: Path, recorded: dict):
        memory = MemoryStore(root / "claw.db")
        observe = ObserveStream(root / "observe.db")

        class _RecordingCoordinator:
            def detect_resume_phase(self, task_id):
                recorded["detect_task_id"] = task_id
                return "implementation"

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

    def test_resumed_run_passes_detected_start_phase_and_abort_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(Path(tmpdir), recorded)
            handler._run_coordinated_task(
                "tg-1", "objetivo", mode="coding", forced=False, task_id="t-99", resumed=True
            )
            self.assertEqual(recorded["detect_task_id"], "t-99")
            self.assertEqual(recorded["start_phase"], "implementation")
            self.assertTrue(callable(recorded["should_abort"]))
            self.assertFalse(recorded["should_abort"]())
            with handler._task_lock:
                handler._cancelled_tasks.add("t-99")
            self.assertTrue(recorded["should_abort"]())

    def test_fresh_run_passes_no_start_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            recorded: dict = {}
            handler = self._handler_with_recording_coordinator(Path(tmpdir), recorded)
            handler._run_coordinated_task(
                "tg-1", "objetivo", mode="coding", forced=False, task_id="t-100"
            )
            self.assertIsNone(recorded["start_phase"])
            self.assertNotIn("detect_task_id", recorded)


class BrowserExecutorRoutingTests(unittest.TestCase):
    """Option (b), 2026-06-13: CDP/browser objectives route to the in-process
    browser executor instead of the network-denied Codex coordinator."""

    def test_x_sweep_objective_infers_browse_mode(self) -> None:
        self.assertEqual(_infer_session_mode("Haz un repaso por X"), "browse")
        self.assertEqual(_infer_session_mode("lee X por CDP"), "browse")
        self.assertTrue(_should_use_browser_executor("ops", "Haz un repaso por X"))
        self.assertFalse(_should_use_browser_executor("research", "analiza x variable"))

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
                event for event in events
                if event["event_type"] == "autonomous_task_failed"
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
            payload={"task_id": task_id, "session_id": "tg-1", "objective": objective, "mode": "browse"},
        )
        ledger.create(
            task_id=task_id, session_id="tg-1", objective=objective, mode="browse",
            runtime="coordinator", provider="codex", model="gpt", status="running",
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
