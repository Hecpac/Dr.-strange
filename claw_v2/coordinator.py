from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.tracing import attach_trace, child_trace_context, new_trace_context

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.router = router
        self.observe = observe
        self.scratch_root = Path(scratch_root)
        self.max_workers = max_workers
        self.agent_registry = agent_registry or {}

    def run(
        self,
        task_id: str,
        objective: str,
        research_tasks: list[WorkerTask],
        implementation_tasks: list[WorkerTask] | None = None,
        verification_tasks: list[WorkerTask] | None = None,
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

        try:
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
            research_results = self._dispatch_parallel(research_tasks, trace)
            result.phase_results["research"] = research_results
            self._write_scratch(scratch, "research", research_results)

            # Phase 2: Synthesis
            synthesis = self._synthesize(objective, research_results, trace)
            result.synthesis = synthesis
            self._write_scratch_text(scratch, "synthesis.md", synthesis)

            # Phase 3: Implementation (optional)
            if implementation_tasks:
                impl_tasks = self._inject_context(implementation_tasks, synthesis)
                impl_results = self._dispatch_parallel(impl_tasks, trace)
                result.phase_results["implementation"] = impl_results
                self._write_scratch(scratch, "implementation", impl_results)

            # Phase 4: Verification (optional)
            if verification_tasks:
                verify_tasks = self._inject_context(verification_tasks, synthesis)
                verify_results = self._dispatch_parallel(verify_tasks, trace)
                result.phase_results["verification"] = verify_results
                self._write_scratch(scratch, "verification", verify_results)

            result.duration_seconds = time.time() - start
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
            return result

    def _dispatch_parallel(self, tasks: list[WorkerTask], trace_context: dict[str, Any] | None = None) -> list[WorkerResult]:
        """Run multiple worker tasks in parallel using a thread pool."""
        if not tasks:
            return []

        results: list[WorkerResult] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks))) as pool:
            futures = {
                pool.submit(self._execute_worker, task, trace_context): task
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

    def _execute_worker(self, task: WorkerTask, trace_context: dict[str, Any] | None = None) -> WorkerResult:
        """Execute a single worker task via the LLM router."""
        start = time.time()
        try:
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
            response = self.router.ask(task.instruction, **kwargs)
            return WorkerResult(
                task_name=task.name,
                content=response.content,
                duration_seconds=time.time() - start,
            )
        except Exception as exc:
            return WorkerResult(
                task_name=task.name,
                content="",
                duration_seconds=time.time() - start,
                error=str(exc),
            )

    def _synthesize(self, objective: str, research_results: list[WorkerResult], trace_context: dict[str, Any] | None = None) -> str:
        """Merge research findings into a coherent plan."""
        findings = "\n\n".join(
            f"### {r.task_name}\n{r.content}" if not r.error
            else f"### {r.task_name}\n[ERROR: {r.error}]"
            for r in research_results
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
            "into a clear, actionable plan.\n\n"
            f"## Objective\n{objective}{agent_context}\n\n"
            f"## Research Findings\n{findings}\n\n"
            "Output a structured plan with numbered steps. "
            "For each step, assign it to the most appropriate agent based on their domains and skills. "
            "Use the format: **Step N [agent_name]:** description"
        )

        try:
            synthesis_trace = child_trace_context(trace_context, artifact_id="coordinator_synthesis")
            response = self.router.ask(
                prompt,
                lane="research",
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
    def _inject_context(tasks: list[WorkerTask], synthesis: str) -> list[WorkerTask]:
        """Prepend synthesis context to each task's instruction."""
        return [
            WorkerTask(
                name=t.name,
                instruction=f"## Context from coordinator\n{synthesis}\n\n## Your task\n{t.instruction}",
                lane=t.lane,
            )
            for t in tasks
        ]

    def _ensure_scratch(self, task_id: str) -> Path:
        """Create and return the scratch directory for a task."""
        scratch = self.scratch_root / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def _write_scratch(self, scratch: Path, phase: str, results: list[WorkerResult]) -> None:
        """Write worker results to scratch directory as JSON."""
        phase_dir = scratch / phase
        phase_dir.mkdir(exist_ok=True)
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
