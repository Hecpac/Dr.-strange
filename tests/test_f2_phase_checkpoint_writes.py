from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.config import AppConfig
from claw_v2.coordinator import (
    IMPLEMENTATION_STARTED_MARKER,
    CoordinatorService,
    WorkerTask,
)
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.main import build_runtime
from claw_v2.sqlite_runtime import RuntimeDb
from claw_v2.types import LLMResponse


def _fake_anthropic(request: LLMRequest) -> LLMResponse:
    return LLMResponse(
        content=f"handled:{request.lane}",
        lane=request.lane,
        provider=request.provider,
        model=request.model,
    )


def _runtime_env(root: Path, *, enabled: bool | None = None) -> dict[str, str]:
    env = {
        "DB_PATH": str(root / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(root / "workspace"),
        "AGENT_STATE_ROOT": str(root / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
        "APPROVALS_ROOT": str(root / "approvals"),
        "PIPELINE_STATE_ROOT": str(root / "pipeline"),
        "TELEMETRY_ROOT": str(root / "telemetry"),
    }
    if enabled is not None:
        env["CLAW_F2_DURABILITY_ENABLED"] = "1" if enabled else "0"
    return env


def _make_coordinator(
    *,
    f2_store: object | None = None,
    scratch_root: Path | None = None,
) -> tuple[CoordinatorService, MagicMock, MagicMock]:
    router = MagicMock()
    observe = MagicMock()
    svc = CoordinatorService(
        router=router,
        observe=observe,
        scratch_root=scratch_root or Path(tempfile.mkdtemp()),
        max_workers=1,
        f2_durability_store=f2_store,
    )
    return svc, router, observe


class F2PhaseCheckpointWriteTests(unittest.TestCase):
    def _runtime_db(self, tmpdir: str) -> RuntimeDb:
        db = RuntimeDb(Path(tmpdir) / "claw.db")
        self.addCleanup(db.close)
        return db

    def test_flag_defaults_false_and_accepts_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            home = str(Path(tmpdir) / "home")
            with patch.dict(os.environ, {"HOME": home}, clear=True):
                default_config = AppConfig.from_env()
            self.assertFalse(default_config.f2_durability_enabled)

            with patch.dict(
                os.environ,
                {"HOME": home, "CLAW_F2_DURABILITY_ENABLED": "true"},
                clear=True,
            ):
                enabled_config = AppConfig.from_env()
            self.assertTrue(enabled_config.f2_durability_enabled)

    def test_runtime_does_not_create_f2_store_or_tables_when_flag_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime_env(root, enabled=False), clear=False):
                runtime = build_runtime(anthropic_executor=_fake_anthropic)
            self.addCleanup(runtime.memory._db.close)

            self.assertFalse(runtime.config.f2_durability_enabled)
            self.assertIsNone(runtime.f2_durability_store)
            self.assertIsNone(runtime.coordinator.f2_durability_store)
            with runtime.memory._db.cursor() as cur:
                f2_tables = [
                    row["name"]
                    for row in cur.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                          AND name IN (
                              'phase_checkpoints',
                              'phase_checkpoint_writes',
                              'external_effect_records',
                              'phase_recovery_cursors'
                          )
                        """
                    ).fetchall()
                ]
            self.assertEqual(f2_tables, [])

    def test_runtime_creates_runtimedb_backed_f2_store_when_flag_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, _runtime_env(root, enabled=True), clear=False):
                runtime = build_runtime(anthropic_executor=_fake_anthropic)
            self.addCleanup(runtime.memory._db.close)

            self.assertTrue(runtime.config.f2_durability_enabled)
            self.assertIsInstance(runtime.f2_durability_store, F2DurabilityStore)
            self.assertIs(runtime.coordinator.f2_durability_store, runtime.f2_durability_store)
            self.assertIs(runtime.f2_durability_store._db, runtime.memory._db)

    def test_enabled_coordinator_writes_phase_checkpoints_and_ordered_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            store = F2DurabilityStore(db)
            svc, router, _observe = _make_coordinator(
                f2_store=store,
                scratch_root=Path(tmpdir) / "scratch",
            )
            router.ask.return_value = MagicMock(content="ok")

            result = svc.run("task-1", "objective", [WorkerTask(name="r1", instruction="find")])

            self.assertEqual(result.error, "")
            research_writes = store.list_checkpoint_writes(
                task_id="task-1",
                run_id="task-1",
                phase="research",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in research_writes],
                [(1, "phase_started"), (2, "phase_return")],
            )
            research_checkpoints = store.list_phase_checkpoints(
                task_id="task-1",
                run_id="task-1",
                phase="research",
                order="phase_version_asc",
            )
            self.assertEqual(
                [(row.phase_version, row.status) for row in research_checkpoints],
                [(1, "started"), (2, "succeeded")],
            )
            task_writes = store.list_checkpoint_writes(
                task_id="task-1",
                run_id="task-1",
                phase="task",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in task_writes],
                [(1, "coordinator_start"), (2, "task_succeeded")],
            )
            self.assertEqual(store.list_external_effects(task_id="task-1"), [])

    def test_enabled_coordinator_writes_failed_phase_checkpoint_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            store = F2DurabilityStore(db)
            svc, router, _observe = _make_coordinator(
                f2_store=store,
                scratch_root=Path(tmpdir) / "scratch",
            )

            def fake_ask(_prompt, **kwargs):
                if kwargs.get("lane") == "worker":
                    return MagicMock(content="CRITICAL ERROR EN WORKER\nTraceback: broken")
                return MagicMock(content="ok")

            router.ask.side_effect = fake_ask

            result = svc.run(
                "task-critical",
                "objective",
                [WorkerTask(name="r1", instruction="find")],
                [WorkerTask(name="i1", instruction="build", lane="worker")],
            )

            self.assertEqual(result.error, "critical_worker_error:i1")
            implementation_writes = store.list_checkpoint_writes(
                task_id="task-critical",
                run_id="task-critical",
                phase="implementation",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in implementation_writes],
                [(1, "phase_started"), (2, "phase_return"), (3, "phase_error")],
            )
            implementation_checkpoints = store.list_phase_checkpoints(
                task_id="task-critical",
                run_id="task-critical",
                phase="implementation",
                order="phase_version_asc",
            )
            self.assertEqual(
                [(row.phase_version, row.status) for row in implementation_checkpoints],
                [(1, "started"), (2, "succeeded"), (3, "failed")],
            )
            synthesis_writes = store.list_checkpoint_writes(
                task_id="task-critical",
                run_id="task-critical",
                phase="synthesis",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in synthesis_writes],
                [
                    (1, "phase_started"),
                    (2, "phase_return"),
                    (3, "phase_started"),
                    (4, "phase_return"),
                ],
            )
            synthesis_checkpoints = store.list_phase_checkpoints(
                task_id="task-critical",
                run_id="task-critical",
                phase="synthesis",
                order="phase_version_asc",
            )
            self.assertEqual(
                [(row.phase_version, row.status) for row in synthesis_checkpoints],
                [(1, "started"), (2, "succeeded"), (3, "started"), (4, "succeeded")],
            )
            task_writes = store.list_checkpoint_writes(
                task_id="task-critical",
                run_id="task-critical",
                phase="task",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in task_writes],
                [(1, "coordinator_start"), (2, "task_failed")],
            )
            self.assertEqual(store.list_external_effects(task_id="task-critical"), [])

    def test_generic_exception_closes_active_f2_phase_and_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            store = F2DurabilityStore(db)
            svc, _router, observe = _make_coordinator(
                f2_store=store,
                scratch_root=Path(tmpdir) / "scratch",
            )
            svc._dispatch_parallel = MagicMock(side_effect=RuntimeError("dispatch exploded"))

            result = svc.run(
                "task-generic-fail",
                "objective",
                [WorkerTask(name="r1", instruction="find")],
            )

            self.assertEqual(result.error, "dispatch exploded")
            research_writes = store.list_checkpoint_writes(
                task_id="task-generic-fail",
                run_id="task-generic-fail",
                phase="research",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in research_writes],
                [(1, "phase_started"), (2, "phase_error")],
            )
            research_checkpoints = store.list_phase_checkpoints(
                task_id="task-generic-fail",
                run_id="task-generic-fail",
                phase="research",
                order="phase_version_asc",
            )
            self.assertEqual(
                [(row.phase_version, row.status) for row in research_checkpoints],
                [(1, "started"), (2, "failed")],
            )
            task_writes = store.list_checkpoint_writes(
                task_id="task-generic-fail",
                run_id="task-generic-fail",
                phase="task",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in task_writes],
                [(1, "coordinator_start"), (2, "task_failed")],
            )
            event_names = [call.args[0] for call in observe.emit.call_args_list if call.args]
            self.assertNotIn("coordinator_complete", event_names)

    def test_implementation_marker_is_not_left_when_f2_phase_start_fails(self) -> None:
        class ImplementationStartFailingStore(F2DurabilityStore):
            def append_checkpoint_write(self, **kwargs):
                if (
                    kwargs.get("phase") == "implementation"
                    and kwargs.get("write_kind") == "phase_started"
                ):
                    raise RuntimeError("implementation f2 start failed")
                return super().append_checkpoint_write(**kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = self._runtime_db(tmpdir)
            store = ImplementationStartFailingStore(db)
            scratch_root = Path(tmpdir) / "scratch"
            svc, router, observe = _make_coordinator(
                f2_store=store,
                scratch_root=scratch_root,
            )
            router.ask.return_value = MagicMock(content="ok")

            result = svc.run(
                "task-marker",
                "objective",
                [WorkerTask(name="r1", instruction="find")],
                [WorkerTask(name="i1", instruction="build", lane="worker")],
            )

            self.assertIn("implementation f2 start failed", result.error)
            self.assertFalse(
                (scratch_root / "task-marker" / IMPLEMENTATION_STARTED_MARKER).exists()
            )
            task_writes = store.list_checkpoint_writes(
                task_id="task-marker",
                run_id="task-marker",
                phase="task",
            )
            self.assertEqual(
                [(row.write_order, row.write_kind) for row in task_writes],
                [(1, "coordinator_start"), (2, "task_failed")],
            )
            event_names = [call.args[0] for call in observe.emit.call_args_list if call.args]
            self.assertIn("f2_durability_write_failed", event_names)
            self.assertNotIn("coordinator_complete", event_names)

    def test_checkpoint_write_failure_is_visible_and_does_not_fake_success(self) -> None:
        class FailingStore:
            def append_checkpoint_write(self, **_kwargs):
                raise RuntimeError("f2 unavailable")

        svc, router, observe = _make_coordinator(f2_store=FailingStore())
        router.ask.return_value = MagicMock(content="should not run")

        result = svc.run("task-fail", "objective", [WorkerTask(name="r1", instruction="find")])

        self.assertIn("f2 unavailable", result.error)
        self.assertNotIn("research", result.phase_results)
        event_names = [call.args[0] for call in observe.emit.call_args_list if call.args]
        self.assertIn("f2_durability_write_failed", event_names)
        self.assertNotIn("coordinator_complete", event_names)


if __name__ == "__main__":
    unittest.main()
