from __future__ import annotations

import json
import tempfile
import unittest
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

    def test_degraded_compaction_warning_is_visible_in_prompt(self) -> None:
        # F3.3 (2026-06-12): degraded_compaction was an internal flag only —
        # the next phase consumed mechanically-cut context unknowingly.
        tasks = [WorkerTask(name="impl1", instruction="write code")]
        degraded = CoordinatorService._inject_context(
            tasks,
            objective="ship feature",
            input_artifact_ref="art:abc123",
            input_summary="the plan",
            degraded=True,
        )
        self.assertIn("Advertencia de Contexto", degraded[0].instruction)
        clean = CoordinatorService._inject_context(
            tasks,
            objective="ship feature",
            input_artifact_ref="art:abc123",
            input_summary="the plan",
        )
        self.assertNotIn("Advertencia de Contexto", clean[0].instruction)

    def test_compact_text_marker_reports_kept_and_total(self) -> None:
        # F3.2 (2026-06-12): standard truncation marker.
        from claw_v2.coordinator import _compact_text

        text = "palabra " * 5_000
        clean_len = len(" ".join(text.split()))
        out = _compact_text(text, limit=1_000)
        self.assertLessEqual(len(out), 1_000)
        self.assertIn(f"[truncated: kept 1000 of {clean_len} chars]", out)
        self.assertEqual(_compact_text("corto", limit=1_000), "corto")


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
        _result = svc._synthesize("fix bugs", findings)
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


