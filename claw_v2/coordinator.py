from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.adapters.base import AdapterError
from claw_v2.tracing import attach_trace, child_trace_context, new_trace_context

logger = logging.getLogger(__name__)

WORKER_RESULT_SUMMARY_CHARS = 900
PHASE_INPUT_SUMMARY_CHARS = 1_500


@dataclass(slots=True)
class WorkerTask:
    """A single unit of work dispatched to a worker."""

    name: str
    instruction: str
    lane: str = "research"
    assigned_agent: str | None = None


@dataclass(slots=True)
class WorkerResult:
    """Result from a single worker execution."""

    task_name: str
    content: str
    duration_seconds: float
    error: str = ""


@dataclass(slots=True)
class CoordinatorResult:
    """Outcome of a full coordinator run."""

    task_id: str
    phase_results: dict[str, list[WorkerResult]] = field(default_factory=dict)
    synthesis: str = ""
    duration_seconds: float = 0.0
    error: str = ""


class CoordinatorService:
    """Orchestrates multi-phase parallel work: research -> synthesis -> implementation -> verification.

    Workers run in parallel via a thread pool (router.ask is synchronous).
    A shared scratch directory lets workers exchange findings between phases.
    """

    def __init__(
        self,
        *,
        router: Any,
        observe: Any,
        scratch_root: Path | str = Path.home() / ".claw" / "scratch",
        max_workers: int = 4,
        agent_registry: dict | None = None,
        orchestration_store: Any | None = None,
    ) -> None:
        self.router = router
        self.observe = observe
        self.scratch_root = Path(scratch_root)
        self.max_workers = max_workers
        self.agent_registry = agent_registry or {}
        self.orchestration_store = orchestration_store

    def run(
        self,
        task_id: str,
        objective: str,
        research_tasks: list[WorkerTask],
        implementation_tasks: list[WorkerTask] | None = None,
        verification_tasks: list[WorkerTask] | None = None,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> CoordinatorResult:
        """Execute the full coordinator cycle.

        1. Research   — parallel workers gather information
        2. Synthesis  — coordinator merges findings into a plan
        3. Implementation — parallel workers execute the plan (optional)
        4. Verification   — parallel workers validate results (optional)
        """
        start = time.time()
        scratch = self._ensure_scratch(task_id)
        result = CoordinatorResult(task_id=task_id)
        trace = new_trace_context(job_id=task_id, artifact_id=task_id)
        orchestration_run_id = task_id

        try:
            self._orchestration_begin_run(
                run_id=orchestration_run_id,
                task_id=task_id,
                objective=objective,
                trace_context=trace,
                lane_overrides=lane_overrides,
            )
            self.observe.emit(
                "coordinator_start",
                trace_id=trace["trace_id"],
                root_trace_id=trace["root_trace_id"],
                span_id=trace["span_id"],
                parent_span_id=trace["parent_span_id"],
                job_id=trace["job_id"],
                artifact_id=trace["artifact_id"],
                payload={"task_id": task_id, "objective": objective},
            )
            # Phase 1: Research
            self._orchestration_begin_phase(orchestration_run_id, "research", trace)
            research_results = self._dispatch_parallel(research_tasks, trace, lane_overrides=lane_overrides)
            result.phase_results["research"] = research_results
            self._write_scratch(scratch, "research", research_results)
            research_artifact_id = self._orchestration_record_phase_results(
                orchestration_run_id,
                phase="research",
                results=research_results,
                producer_role="research_workers",
                consumer_role="coordinator_synthesis",
                trace_context=trace,
            )
            self._orchestration_ack(research_artifact_id, consumer_role="coordinator_synthesis")
            self._orchestration_require_ack(research_artifact_id, consumer_role="coordinator_synthesis")
            self._orchestration_checkpoint(
                orchestration_run_id,
                phase="research",
                reason="research_phase_completed",
                artifact_ids=[research_artifact_id] if research_artifact_id else [],
            )
            self._orchestration_finish_phase(
                orchestration_run_id,
                "research",
                trace,
                results=research_results,
            )

            # Phase 2: Synthesis
            self._orchestration_begin_phase(orchestration_run_id, "synthesis", trace)
            synthesis = self._synthesize(objective, research_results, trace, lane_overrides=lane_overrides)
            result.synthesis = synthesis
            self._write_scratch_text(scratch, "synthesis.md", synthesis)
            if implementation_tasks:
                synthesis_consumer = "implementation_workers"
            elif verification_tasks:
                synthesis_consumer = "verification_workers"
            else:
                synthesis_consumer = "coordinator_result"
            synthesis_artifact_id = self._orchestration_record_text_artifact(
                orchestration_run_id,
                phase="synthesis",
                artifact_type="synthesis",
                content=synthesis,
                producer_role="coordinator_synthesis",
                consumer_role=synthesis_consumer,
                trace_context=trace,
            )
            self._orchestration_ack(synthesis_artifact_id, consumer_role=synthesis_consumer)
            self._orchestration_require_ack(synthesis_artifact_id, consumer_role=synthesis_consumer)
            synthesis_artifact_ref = synthesis_artifact_id or str(scratch / "synthesis.md")
            synthesis_summary = _compact_text(synthesis, limit=PHASE_INPUT_SUMMARY_CHARS)
            self._orchestration_checkpoint(
                orchestration_run_id,
                phase="synthesis",
                reason="synthesis_phase_completed",
                artifact_ids=[synthesis_artifact_id] if synthesis_artifact_id else [],
            )
            self._orchestration_finish_phase(
                orchestration_run_id,
                "synthesis",
                trace,
                payload={"content_length": len(synthesis or "")},
            )

            # Phase 3: Implementation (optional)
            impl_results: list[WorkerResult] = []
            impl_artifact_id: str | None = None
            if implementation_tasks:
                self._orchestration_begin_phase(orchestration_run_id, "implementation", trace)
                impl_tasks = self._inject_context(
                    implementation_tasks,
                    objective=objective,
                    input_artifact_ref=synthesis_artifact_ref,
                    input_summary=synthesis_summary,
                )
                impl_results = self._dispatch_parallel(impl_tasks, trace, lane_overrides=lane_overrides)
                result.phase_results["implementation"] = impl_results
                self._write_scratch(scratch, "implementation", impl_results)
                impl_artifact_id = self._orchestration_record_phase_results(
                    orchestration_run_id,
                    phase="implementation",
                    results=impl_results,
                    producer_role="implementation_workers",
                    consumer_role="verification_workers",
                    trace_context=trace,
                )
                self._orchestration_ack(impl_artifact_id, consumer_role="verification_workers")
                self._orchestration_require_ack(impl_artifact_id, consumer_role="verification_workers")
                self._orchestration_checkpoint(
                    orchestration_run_id,
                    phase="implementation",
                    reason="implementation_phase_completed",
                    artifact_ids=[impl_artifact_id] if impl_artifact_id else [],
                )
                self._orchestration_finish_phase(
                    orchestration_run_id,
                    "implementation",
                    trace,
                    results=impl_results,
                )

            # Phase 4: Verification (optional)
            if verification_tasks:
                self._orchestration_begin_phase(orchestration_run_id, "verification", trace)
                verification_input_ref = synthesis_artifact_ref
                verification_input_summary = synthesis_summary
                if impl_results:
                    verification_input_ref = impl_artifact_id or str(scratch / "implementation")
                    verification_input_summary = _phase_results_summary(
                        impl_results,
                        limit=PHASE_INPUT_SUMMARY_CHARS,
                    )
                verify_tasks = self._inject_context(
                    verification_tasks,
                    objective=objective,
                    input_artifact_ref=verification_input_ref,
                    input_summary=verification_input_summary,
                )
                verify_results = self._dispatch_parallel(verify_tasks, trace, lane_overrides=lane_overrides)
                result.phase_results["verification"] = verify_results
                self._write_scratch(scratch, "verification", verify_results)
                verification_artifact_id = self._orchestration_record_phase_results(
                    orchestration_run_id,
                    phase="verification",
                    results=verify_results,
                    producer_role="verification_workers",
                    consumer_role="coordinator_result",
                    trace_context=trace,
                )
                self._orchestration_ack(verification_artifact_id, consumer_role="coordinator_result")
                self._orchestration_require_ack(verification_artifact_id, consumer_role="coordinator_result")
                self._orchestration_checkpoint(
                    orchestration_run_id,
                    phase="verification",
                    reason="verification_phase_completed",
                    artifact_ids=[verification_artifact_id] if verification_artifact_id else [],
                )
                self._orchestration_finish_phase(
                    orchestration_run_id,
                    "verification",
                    trace,
                    results=verify_results,
                )

            result.duration_seconds = time.time() - start
            self._orchestration_complete_run(
                orchestration_run_id,
                status="succeeded",
                reason="coordinator_complete",
                trace_context=trace,
            )
            self.observe.emit(
                "coordinator_complete",
                trace_id=trace["trace_id"],
                root_trace_id=trace["root_trace_id"],
                span_id=trace["span_id"],
                parent_span_id=trace["parent_span_id"],
                job_id=trace["job_id"],
                artifact_id=trace["artifact_id"],
                payload={
                    "task_id": task_id,
                    "phases": list(result.phase_results.keys()),
                    "duration": result.duration_seconds,
                    "workers_total": sum(len(v) for v in result.phase_results.values()),
                },
            )
            return result

        except Exception as exc:
            logger.exception("Coordinator run failed for task %s", task_id)
            result.error = str(exc)
            result.duration_seconds = time.time() - start
            self._orchestration_complete_run(
                orchestration_run_id,
                status="failed",
                reason=str(exc)[:300],
                trace_context=trace,
            )
            return result

    def _dispatch_parallel(
        self,
        tasks: list[WorkerTask],
        trace_context: dict[str, Any] | None = None,
        *,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> list[WorkerResult]:
        """Run multiple worker tasks in parallel using a thread pool."""
        if not tasks:
            return []

        results: list[WorkerResult] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks))) as pool:
            futures = {
                pool.submit(self._execute_worker, task, trace_context, lane_overrides=lane_overrides): task
                for task in tasks
            }
            for future in as_completed(futures):
                task = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(WorkerResult(
                        task_name=task.name,
                        content="",
                        duration_seconds=0.0,
                        error=str(exc),
                    ))
        return results

    _RETRY_LANES: frozenset[str] = frozenset({"worker", "worker_heavy"})

    def _execute_worker(
        self,
        task: WorkerTask,
        trace_context: dict[str, Any] | None = None,
        *,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> WorkerResult:
        """Execute a single worker task via the LLM router."""
        start = time.time()
        task_trace = child_trace_context(trace_context, artifact_id=task.name)
        kwargs: dict[str, Any] = {
            "lane": task.lane,
            "evidence_pack": attach_trace({"coordinator_task": task.name}, task_trace),
        }
        if task.assigned_agent and task.assigned_agent in self.agent_registry:
            agent = self.agent_registry[task.assigned_agent]
            kwargs["provider"] = agent["provider"]
            kwargs["model"] = agent["model"]
            kwargs["system_prompt"] = agent.get("soul_text", "")
        elif lane_overrides and task.lane in lane_overrides:
            override = lane_overrides[task.lane]
            kwargs["provider"] = override.get("provider")
            kwargs["model"] = override.get("model")
            if override.get("effort"):
                kwargs["effort"] = override.get("effort")
        attempts = 2 if task.lane in self._RETRY_LANES else 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.router.ask(task.instruction, **kwargs)
                return WorkerResult(
                    task_name=task.name,
                    content=response.content,
                    duration_seconds=time.time() - start,
                )
            except AdapterError as exc:
                last_exc = exc
                if attempt < attempts:
                    self.observe.emit(
                        "coordinator_worker_retry",
                        trace_id=task_trace["trace_id"],
                        root_trace_id=task_trace["root_trace_id"],
                        span_id=task_trace["span_id"],
                        parent_span_id=task_trace["parent_span_id"],
                        artifact_id=task.name,
                        payload={"task_name": task.name, "lane": task.lane, "error": str(exc)[:300], "attempt": attempt},
                    )
                    continue
                break
            except Exception as exc:
                last_exc = exc
                break
        return WorkerResult(
            task_name=task.name,
            content="",
            duration_seconds=time.time() - start,
            error=str(last_exc) if last_exc else "unknown_error",
        )

    def _synthesize(
        self,
        objective: str,
        research_results: list[WorkerResult],
        trace_context: dict[str, Any] | None = None,
        *,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """Merge research findings into a coherent plan."""
        findings = _phase_results_summary(
            research_results,
            limit=PHASE_INPUT_SUMMARY_CHARS,
        )

        agent_context = ""
        if self.agent_registry:
            agent_lines = "\n".join(
                f"- {name}: domains={caps.get('domains', [])}, model={caps.get('model', '?')}, skills={caps.get('skills', [])}"
                for name, caps in self.agent_registry.items()
            )
            agent_context = f"\n\n## Available Agents\n{agent_lines}"

        prompt = (
            "You are a coordinator agent. Synthesize the research findings below "
            "into a clear, actionable plan. The findings are compact summaries; "
            "do not assume omitted details are verified facts.\n\n"
            f"## Objective\n{objective}{agent_context}\n\n"
            f"## Research Result Summaries\n{findings}\n\n"
            "Output a structured plan with numbered steps. "
            "For each step, assign it to the most appropriate agent based on their domains and skills. "
            "Use the format: **Step N [agent_name]:** description"
        )

        try:
            synthesis_trace = child_trace_context(trace_context, artifact_id="coordinator_synthesis")
            response = self.router.ask(
                prompt,
                lane="research",
                provider=(lane_overrides or {}).get("research", {}).get("provider"),
                model=(lane_overrides or {}).get("research", {}).get("model"),
                effort=(lane_overrides or {}).get("research", {}).get("effort"),
                evidence_pack=attach_trace(
                    {"coordinator_phase": "synthesis", "objective": objective},
                    synthesis_trace,
                ),
            )
            return response.content
        except Exception:
            logger.exception("Coordinator synthesis failed")
            return ""

    @staticmethod
    def _inject_context(
        tasks: list[WorkerTask],
        *,
        objective: str,
        input_artifact_ref: str | None,
        input_summary: str,
    ) -> list[WorkerTask]:
        """Prepend compact artifact-mediated context to each task's instruction."""
        ref = input_artifact_ref or "none"
        summary = _compact_text(input_summary, limit=PHASE_INPUT_SUMMARY_CHARS)
        return [
            WorkerTask(
                name=t.name,
                instruction=(
                    "## Task Context\n"
                    f"objective: {objective}\n"
                    f"input_artifact_ref: {ref}\n"
                    f"input_summary: {summary or 'none'}\n\n"
                    "## Your task\n"
                    f"{t.instruction}"
                ),
                lane=t.lane,
                assigned_agent=t.assigned_agent,
            )
            for t in tasks
        ]

    def _ensure_scratch(self, task_id: str) -> Path:
        """Create and return the scratch directory for a task."""
        scratch = self.scratch_root / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def _orchestration_begin_run(
        self,
        *,
        run_id: str,
        task_id: str,
        objective: str,
        trace_context: dict[str, Any],
        lane_overrides: dict[str, dict[str, Any]] | None,
    ) -> None:
        if self.orchestration_store is None:
            return
        self.orchestration_store.begin_run(
            run_id=run_id,
            task_id=task_id,
            objective=objective,
            kind="coordinator",
            metadata={"lane_overrides": lane_overrides or {}},
            trace_context=trace_context,
        )

    def _orchestration_begin_phase(
        self,
        run_id: str,
        phase: str,
        trace_context: dict[str, Any],
    ) -> None:
        if self.orchestration_store is None:
            return
        self.orchestration_store.begin_phase(run_id, phase, trace_context=trace_context)

    def _orchestration_finish_phase(
        self,
        run_id: str,
        phase: str,
        trace_context: dict[str, Any],
        *,
        results: list[WorkerResult] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.orchestration_store is None:
            return
        phase_payload = dict(payload or {})
        if results is not None:
            phase_payload.update(_phase_counts(results))
        self.orchestration_store.finish_phase(
            run_id,
            phase,
            status="succeeded",
            trace_context=trace_context,
            payload=phase_payload,
        )

    def _orchestration_record_phase_results(
        self,
        run_id: str,
        *,
        phase: str,
        results: list[WorkerResult],
        producer_role: str,
        consumer_role: str,
        trace_context: dict[str, Any],
    ) -> str | None:
        if self.orchestration_store is None:
            return None
        artifact = self.orchestration_store.record_artifact(
            run_id,
            phase=phase,
            artifact_type="worker_results",
            payload={
                "results": [_worker_result_payload(item) for item in results],
                **_phase_counts(results),
            },
            producer_role=producer_role,
            consumer_role=consumer_role,
            trace_context=trace_context,
        )
        return artifact.artifact_id

    def _orchestration_record_text_artifact(
        self,
        run_id: str,
        *,
        phase: str,
        artifact_type: str,
        content: str,
        producer_role: str,
        consumer_role: str,
        trace_context: dict[str, Any],
    ) -> str | None:
        if self.orchestration_store is None:
            return None
        artifact = self.orchestration_store.record_artifact(
            run_id,
            phase=phase,
            artifact_type=artifact_type,
            payload={"content": content or "", "content_length": len(content or "")},
            producer_role=producer_role,
            consumer_role=consumer_role,
            trace_context=trace_context,
        )
        return artifact.artifact_id

    def _orchestration_ack(self, artifact_id: str | None, *, consumer_role: str) -> None:
        if self.orchestration_store is None or not artifact_id:
            return
        self.orchestration_store.acknowledge_artifact(
            artifact_id,
            consumer_role=consumer_role,
        )

    def _orchestration_require_ack(self, artifact_id: str | None, *, consumer_role: str) -> None:
        if self.orchestration_store is None or not artifact_id:
            return
        self.orchestration_store.require_ack_received(
            artifact_id,
            consumer_role=consumer_role,
        )

    def _orchestration_checkpoint(
        self,
        run_id: str,
        *,
        phase: str,
        reason: str,
        artifact_ids: list[str],
    ) -> None:
        if self.orchestration_store is None:
            return
        self.orchestration_store.checkpoint(
            run_id,
            phase=phase,
            reason=reason,
            artifact_ids=artifact_ids,
        )

    def _orchestration_complete_run(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        trace_context: dict[str, Any],
    ) -> None:
        if self.orchestration_store is None:
            return
        existing = self.orchestration_store.get_run(run_id)
        if status == "failed" and existing is not None and existing.status in {"alarm", "blocked"}:
            return
        try:
            self.orchestration_store.complete_run(
                run_id,
                status=status,
                reason=reason,
                trace_context=trace_context,
            )
        except KeyError:
            return

    def _write_scratch(self, scratch: Path, phase: str, results: list[WorkerResult]) -> None:
        """Write worker results to scratch directory as JSON."""
        phase_dir = scratch / phase
        phase_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            data = {
                "task_name": r.task_name,
                "content": r.content,
                "duration_seconds": r.duration_seconds,
                "error": r.error,
            }
            (phase_dir / f"{r.task_name}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )

    @staticmethod
    def _write_scratch_text(scratch: Path, filename: str, content: str) -> None:
        """Write a text file to the scratch directory."""
        (scratch / filename).write_text(content, encoding="utf-8")


def _worker_result_payload(result: WorkerResult) -> dict[str, Any]:
    return {
        "task_name": result.task_name,
        "content": result.content,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
    }


def _phase_results_summary(results: list[WorkerResult], *, limit: int) -> str:
    if not results:
        return "none"
    lines: list[str] = []
    for result in results:
        lines.append(
            f"- {result.task_name}: "
            f"{_worker_result_summary(result, limit=WORKER_RESULT_SUMMARY_CHARS)}"
        )
    return _compact_text("\n".join(lines), limit=limit)


def _worker_result_summary(result: WorkerResult, *, limit: int) -> str:
    if result.error:
        return _compact_text(f"ERROR: {result.error}", limit=limit)
    if not result.content:
        return "empty result"
    return _compact_text(result.content, limit=limit)


def _compact_text(text: str, *, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    suffix = "... [truncated]"
    return clean[: max(limit - len(suffix), 0)].rstrip() + suffix


def _phase_counts(results: list[WorkerResult]) -> dict[str, Any]:
    error_count = sum(1 for item in results if item.error)
    return {
        "worker_count": len(results),
        "error_count": error_count,
        "ok_count": len(results) - error_count,
    }
