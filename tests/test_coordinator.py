from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.coordinator import CoordinatorService, WorkerTask


def _make_service(**overrides):
    router = MagicMock()
    observe = MagicMock()
    tmpdir = overrides.pop("scratch_root", None) or Path(tempfile.mkdtemp())
    defaults = dict(
        router=router,
        observe=observe,
        scratch_root=tmpdir,
        max_workers=2,
    )
    defaults.update(overrides)
    svc = CoordinatorService(**defaults)
    return svc, router, observe, tmpdir


class DispatchParallelTests(unittest.TestCase):
    def test_empty_tasks_returns_empty(self) -> None:
        svc, *_ = _make_service()
        results = svc._dispatch_parallel([])
        self.assertEqual(results, [])

    def test_single_task_returns_result(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content="found something")
        tasks = [WorkerTask(name="t1", instruction="research X")]
        results = svc._dispatch_parallel(tasks)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].task_name, "t1")
        self.assertEqual(results[0].content, "found something")
        self.assertEqual(results[0].error, "")

    def test_multiple_tasks_run_in_parallel(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        tasks = [
            WorkerTask(name="t1", instruction="do A"),
            WorkerTask(name="t2", instruction="do B"),
            WorkerTask(name="t3", instruction="do C"),
        ]
        results = svc._dispatch_parallel(tasks)
        self.assertEqual(len(results), 3)
        self.assertEqual(router.ask.call_count, 3)

    def test_worker_error_captured(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.side_effect = RuntimeError("provider down")
        tasks = [WorkerTask(name="t1", instruction="fail")]
        results = svc._dispatch_parallel(tasks)
        self.assertEqual(len(results), 1)
        self.assertIn("provider down", results[0].error)
        self.assertEqual(results[0].content, "")


class SynthesizeTests(unittest.TestCase):
    def test_synthesis_calls_router(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content="Step 1: do X\nStep 2: do Y")
        from claw_v2.coordinator import WorkerResult
        findings = [
            WorkerResult(task_name="r1", content="data A", duration_seconds=1.0),
            WorkerResult(task_name="r2", content="data B", duration_seconds=1.0),
        ]
        result = svc._synthesize("build feature", findings)
        self.assertIn("Step 1", result)
        call_kwargs = router.ask.call_args
        self.assertEqual(call_kwargs.kwargs["lane"], "research")
        self.assertIn("evidence_pack", call_kwargs.kwargs)

    def test_synthesis_handles_error(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.side_effect = RuntimeError("oops")
        from claw_v2.coordinator import WorkerResult
        result = svc._synthesize("objective", [
            WorkerResult(task_name="r1", content="data", duration_seconds=0.5),
        ])
        self.assertEqual(result, "")


class InjectContextTests(unittest.TestCase):
    def test_prepends_synthesis(self) -> None:
        tasks = [WorkerTask(name="impl1", instruction="write code")]
        result = CoordinatorService._inject_context(tasks, "the plan")
        self.assertEqual(len(result), 1)
        self.assertIn("the plan", result[0].instruction)
        self.assertIn("write code", result[0].instruction)
        self.assertEqual(result[0].name, "impl1")


class FullRunTests(unittest.TestCase):
    def test_research_only_run(self) -> None:
        svc, router, observe, tmpdir = _make_service()
        router.ask.return_value = MagicMock(content="result")
        tasks = [WorkerTask(name="r1", instruction="research")]

        result = svc.run("test-task", "find bugs", tasks)

        self.assertEqual(result.task_id, "test-task")
        self.assertIn("research", result.phase_results)
        self.assertEqual(len(result.phase_results["research"]), 1)
        self.assertGreater(result.duration_seconds, 0)
        self.assertEqual(result.error, "")
        observe.emit.assert_called_once()

    def test_full_four_phase_run(self) -> None:
        svc, router, observe, tmpdir = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        research = [WorkerTask(name="r1", instruction="find")]
        impl = [WorkerTask(name="i1", instruction="build")]
        verify = [WorkerTask(name="v1", instruction="check")]

        result = svc.run("full-task", "objective", research, impl, verify)

        self.assertIn("research", result.phase_results)
        self.assertIn("implementation", result.phase_results)
        self.assertIn("verification", result.phase_results)
        self.assertNotEqual(result.synthesis, "")
        self.assertEqual(result.error, "")

    def test_scratch_directory_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            svc, router, observe, _ = _make_service(scratch_root=Path(tmpdir))
            router.ask.return_value = MagicMock(content="data")
            tasks = [WorkerTask(name="r1", instruction="go")]

            svc.run("scratch-test", "obj", tasks)

            scratch = Path(tmpdir) / "scratch-test" / "research"
            self.assertTrue(scratch.exists())
            files = list(scratch.iterdir())
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            self.assertEqual(data["task_name"], "r1")
            self.assertEqual(data["content"], "data")

    def test_run_handles_exception_gracefully(self) -> None:
        svc, router, observe, _ = _make_service()
        router.ask.side_effect = RuntimeError("boom")
        tasks = [WorkerTask(name="r1", instruction="fail")]

        result = svc.run("fail-task", "obj", tasks)

        # Research workers capture errors individually, synthesis fails → empty
        self.assertEqual(result.synthesis, "")
        self.assertGreater(result.duration_seconds, 0)


class WorkerTaskTests(unittest.TestCase):
    def test_default_lane(self) -> None:
        task = WorkerTask(name="t", instruction="do")
        self.assertEqual(task.lane, "research")

    def test_custom_lane(self) -> None:
        task = WorkerTask(name="t", instruction="do", lane="worker")
        self.assertEqual(task.lane, "worker")


if __name__ == "__main__":
    unittest.main()