class ResumeFromScratchTests(unittest.TestCase):
    """F3.1 — kill+resume must not re-execute completed phases."""

    def test_detect_resume_phase_progression(self) -> None:
        svc, _, _, tmp = _make_service()
        self.assertEqual(svc.detect_resume_phase("missing-task"), "research")
        scratch = tmp / "t-detect"
        (scratch / "research").mkdir(parents=True)
        (scratch / "research" / "r1.json").write_text(
            json.dumps({"task_name": "r1", "content": "hallazgo"}), encoding="utf-8"
        )
        self.assertEqual(svc.detect_resume_phase("t-detect"), "synthesis")
        (scratch / "synthesis.md").write_text("plan", encoding="utf-8")
        self.assertEqual(svc.detect_resume_phase("t-detect"), "implementation")
        (scratch / "implementation").mkdir()
        (scratch / "implementation" / "i1.json").write_text(
            json.dumps({"task_name": "i1", "content": "hecho"}), encoding="utf-8"
        )
        self.assertEqual(svc.detect_resume_phase("t-detect"), "verification")

    def test_resume_does_not_reexecute_research_or_synthesis(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        svc, router, _, _ = _make_service(scratch_root=tmp)
        router.ask.return_value = MagicMock(content="hallazgo de research")
        research = [WorkerTask(name="r1", instruction="investiga")]
        # First attempt completes research + synthesis and is then "killed"
        # (research-only run leaves exactly those artifacts in scratch).
        svc.run("task-resume", "objetivo", research)

        svc2, router2, observe2, _ = _make_service(scratch_root=tmp)
        router2.ask.return_value = MagicMock(content="Verification Status: passed")
        start_phase = svc2.detect_resume_phase("task-resume")
        self.assertEqual(start_phase, "implementation")
        impl = [WorkerTask(name="i1", instruction="implementa", lane="worker")]
        verify = [WorkerTask(name="v1", instruction="verifica", lane="verifier")]
        result = svc2.run(
            "task-resume",
            "objetivo",
            research,
            implementation_tasks=impl,
            verification_tasks=verify,
            start_phase=start_phase,
        )

        lanes = [call.kwargs.get("lane") for call in router2.ask.call_args_list]
        self.assertNotIn("research", lanes, "research/synthesis must load from scratch")
        self.assertEqual(sorted(lanes), ["verifier", "worker"])
        self.assertEqual(result.phase_results["research"][0].content, "hallazgo de research")
        self.assertEqual(result.synthesis, "hallazgo de research")
        self.assertEqual(result.error, "")
        event_names = [call.args[0] for call in observe2.emit.call_args_list]
        self.assertIn("coordinator_phase_resumed_from_scratch", event_names)

    def test_resumed_run_blocks_implementation_rerun_after_partial_attempt(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        svc, router, _, _ = _make_service(scratch_root=tmp)
        router.ask.return_value = MagicMock(content="ok")
        research = [WorkerTask(name="r1", instruction="investiga")]
        svc.run("task-gate", "objetivo", research)
        # Simulate a previous attempt that STARTED implementation but died
        # before persisting completed results: side effects are possible.
        (tmp / "task-gate" / "implementation.started").write_text("{}", encoding="utf-8")

        svc2, router2, observe2, _ = _make_service(scratch_root=tmp)
        router2.ask.return_value = MagicMock(content="ok")
        impl = [WorkerTask(name="i1", instruction="implementa", lane="worker")]
        result = svc2.run(
            "task-gate",
            "objetivo",
            research,
            implementation_tasks=impl,
            start_phase="implementation",
        )
        self.assertEqual(result.error, "implementation_rerun_blocked")
        lanes = [call.kwargs.get("lane") for call in router2.ask.call_args_list]
        self.assertNotIn("worker", lanes, "implementation must not silently re-run")
        event_names = [call.args[0] for call in observe2.emit.call_args_list]
        self.assertIn("coordinator_implementation_rerun_blocked", event_names)

        # Explicit override is the only path to a re-run.
        svc3, router3, _, _ = _make_service(scratch_root=tmp)
        router3.ask.return_value = MagicMock(content="ok")
        result = svc3.run(
            "task-gate",
            "objetivo",
            research,
            implementation_tasks=impl,
            start_phase="implementation",
            allow_implementation_rerun=True,
        )
        self.assertEqual(result.error, "")
        self.assertIn("implementation", result.phase_results)

    def test_fresh_run_is_not_blocked_by_its_own_marker(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        research = [WorkerTask(name="r1", instruction="investiga")]
        impl = [WorkerTask(name="i1", instruction="implementa", lane="worker")]
        result = svc.run("task-fresh", "objetivo", research, implementation_tasks=impl)
        self.assertEqual(result.error, "")
        self.assertIn("implementation", result.phase_results)

    def test_invalid_start_phase_rejected(self) -> None:
        svc, *_ = _make_service()
        with self.assertRaises(ValueError):
            svc.run("t", "obj", [], start_phase="deploy")

    def test_prune_stale_scratch_dirs_respects_retention(self) -> None:
        import os
        import time as time_mod

        svc, router, _, tmp = _make_service(scratch_retention_days=1.0)
        old_dir = tmp / "old-task"
        old_dir.mkdir(parents=True)
        stale = time_mod.time() - 3 * 86_400
        os.utime(old_dir, (stale, stale))
        fresh_dir = tmp / "fresh-task"
        fresh_dir.mkdir()
        router.ask.return_value = MagicMock(content="ok")
        svc.run("task-prune", "objetivo", [WorkerTask(name="r1", instruction="x")])
        self.assertFalse(old_dir.exists(), "stale scratch dir must be pruned")
        self.assertTrue(fresh_dir.exists(), "fresh scratch dir must be kept")


class ResumeCriticalWorkerTests(unittest.TestCase):
    """PR #96 review (codex): a resume must fail closed on a critical
    artifact persisted by a killed attempt, never proceed past it."""

    def _seed_research(self, tmp: Path, task_id: str, *, content: str) -> None:
        research_dir = tmp / task_id / "research"
        research_dir.mkdir(parents=True)
        (research_dir / "r1.json").write_text(
            json.dumps({"task_name": "r1", "content": content}), encoding="utf-8"
        )

    def test_loaded_research_with_critical_marker_runs_self_healing(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self._seed_research(
            tmp, "task-crit", content="CRITICAL ERROR EN WORKER: entorno roto"
        )
        svc, router, observe, _ = _make_service(scratch_root=tmp)
        router.ask.return_value = MagicMock(content="diagnóstico")
        impl = [WorkerTask(name="i1", instruction="implementa", lane="worker")]
        result = svc.run(
            "task-crit",
            "objetivo",
            [WorkerTask(name="r1", instruction="investiga")],
            implementation_tasks=impl,
            start_phase="synthesis",
        )
        self.assertTrue(result.error.startswith("critical_worker_error:"), result.error)
        self.assertNotIn("implementation", result.phase_results)
        lanes = [call.kwargs.get("lane") for call in router.ask.call_args_list]
        self.assertNotIn("worker", lanes, "implementation must never run after a critical artifact")
        event_names = [call.args[0] for call in observe.emit.call_args_list]
        self.assertIn("coordinator_critical_worker_error", event_names)

    def test_loaded_implementation_with_critical_marker_fails_closed(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self._seed_research(tmp, "task-crit2", content="hallazgo limpio")
        (tmp / "task-crit2" / "synthesis.md").write_text("plan", encoding="utf-8")
        impl_dir = tmp / "task-crit2" / "implementation"
        impl_dir.mkdir()
        (impl_dir / "i1.json").write_text(
            json.dumps(
                {"task_name": "i1", "content": "CRITICAL ERROR EN WORKER: deploy roto"}
            ),
            encoding="utf-8",
        )
        svc, router, observe, _ = _make_service(scratch_root=tmp)
        router.ask.return_value = MagicMock(content="diagnóstico")
        result = svc.run(
            "task-crit2",
            "objetivo",
            [WorkerTask(name="r1", instruction="investiga")],
            implementation_tasks=[WorkerTask(name="i1", instruction="implementa", lane="worker")],
            verification_tasks=[WorkerTask(name="v1", instruction="verifica", lane="verifier")],
            start_phase="verification",
        )
        self.assertTrue(result.error.startswith("critical_worker_error:"), result.error)
        self.assertNotIn("verification", result.phase_results)
        lanes = [call.kwargs.get("lane") for call in router.ask.call_args_list]
        self.assertNotIn("verifier", lanes, "verification must never run after a critical artifact")
        self.assertNotIn("worker", lanes)


class CancelAtPhaseBoundaryTests(unittest.TestCase):
    """AM-CANCEL — should_abort is honored between phases."""

    def test_abort_after_research_skips_synthesis(self) -> None:
        svc, router, observe, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")
        research = [WorkerTask(name="r1", instruction="investiga")]
        impl = [WorkerTask(name="i1", instruction="implementa", lane="worker")]
        result = svc.run(
            "task-cancel",
            "objetivo",
            research,
            implementation_tasks=impl,
            should_abort=lambda: True,
        )
        self.assertEqual(result.error, "cancelled_at_phase_boundary:synthesis")
        self.assertEqual(list(result.phase_results.keys()), ["research"])
        self.assertEqual(router.ask.call_count, 1, "only the research worker may run")
        event_names = [call.args[0] for call in observe.emit.call_args_list]
        self.assertIn("coordinator_cancelled", event_names)

    def test_abort_callback_failure_does_not_cancel(self) -> None:
        svc, router, _, _ = _make_service()
        router.ask.return_value = MagicMock(content="ok")

        def broken() -> bool:
            raise RuntimeError("callback exploded")

        result = svc.run(
            "task-cb",
            "objetivo",
            [WorkerTask(name="r1", instruction="x")],
            should_abort=broken,
        )
        self.assertEqual(result.error, "")


class EmptySynthesisDegradationTests(unittest.TestCase):
    """AM-SYNTH — an empty synthesis must degrade visibly, not in silence."""

    def test_empty_synthesis_marks_audit_event_and_context_warning(self) -> None:
        svc, router, observe, _ = _make_service()

        def ask(prompt: str, **kwargs: Any) -> MagicMock:
            evidence = kwargs.get("evidence_pack") or {}
            if evidence.get("coordinator_phase") == "synthesis":
                raise RuntimeError("synthesis lane down")
            return MagicMock(content="ok")

        router.ask.side_effect = ask
        research = [WorkerTask(name="r1", instruction="investiga")]
        impl = [WorkerTask(name="i1", instruction="implementa", lane="worker")]
        result = svc.run("task-synth", "objetivo", research, implementation_tasks=impl)

        self.assertTrue(result.audit.get("synthesis_empty"))
        event_names = [call.args[0] for call in observe.emit.call_args_list]
        self.assertIn("coordinator_synthesis_empty", event_names)
        impl_call = next(
            call for call in router.ask.call_args_list if call.kwargs.get("lane") == "worker"
        )
        self.assertIn("Advertencia de Contexto", impl_call.args[0])


class ParallelDistillationTests(unittest.TestCase):
    """AM-DISTILL — per-worker-result distillation runs concurrently."""

    def test_distillation_calls_overlap(self) -> None:
        import threading

        from claw_v2.coordinator import WorkerResult

        svc, router, _, _ = _make_service(worker_result_summary_chars=10, max_workers=2)
        barrier = threading.Barrier(2, timeout=10)

        def ask(prompt: str, **kwargs: Any) -> MagicMock:
            evidence = kwargs.get("evidence_pack") or {}
            if evidence.get("coordinator_phase") == "semantic_distillation":
                # Deadlocks (and falls back to mechanical compaction) if the
                # two distillation calls run serially.
                barrier.wait()
                return MagicMock(content="corto")
            return MagicMock(content="x" * 200)

        router.ask.side_effect = ask
        results = [
            WorkerResult(task_name="a", content="y" * 200, duration_seconds=0.1),
            WorkerResult(task_name="b", content="z" * 200, duration_seconds=0.1),
        ]
        summary = svc._phase_results_summary(results, limit=10_000)
        self.assertIn("- a: corto", summary)
        self.assertIn("- b: corto", summary)
        self.assertFalse(results[0].degraded_compaction)
        self.assertFalse(results[1].degraded_compaction)


if __name__ == "__main__":
    unittest.main()
