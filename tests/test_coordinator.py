from __future__ import annotations

import json
import tempfile
import unittest
from contextvars import copy_context
from pathlib import Path
from typing import Any
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

    def test_worker_thread_preserves_tool_artifact_context(self) -> None:
        from claw_v2.turn_context import (
            current_tool_artifact_result,
            record_tool_artifact_result,
            reset_tool_artifact_result,
        )
        from claw_v2.verification.local_tool_runner import CONTRACT_REQUIRED_KEY

        svc, router, *_ = _make_service()

        def fake_ask(_prompt, **_kwargs):
            record_tool_artifact_result({CONTRACT_REQUIRED_KEY: True})
            return MagicMock(content="ok")

        reset_tool_artifact_result()
        try:
            router.ask.side_effect = fake_ask
            results = svc._dispatch_parallel([WorkerTask(name="t1", instruction="do A")])

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].error, "")
            result = current_tool_artifact_result()
            self.assertIsNotNone(result)
            assert result is not None
            self.assertTrue(result[CONTRACT_REQUIRED_KEY])
        finally:
            reset_tool_artifact_result()

    def test_reset_tool_artifact_result_isolates_copied_contexts(self) -> None:
        from claw_v2.turn_context import (
            current_tool_artifact_result,
            record_tool_artifact_result,
            reset_tool_artifact_result,
        )
        from claw_v2.verification.local_tool_runner import CONTRACT_REQUIRED_KEY

        reset_tool_artifact_result()
        stale_context = copy_context()
        reset_tool_artifact_result()
        try:
            stale_context.run(record_tool_artifact_result, {CONTRACT_REQUIRED_KEY: True})

            self.assertIsNone(current_tool_artifact_result())
        finally:
            reset_tool_artifact_result()

    def test_worker_error_captured(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.side_effect = RuntimeError("provider down")
        tasks = [WorkerTask(name="t1", instruction="fail")]
        results = svc._dispatch_parallel(tasks)
        self.assertEqual(len(results), 1)
        self.assertIn("provider down", results[0].error)
        self.assertEqual(results[0].content, "")

    def test_critical_worker_error_cancels_pending_futures(self) -> None:
        svc, router, *_ = _make_service(max_workers=1)
        calls: list[str] = []

        def fake_ask(prompt, **_kwargs):
            calls.append(prompt)
            if "critical" in prompt:
                return MagicMock(content="CRITICAL ERROR EN WORKER\nTraceback: broken local env")
            return MagicMock(content="should not run")

        router.ask.side_effect = fake_ask
        tasks = [
            WorkerTask(name="critical", instruction="critical"),
            WorkerTask(name="pending", instruction="pending"),
        ]
        results = svc._dispatch_parallel(tasks)

        self.assertEqual(len(results), 1)
        self.assertIn("CRITICAL ERROR EN WORKER", results[0].content)
        self.assertEqual(results[0].task_name, "critical")
        self.assertNotIn("should not run", [result.content for result in results])


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
        self.assertEqual(call_kwargs.kwargs["role"], "coordinator_research")
        self.assertEqual(call_kwargs.kwargs["timeout"], 90.0)
        self.assertIn("evidence_pack", call_kwargs.kwargs)

    def test_synthesis_distills_long_worker_summaries(self) -> None:
        svc, router, *_ = _make_service(worker_result_summary_chars=120)
        router.ask.side_effect = [
            MagicMock(content="- causa raiz: fallo en /Users/hector/Projects/Dr.-strange/claw_v2/foo.py"),
            MagicMock(content="plan"),
        ]
        from claw_v2.coordinator import WorkerResult
        tail_marker = "FULL_CONTENT_TAIL_SHOULD_NOT_APPEAR"
        svc._synthesize("build feature", [
            WorkerResult(
                task_name="r1",
                content=("important " * 200) + tail_marker,
                duration_seconds=1.0,
            ),
        ])

        distill_prompt = router.ask.call_args_list[0].args[0]
        synthesis_prompt = router.ask.call_args_list[-1].args[0]
        self.assertIn("Destilación de Contexto Crítico", distill_prompt)
        self.assertIn("Evidencia Recopilada por los Workers", synthesis_prompt)
        self.assertIn("causa raiz", synthesis_prompt)
        self.assertNotIn(tail_marker, synthesis_prompt)

    def test_distillation_fallback_preserves_head_tail_and_marks_degraded(self) -> None:
        svc, router, *_ = _make_service(worker_result_summary_chars=360)
        router.ask.side_effect = [RuntimeError("distiller down"), MagicMock(content="plan")]
        from claw_v2.coordinator import MECHANICAL_TRUNCATION_SIGNATURE, WorkerResult
        worker = WorkerResult(
            task_name="r1",
            content="HEAD-/Users/hector/start\n" + ("noise\n" * 500) + "TAIL-claw_v2/end.py",
            duration_seconds=1.0,
        )

        svc._synthesize("build feature", [worker])

        synthesis_prompt = router.ask.call_args_list[-1].args[0]
        self.assertTrue(worker.degraded_compaction)
        self.assertIn("HEAD-/Users/hector/start", synthesis_prompt)
        self.assertIn("TAIL-claw_v2/end.py", synthesis_prompt)
        self.assertIn(MECHANICAL_TRUNCATION_SIGNATURE, synthesis_prompt)

    def test_synthesis_handles_error(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.side_effect = RuntimeError("oops")
        from claw_v2.coordinator import WorkerResult
        result = svc._synthesize("objective", [
            WorkerResult(task_name="r1", content="data", duration_seconds=0.5),
        ])
        self.assertEqual(result, "")


class InjectContextTests(unittest.TestCase):
    def test_prepends_artifact_reference_and_summary(self) -> None:
        tasks = [WorkerTask(name="impl1", instruction="write code")]
        result = CoordinatorService._inject_context(
            tasks,
            objective="ship feature",
            input_artifact_ref="art:abc123",
            input_summary="the plan",
        )
        self.assertEqual(len(result), 1)
        self.assertIn("Contexto de Continuidad Operativa", result[0].instruction)
        self.assertIn("Objetivo General del Dueño:** ship feature", result[0].instruction)
        self.assertIn("Artefacto de Referencia en Scratch:** art:abc123", result[0].instruction)
        self.assertIn("the plan", result[0].instruction)
        self.assertIn("write code", result[0].instruction)
        self.assertEqual(result[0].name, "impl1")

    def test_preserves_assigned_agent(self) -> None:
        tasks = [WorkerTask(name="impl1", instruction="write code", assigned_agent="hex")]
        result = CoordinatorService._inject_context(
            tasks,
            objective="ship feature",
            input_artifact_ref="art:abc123",
            input_summary="the plan",
        )
        self.assertEqual(result[0].assigned_agent, "hex")


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
        self.assertEqual(observe.emit.call_count, 2)

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

    def test_critical_worker_error_runs_self_healing_synthesis_and_stops(self) -> None:
        svc, router, observe, _ = _make_service()
        prompts: list[str] = []

        def fake_ask(prompt, **kwargs):
            prompts.append(prompt)
            if kwargs.get("lane") == "worker":
                return MagicMock(
                    content=(
                        "CRITICAL ERROR EN WORKER\n"
                        "Traceback (most recent call last):\n"
                        "  File \"/Users/hector/Projects/Dr.-strange/claw_v2/broken.py\", line 7\n"
                        "RuntimeError: missing dependency"
                    )
                )
            return MagicMock(content="**Step 1 [rook]:** Diagnosticar dependencia local.")

        router.ask.side_effect = fake_ask
        research = [WorkerTask(name="r1", instruction="find")]
        impl = [WorkerTask(name="i1", instruction="build", lane="worker")]
        verify = [WorkerTask(name="v1", instruction="check", lane="verifier")]

        result = svc.run("critical-task", "objective", research, impl, verify)

        self.assertEqual(result.error, "critical_worker_error:i1")
        self.assertTrue(result.audit["critical_worker_error"])
        self.assertIn("implementation", result.phase_results)
        self.assertNotIn("verification", result.phase_results)
        self.assertIn("/Users/hector/Projects/Dr.-strange/claw_v2/broken.py", result.audit["raw_error"])
        critical_synthesis_prompt = prompts[-1]
        self.assertIn("Self-Healing", critical_synthesis_prompt)
        self.assertIn("CRITICAL ERROR EN WORKER", critical_synthesis_prompt)


class WorkerTaskTests(unittest.TestCase):
    def test_default_lane(self) -> None:
        task = WorkerTask(name="t", instruction="do")
        self.assertEqual(task.lane, "research")

    def test_custom_lane(self) -> None:
        task = WorkerTask(name="t", instruction="do", lane="worker")
        self.assertEqual(task.lane, "worker")

    def test_accepts_timeout_override(self) -> None:
        task = WorkerTask(name="t", instruction="do", timeout_seconds=12.5)
        self.assertEqual(task.timeout_seconds, 12.5)


class AgentAwareTests(unittest.TestCase):
    def test_worker_task_accepts_assigned_agent(self) -> None:
        task = WorkerTask(name="t1", instruction="fix bug", assigned_agent="hex")
        self.assertEqual(task.assigned_agent, "hex")

    def test_worker_task_default_no_agent(self) -> None:
        task = WorkerTask(name="t1", instruction="fix bug")
        self.assertIsNone(task.assigned_agent)

    def test_execute_worker_uses_agent_provider_and_model(self) -> None:
        registry = {
            "hex": {"provider": "openai", "model": "gpt-5.3-codex", "soul_text": "You are Hex.", "domains": [], "skills": []},
        }
        svc, router, _, _ = _make_service(agent_registry=registry)
        router.ask.return_value = MagicMock(content="fixed")
        task = WorkerTask(name="fix", instruction="fix the bug", assigned_agent="hex")
        result = svc._execute_worker(task)
        self.assertEqual(result.content, "fixed")
        call_kwargs = router.ask.call_args
        self.assertEqual(call_kwargs.kwargs.get("provider"), "openai")
        self.assertEqual(call_kwargs.kwargs.get("model"), "gpt-5.3-codex")
        self.assertEqual(call_kwargs.kwargs.get("system_prompt"), "You are Hex.")

    def test_execute_worker_without_agent_uses_defaults(self) -> None:
        registry = {"hex": {"provider": "openai", "model": "gpt-5.3-codex", "domains": [], "skills": []}}
        svc, router, _, _ = _make_service(agent_registry=registry)
        router.ask.return_value = MagicMock(content="ok")
        task = WorkerTask(name="t1", instruction="do something")
        svc._execute_worker(task)
        call_kwargs = router.ask.call_args
        self.assertNotIn("provider", call_kwargs.kwargs)
        self.assertNotIn("model", call_kwargs.kwargs)
        self.assertEqual(call_kwargs.kwargs["role"], "coordinator_research")
        self.assertEqual(call_kwargs.kwargs["timeout"], 90.0)

    def test_execute_worker_propagates_timeout_by_lane(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")

        for lane, expected_role, expected_timeout in (
            ("research", "coordinator_research", 90.0),
            ("worker", "coordinator_worker", 120.0),
            ("worker_heavy", "heavy_coding", 180.0),
            ("verifier", "coordinator_verification", 60.0),
        ):
            with self.subTest(lane=lane):
                router.ask.reset_mock()
                svc._execute_worker(WorkerTask(name=f"t-{lane}", instruction="do", lane=lane))
                call_kwargs = router.ask.call_args.kwargs
                self.assertEqual(call_kwargs["role"], expected_role)
                self.assertEqual(call_kwargs["timeout"], expected_timeout)

    def test_execute_worker_uses_task_timeout_override(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        task = WorkerTask(name="t1", instruction="do something", timeout_seconds=12.0)
        svc._execute_worker(task)
        self.assertEqual(router.ask.call_args.kwargs["timeout"], 12.0)

    def test_execute_worker_preserves_explicit_zero_timeout(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        task = WorkerTask(name="t1", instruction="do something", timeout_seconds=0.0)
        svc._execute_worker(task)
        self.assertEqual(router.ask.call_args.kwargs["timeout"], 0.0)

    def test_execute_worker_uses_lane_override_when_no_agent_assigned(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        task = WorkerTask(name="impl", instruction="write code", lane="worker")

        svc._execute_worker(
            task,
            lane_overrides={
                "worker": {
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "effort": "xhigh",
                }
            },
        )

        call_kwargs = router.ask.call_args.kwargs
        self.assertEqual(call_kwargs["provider"], "codex")
        self.assertEqual(call_kwargs["model"], "gpt-5.5")
        self.assertEqual(call_kwargs["effort"], "xhigh")

    def test_synthesis_preserves_explicit_zero_timeout_override(self) -> None:
        svc, router, *_ = _make_service()
        router.ask.return_value = MagicMock(content="plan")
        from claw_v2.coordinator import WorkerResult

        svc._synthesize(
            "objective",
            [WorkerResult(task_name="r1", content="data", duration_seconds=0.1)],
            lane_overrides={"research": {"timeout": 0.0}},
        )

        self.assertEqual(router.ask.call_args.kwargs["timeout"], 0.0)

    def test_synthesize_includes_agent_context(self) -> None:
        registry = {
            "hex": {"provider": "openai", "model": "gpt-5.3-codex", "domains": ["code"], "skills": ["bug-triage"]},
        }
        svc, router, _, _ = _make_service(agent_registry=registry)
        router.ask.return_value = MagicMock(content="plan here")
        from claw_v2.coordinator import WorkerResult
        findings = [WorkerResult(task_name="r1", content="found bug", duration_seconds=1.0)]
        result = svc._synthesize("fix bugs", findings)
        prompt_arg = router.ask.call_args.args[0]
        self.assertIn("hex", prompt_arg)
        self.assertIn("code", prompt_arg)


class RetryAndContextTests(unittest.TestCase):
    def test_worker_lane_retries_once_on_adapter_error(self) -> None:
        from claw_v2.adapters.base import AdapterError
        svc, router, observe, _ = _make_service()
        router.ask.side_effect = [AdapterError("Codex CLI timed out after 120s"), MagicMock(content="done")]
        task = WorkerTask(name="impl", instruction="build", lane="worker")
        result = svc._execute_worker(task)
        self.assertEqual(result.content, "done")
        self.assertEqual(result.error, "")
        self.assertEqual(router.ask.call_count, 2)
        retry_calls = [c for c in observe.emit.call_args_list if c.args and c.args[0] == "coordinator_worker_retry"]
        self.assertEqual(len(retry_calls), 1)

    def test_worker_heavy_lane_retries_once_on_adapter_error(self) -> None:
        from claw_v2.adapters.base import AdapterError
        svc, router, observe, _ = _make_service()
        router.ask.side_effect = [AdapterError("terminal failure"), MagicMock(content="done")]
        task = WorkerTask(name="debug", instruction="debug", lane="worker_heavy")
        result = svc._execute_worker(task)
        self.assertEqual(result.content, "done")
        self.assertEqual(router.ask.call_count, 2)
        retry_calls = [c for c in observe.emit.call_args_list if c.args and c.args[0] == "coordinator_worker_retry"]
        self.assertEqual(len(retry_calls), 1)

    def test_worker_lane_gives_up_after_two_attempts(self) -> None:
        from claw_v2.adapters.base import AdapterError
        svc, router, *_ = _make_service()
        router.ask.side_effect = AdapterError("persistent timeout")
        task = WorkerTask(name="impl", instruction="build", lane="worker")
        result = svc._execute_worker(task)
        self.assertEqual(result.content, "")
        self.assertIn("persistent timeout", result.error)
        self.assertEqual(router.ask.call_count, 2)

    def test_research_lane_does_not_retry(self) -> None:
        from claw_v2.adapters.base import AdapterError
        svc, router, *_ = _make_service()
        router.ask.side_effect = AdapterError("fail")
        task = WorkerTask(name="r1", instruction="find", lane="research")
        svc._execute_worker(task)
        self.assertEqual(router.ask.call_count, 1)

    def test_verification_receives_implementation_evidence(self) -> None:
        svc, router, *_ = _make_service()
        responses: list[Any] = []  # type: ignore[name-defined]

        from claw_v2.coordinator import WorkerResult
        def fake_ask(prompt, **_kwargs):
            responses.append(prompt)
            return MagicMock(content="plan" if "Synthesize" in prompt else "result")
        router.ask.side_effect = fake_ask

        research = [WorkerTask(name="r1", instruction="find")]
        impl = [WorkerTask(name="i1", instruction="build", lane="worker")]
        verify = [WorkerTask(name="v1", instruction="check", lane="verifier")]
        svc.run("t", "obj", research, impl, verify)

        verify_prompts = [p for p in responses if "## Tu Tarea Específica:" in p and "check" in p]
        self.assertEqual(len(verify_prompts), 1)
        self.assertIn("Artefacto de Referencia en Scratch", verify_prompts[0])
        self.assertIn("Estado Técnico Consolidado", verify_prompts[0])
        self.assertIn("i1: result", verify_prompts[0])

    def test_verification_surfaces_implementation_errors(self) -> None:
        from claw_v2.adapters.base import AdapterError
        svc, router, *_ = _make_service()
        captured: list[str] = []

        def fake_ask(prompt, **kwargs):
            lane = kwargs.get("lane")
            if lane == "worker":
                raise AdapterError("Codex CLI timed out after 120s")
            captured.append(prompt)
            return MagicMock(content="ok")
        router.ask.side_effect = fake_ask

        research = [WorkerTask(name="r1", instruction="find")]
        impl = [WorkerTask(name="i1", instruction="build", lane="worker")]
        verify = [WorkerTask(name="v1", instruction="check", lane="verifier")]
        svc.run("t", "obj", research, impl, verify)

        verify_prompts = [p for p in captured if "## Tu Tarea Específica:" in p and "check" in p]
        self.assertEqual(len(verify_prompts), 1)
        self.assertIn("Artefacto de Referencia en Scratch", verify_prompts[0])
        self.assertIn("i1: ERROR: Codex CLI timed out after 120s", verify_prompts[0])


if __name__ == "__main__":
    unittest.main()
