from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from claw_v2.coordinator import CoordinatorResult, WorkerResult
from claw_v2.jobs import JobService
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.task_handler import TaskHandler
from claw_v2.task_ledger import TaskLedger


class _BlockingCoordinator:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, task_id, objective, research_tasks, implementation_tasks=None, verification_tasks=None, lane_overrides=None):
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


if __name__ == "__main__":
    unittest.main()
