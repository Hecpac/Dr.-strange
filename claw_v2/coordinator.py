from __future__ import annotations

import contextvars
import json
import logging
import os
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as futures_wait
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

from claw_v2.adapters.base import AdapterError
from claw_v2.browser_evidence import BrowserEvidenceCollector
from claw_v2.langgraph_coordinator import LangGraphShadowRunner
from claw_v2.redaction import redact_text
from claw_v2.tracing import attach_trace, child_trace_context, new_trace_context
from claw_v2.types import ProviderRole

logger = logging.getLogger(__name__)

DEFAULT_WORKER_RESULT_SUMMARY_CHARS = 16_000
DEFAULT_PHASE_INPUT_SUMMARY_CHARS = 48_000
CRITICAL_WORKER_MARKER = "CRITICAL ERROR EN WORKER"
MECHANICAL_TRUNCATION_SIGNATURE = (
    "... [CRITICAL: Contenido truncado mecánicamente por falla en destilación semántica. "
    "Los datos intermedios fueron omitidos]"
)
HEAD_TAIL_PRESERVE_CHARS = 2_000
PHASE_ORDER = ("research", "synthesis", "implementation", "verification")
IMPLEMENTATION_STARTED_MARKER = "implementation.started"
DEFAULT_SCRATCH_RETENTION_DAYS = 14.0
_SCRATCH_PRUNE_MAX_DIRS = 50
F2_PAYLOAD_TEXT_LIMIT = 300


@dataclass(slots=True)
class WorkerTask:
    """A single unit of work dispatched to a worker."""

    name: str
    instruction: str
    lane: str = "research"
    assigned_agent: str | None = None
    timeout_seconds: float | None = None


@dataclass(slots=True)
class WorkerResult:
    """Result from a single worker execution."""

    task_name: str
    content: str
    duration_seconds: float
    error: str = ""
    degraded_compaction: bool = False


@dataclass(slots=True)
class CoordinatorResult:
    """Outcome of a full coordinator run."""

    task_id: str
    phase_results: dict[str, list[WorkerResult]] = field(default_factory=dict)
    synthesis: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    audit: dict[str, Any] = field(default_factory=dict)


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
        worker_result_summary_chars: int = DEFAULT_WORKER_RESULT_SUMMARY_CHARS,
        phase_input_summary_chars: int = DEFAULT_PHASE_INPUT_SUMMARY_CHARS,
        default_worker_timeout_seconds: float = 120.0,
        default_research_timeout_seconds: float = 90.0,
        default_verification_timeout_seconds: float = 60.0,
        default_implementation_timeout_seconds: float = 180.0,
        scratch_retention_days: float = DEFAULT_SCRATCH_RETENTION_DAYS,
        f2_durability_store: Any | None = None,
        browser_evidence_collector: BrowserEvidenceCollector | None = None,
        langgraph_shadow_runner: LangGraphShadowRunner | None = None,
    ) -> None:
        self.router = router
        self.observe = observe
        self.scratch_root = Path(scratch_root)
        self.scratch_retention_days = float(scratch_retention_days)
        self.max_workers = max_workers
        self.agent_registry = agent_registry or {}
        self.orchestration_store = orchestration_store
        self.worker_result_summary_chars = max(1, int(worker_result_summary_chars))
        self.phase_input_summary_chars = max(1, int(phase_input_summary_chars))
        self.default_worker_timeout_seconds = max(1.0, float(default_worker_timeout_seconds))
        self.default_research_timeout_seconds = max(1.0, float(default_research_timeout_seconds))
        self.default_verification_timeout_seconds = max(
            1.0, float(default_verification_timeout_seconds)
        )
        self.default_implementation_timeout_seconds = max(
            1.0, float(default_implementation_timeout_seconds)
        )
        self.f2_durability_store = f2_durability_store
        self.browser_evidence_collector = browser_evidence_collector
        self.langgraph_shadow_runner = langgraph_shadow_runner

    def run(
        self,
        task_id: str,
        objective: str,
        research_tasks: list[WorkerTask],
        implementation_tasks: list[WorkerTask] | None = None,
        verification_tasks: list[WorkerTask] | None = None,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
        *,
        start_phase: str | None = None,
        should_abort: Callable[[], bool] | None = None,
        allow_implementation_rerun: bool = False,
    ) -> CoordinatorResult:
        """Execute the full coordinator cycle.

        1. Research   — parallel workers gather information
        2. Synthesis  — coordinator merges findings into a plan
        3. Implementation — parallel workers execute the plan (optional)
        4. Verification   — parallel workers validate results (optional)

        F3.1 (2026-06-12): ``start_phase`` resumes a killed run — phases
        before it load their artifacts from scratch instead of re-executing.
        Implementation (the phase with external side effects) is gated: if a
        previous attempt started it without persisting completed results, a
        resumed run fails closed with ``implementation_rerun_blocked`` unless
        ``allow_implementation_rerun`` is set. AM-CANCEL: ``should_abort`` is
        checked at every phase boundary.
        """
        if start_phase is not None and start_phase not in PHASE_ORDER:
            raise ValueError(f"start_phase must be one of {PHASE_ORDER}, got {start_phase!r}")
        start = time.time()
        self._prune_stale_scratch_dirs(keep_task_id=task_id)
        scratch = self._ensure_scratch(task_id)
        result = CoordinatorResult(task_id=task_id)
        trace = new_trace_context(job_id=task_id, artifact_id=task_id)
        orchestration_run_id = task_id
        start_rank = PHASE_ORDER.index(start_phase) if start_phase else 0

        def _phase_resumable(phase: str) -> bool:
            return PHASE_ORDER.index(phase) < start_rank

        f2_task_started = False
        f2_active_phase: str | None = None
        f2_task_terminal_recorded = False

        def _mark_f2_task_started() -> None:
            nonlocal f2_task_started
            self._f2_record_task_started(
                task_id=task_id,
                run_id=orchestration_run_id,
                start_phase=start_phase,
            )
            if self.f2_durability_store is not None:
                f2_task_started = True

        def _mark_f2_phase_started(phase: str) -> None:
            nonlocal f2_active_phase
            self._f2_record_phase_started(
                task_id=task_id,
                run_id=orchestration_run_id,
                phase=phase,
            )
            if self.f2_durability_store is not None:
                f2_active_phase = phase

        def _mark_f2_phase_completed(
            phase: str,
            *,
            results: list[WorkerResult] | None = None,
            payload: dict[str, Any] | None = None,
        ) -> None:
            nonlocal f2_active_phase
            self._f2_record_phase_completed(
                task_id=task_id,
                run_id=orchestration_run_id,
                phase=phase,
                results=results,
                payload=payload,
            )
            if f2_active_phase == phase:
                f2_active_phase = None

        def _mark_f2_task_terminal(status: str) -> None:
            nonlocal f2_task_terminal_recorded
            self._f2_record_task_terminal(
                task_id=task_id,
                run_id=orchestration_run_id,
                status=status,
                result=result,
            )
            if self.f2_durability_store is not None:
                f2_task_terminal_recorded = True

        def _close_f2_after_exception(exc: Exception) -> None:
            if not f2_task_started or self.f2_durability_store is None:
                return
            if f2_active_phase is not None:
                try:
                    self._f2_record_phase_failed(
                        task_id=task_id,
                        run_id=orchestration_run_id,
                        phase=f2_active_phase,
                        error=str(exc),
                    )
                except Exception:
                    logger.debug("F2 phase failure close failed", exc_info=True)
            if not f2_task_terminal_recorded:
                try:
                    self._f2_record_task_terminal(
                        task_id=task_id,
                        run_id=orchestration_run_id,
                        status="failed",
                        result=result,
                    )
                except Exception:
                    logger.debug("F2 task terminal failure close failed", exc_info=True)

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
                payload={"task_id": task_id, "objective": objective, "start_phase": start_phase},
            )
            _mark_f2_task_started()
            # Phase 1: Research
            research_results: list[WorkerResult] = []
            if _phase_resumable("research"):
                research_results = self._load_scratch_results(task_id, "research")
                if research_results:
                    result.phase_results["research"] = research_results
                    self._emit_phase_resumed_from_scratch(
                        trace, task_id, "research", len(research_results)
                    )
                    # A kill between _write_scratch and the self-healing path
                    # can persist a critical artifact: re-check loaded results
                    # so a resume never proceeds past a critical worker error.
                    if _phase_all_workers_failed(research_results):
                        return self._complete_worker_phase_failed_run(
                            result=result,
                            phase="research",
                            phase_results=research_results,
                            orchestration_run_id=orchestration_run_id,
                            trace_context=trace,
                            start_time=start,
                        )
                    critical = _critical_worker_result(research_results)
                    if critical is not None:
                        return self._complete_critical_worker_run(
                            result=result,
                            objective=objective,
                            phase="research",
                            critical_result=critical,
                            collected_results=research_results,
                            scratch=scratch,
                            orchestration_run_id=orchestration_run_id,
                            trace_context=trace,
                            lane_overrides=lane_overrides,
                            start_time=start,
                        )
            if not result.phase_results.get("research"):
                self._orchestration_begin_phase(orchestration_run_id, "research", trace)
                _mark_f2_phase_started("research")
                research_results = self._dispatch_parallel(
                    self._with_phase_timeout(research_tasks, self.default_research_timeout_seconds),
                    trace,
                    lane_overrides=lane_overrides,
                )
                research_results = self._append_browser_evidence(
                    task_id=task_id,
                    objective=objective,
                    research_results=research_results,
                    trace_context=trace,
                )
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
                self._orchestration_require_ack(
                    research_artifact_id, consumer_role="coordinator_synthesis"
                )
                research_failed = _phase_all_workers_failed(research_results)
                research_reason = (
                    "research_phase_failed" if research_failed else "research_phase_completed"
                )
                self._orchestration_checkpoint(
                    orchestration_run_id,
                    phase="research",
                    reason=research_reason,
                    artifact_ids=[research_artifact_id] if research_artifact_id else [],
                )
                self._orchestration_finish_phase(
                    orchestration_run_id,
                    "research",
                    trace,
                    results=research_results,
                )
                if research_failed:
                    return self._complete_worker_phase_failed_run(
                        result=result,
                        phase="research",
                        phase_results=research_results,
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        start_time=start,
                    )
                _mark_f2_phase_completed("research", results=research_results)
                critical = _critical_worker_result(research_results)
                if critical is not None:
                    return self._complete_critical_worker_run(
                        result=result,
                        objective=objective,
                        phase="research",
                        critical_result=critical,
                        collected_results=research_results,
                        scratch=scratch,
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        lane_overrides=lane_overrides,
                        start_time=start,
                    )

            if self._abort_requested(should_abort):
                return self._complete_cancelled_run(
                    result=result,
                    next_phase="synthesis",
                    orchestration_run_id=orchestration_run_id,
                    trace_context=trace,
                    start_time=start,
                )

            # Phase 2: Synthesis
            synthesis = ""
            synthesis_artifact_id: str | None = None
            if _phase_resumable("synthesis"):
                synthesis = self._load_scratch_text(task_id, "synthesis.md")
                if synthesis:
                    result.synthesis = synthesis
                    self._emit_phase_resumed_from_scratch(trace, task_id, "synthesis", 1)
            if not synthesis:
                self._orchestration_begin_phase(orchestration_run_id, "synthesis", trace)
                _mark_f2_phase_started("synthesis")
                synthesis = self._synthesize(
                    objective, research_results, trace, lane_overrides=lane_overrides
                )
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
                self._orchestration_require_ack(
                    synthesis_artifact_id, consumer_role=synthesis_consumer
                )
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
                    payload={
                        "content_length": len(synthesis or ""),
                        "synthesis_empty": not (synthesis or "").strip(),
                    },
                )
                _mark_f2_phase_completed(
                    "synthesis",
                    payload={
                        "content_length": len(synthesis or ""),
                        "synthesis_empty": not (synthesis or "").strip(),
                    },
                )
            synthesis_artifact_ref = synthesis_artifact_id or str(scratch / "synthesis.md")
            synthesis_summary = _compact_text(synthesis, limit=self.phase_input_summary_chars)
            synthesis_empty = not (synthesis or "").strip()
            if synthesis_empty:
                # AM-SYNTH (2026-06-12): an empty synthesis used to degrade in
                # silence — downstream phases consumed "none" as their plan and
                # every phase still closed succeeded. Make it visible: audit
                # flag + event here, "Advertencia de Contexto" downstream.
                result.audit = {**result.audit, "synthesis_empty": True}
                self.observe.emit(
                    "coordinator_synthesis_empty",
                    trace_id=trace["trace_id"],
                    root_trace_id=trace["root_trace_id"],
                    span_id=trace["span_id"],
                    parent_span_id=trace["parent_span_id"],
                    job_id=trace["job_id"],
                    artifact_id=trace["artifact_id"],
                    payload={"task_id": task_id, "objective": objective[:300]},
                )

            # Phase 3: Implementation (optional)
            impl_results: list[WorkerResult] = []
            impl_artifact_id: str | None = None
            if implementation_tasks:
                if self._abort_requested(should_abort):
                    return self._complete_cancelled_run(
                        result=result,
                        next_phase="implementation",
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        start_time=start,
                    )
                if _phase_resumable("implementation"):
                    impl_results = self._load_scratch_results(task_id, "implementation")
                    if impl_results:
                        result.phase_results["implementation"] = impl_results
                        self._emit_phase_resumed_from_scratch(
                            trace, task_id, "implementation", len(impl_results)
                        )
                        critical = _critical_worker_result(impl_results)
                        if _phase_all_workers_failed(impl_results):
                            return self._complete_worker_phase_failed_run(
                                result=result,
                                phase="implementation",
                                phase_results=impl_results,
                                orchestration_run_id=orchestration_run_id,
                                trace_context=trace,
                                start_time=start,
                            )
                        if critical is not None:
                            return self._complete_critical_worker_run(
                                result=result,
                                objective=objective,
                                phase="implementation",
                                critical_result=critical,
                                collected_results=research_results + impl_results,
                                scratch=scratch,
                                orchestration_run_id=orchestration_run_id,
                                trace_context=trace,
                                lane_overrides=lane_overrides,
                                start_time=start,
                            )
            if implementation_tasks and not result.phase_results.get("implementation"):
                started_marker = scratch / IMPLEMENTATION_STARTED_MARKER
                if (
                    start_phase is not None
                    and started_marker.exists()
                    and not allow_implementation_rerun
                ):
                    # F3.1 gate: a previous attempt started implementation but
                    # never persisted completed results — partial external side
                    # effects are possible. Re-execution must be an explicit
                    # decision, never an automatic replay.
                    result.error = "implementation_rerun_blocked"
                    result.duration_seconds = time.time() - start
                    self.observe.emit(
                        "coordinator_implementation_rerun_blocked",
                        trace_id=trace["trace_id"],
                        root_trace_id=trace["root_trace_id"],
                        span_id=trace["span_id"],
                        parent_span_id=trace["parent_span_id"],
                        job_id=trace["job_id"],
                        artifact_id=trace["artifact_id"],
                        payload={"task_id": task_id, "marker": str(started_marker)},
                    )
                    self._f2_record_phase_blocked(
                        task_id=task_id,
                        run_id=orchestration_run_id,
                        phase="implementation",
                        reason=result.error,
                    )
                    _mark_f2_task_terminal("failed")
                    self._orchestration_complete_run(
                        orchestration_run_id,
                        status="failed",
                        reason=result.error,
                        trace_context=trace,
                    )
                    return result
                self._orchestration_begin_phase(orchestration_run_id, "implementation", trace)
                _mark_f2_phase_started("implementation")
                started_marker.write_text(
                    json.dumps({"task_id": task_id, "started_at": time.time()}),
                    encoding="utf-8",
                )
                impl_tasks = self._inject_context(
                    self._with_phase_timeout(
                        implementation_tasks, self.default_implementation_timeout_seconds
                    ),
                    objective=objective,
                    input_artifact_ref=synthesis_artifact_ref,
                    input_summary=synthesis_summary,
                    phase_input_summary_chars=self.phase_input_summary_chars,
                    degraded=any(r.degraded_compaction for r in research_results)
                    or synthesis_empty,
                )
                impl_results = self._dispatch_parallel(
                    impl_tasks, trace, lane_overrides=lane_overrides
                )
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
                self._orchestration_require_ack(
                    impl_artifact_id, consumer_role="verification_workers"
                )
                implementation_failed = _phase_all_workers_failed(impl_results)
                self._orchestration_checkpoint(
                    orchestration_run_id,
                    phase="implementation",
                    reason="implementation_phase_failed"
                    if implementation_failed
                    else "implementation_phase_completed",
                    artifact_ids=[impl_artifact_id] if impl_artifact_id else [],
                )
                self._orchestration_finish_phase(
                    orchestration_run_id,
                    "implementation",
                    trace,
                    results=impl_results,
                )
                if implementation_failed:
                    return self._complete_worker_phase_failed_run(
                        result=result,
                        phase="implementation",
                        phase_results=impl_results,
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        start_time=start,
                    )
                _mark_f2_phase_completed("implementation", results=impl_results)
                critical = _critical_worker_result(impl_results)
                if critical is not None:
                    return self._complete_critical_worker_run(
                        result=result,
                        objective=objective,
                        phase="implementation",
                        critical_result=critical,
                        collected_results=research_results + impl_results,
                        scratch=scratch,
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        lane_overrides=lane_overrides,
                        start_time=start,
                    )

            # Phase 4: Verification (optional)
            if verification_tasks:
                if self._abort_requested(should_abort):
                    return self._complete_cancelled_run(
                        result=result,
                        next_phase="verification",
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        start_time=start,
                    )
                self._orchestration_begin_phase(orchestration_run_id, "verification", trace)
                _mark_f2_phase_started("verification")
                verification_input_ref = synthesis_artifact_ref
                verification_input_summary = synthesis_summary
                if impl_results:
                    verification_input_ref = impl_artifact_id or str(scratch / "implementation")
                    verification_input_summary = self._phase_results_summary(
                        impl_results,
                        limit=self.phase_input_summary_chars,
                        trace_context=trace,
                        lane_overrides=lane_overrides,
                    )
                verify_tasks = self._inject_context(
                    self._with_phase_timeout(
                        verification_tasks, self.default_verification_timeout_seconds
                    ),
                    objective=objective,
                    input_artifact_ref=verification_input_ref,
                    input_summary=verification_input_summary,
                    phase_input_summary_chars=self.phase_input_summary_chars,
                    degraded=any(
                        r.degraded_compaction
                        for r in (impl_results if impl_results else research_results)
                    )
                    or synthesis_empty,
                )
                verify_results = self._dispatch_parallel(
                    verify_tasks, trace, lane_overrides=lane_overrides
                )
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
                self._orchestration_ack(
                    verification_artifact_id, consumer_role="coordinator_result"
                )
                self._orchestration_require_ack(
                    verification_artifact_id, consumer_role="coordinator_result"
                )
                verification_failed = _phase_all_workers_failed(verify_results)
                self._orchestration_checkpoint(
                    orchestration_run_id,
                    phase="verification",
                    reason="verification_phase_failed"
                    if verification_failed
                    else "verification_phase_completed",
                    artifact_ids=[verification_artifact_id] if verification_artifact_id else [],
                )
                self._orchestration_finish_phase(
                    orchestration_run_id,
                    "verification",
                    trace,
                    results=verify_results,
                )
                if verification_failed:
                    return self._complete_worker_phase_failed_run(
                        result=result,
                        phase="verification",
                        phase_results=verify_results,
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        start_time=start,
                    )
                _mark_f2_phase_completed("verification", results=verify_results)
                critical = _critical_worker_result(verify_results)
                if critical is not None:
                    return self._complete_critical_worker_run(
                        result=result,
                        objective=objective,
                        phase="verification",
                        critical_result=critical,
                        collected_results=research_results + impl_results + verify_results,
                        scratch=scratch,
                        orchestration_run_id=orchestration_run_id,
                        trace_context=trace,
                        lane_overrides=lane_overrides,
                        start_time=start,
                    )

            result.duration_seconds = time.time() - start
            _mark_f2_task_terminal("succeeded")
            self._run_langgraph_shadow(
                task_id=task_id,
                objective=objective,
                research_tasks=research_tasks,
                implementation_tasks=implementation_tasks,
                verification_tasks=verification_tasks,
                lane_overrides=lane_overrides,
                start_phase=start_phase,
                legacy_result=result,
            )
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
            _close_f2_after_exception(exc)
            self._orchestration_complete_run(
                orchestration_run_id,
                status="failed",
                reason=str(exc)[:300],
                trace_context=trace,
            )
            return result

    # Bounded wait for in-flight workers to finish after a critical error
    # cancellation. Workers have per-task timeouts up to 900s; waiting the full
    # duration would block the coordinator indefinitely. 30s gives active LLM
    # calls a chance to complete or hit their own timeout, while preventing
    # unbounded blocking. Orphaned threads will terminate on their own when
    # their underlying adapter timeout fires.
    _CRITICAL_ABORT_DRAIN_SECONDS = 30.0

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

        results_by_index: list[WorkerResult | None] = [None] * len(tasks)
        pool = ThreadPoolExecutor(max_workers=min(self.max_workers, len(tasks)))
        from claw_v2.verification.local_tool_runner import (
            contract_artifact_scope,
            current_contract_artifact_scope,
        )

        worker_contract_scope = current_contract_artifact_scope()
        # Cooperative cancellation token: workers check this event between
        # retry attempts and before initiating long-running LLM calls.
        abort_event = threading.Event()

        def _cancel_non_critical_results_after_critical() -> None:
            for idx, existing in enumerate(results_by_index):
                if existing is None or _has_critical_worker_error(existing):
                    continue
                results_by_index[idx] = WorkerResult(
                    task_name=existing.task_name,
                    content="",
                    duration_seconds=existing.duration_seconds,
                    error=existing.error or "cancelled:critical_worker_error",
                    degraded_compaction=existing.degraded_compaction,
                )

        def _execute_scoped_worker(task: WorkerTask) -> WorkerResult:
            if abort_event.is_set():
                return WorkerResult(
                    task_name=task.name,
                    content="",
                    duration_seconds=0.0,
                    error="cancelled:before_start",
                )
            if worker_contract_scope:
                with contract_artifact_scope(worker_contract_scope):
                    return self._execute_worker(
                        task, trace_context, lane_overrides=lane_overrides, abort_event=abort_event
                    )
            return self._execute_worker(
                task, trace_context, lane_overrides=lane_overrides, abort_event=abort_event
            )

        try:
            futures = {
                pool.submit(_execute_scoped_worker, task): (index, task)
                for index, task in enumerate(tasks)
            }
            for future in as_completed(futures):
                index, task = futures[future]
                try:
                    worker_result = future.result()
                except Exception as exc:
                    worker_result = WorkerResult(
                        task_name=task.name,
                        content="",
                        duration_seconds=0.0,
                        error=str(exc),
                    )
                results_by_index[index] = worker_result
                if _has_critical_worker_error(worker_result):
                    # Signal all in-flight workers to abort at their next
                    # cancellation checkpoint (between retries, before LLM calls).
                    abort_event.set()
                    _cancel_non_critical_results_after_critical()
                    # Cancel futures that haven't started execution yet.
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    # Drain in-flight workers with a bounded timeout. This
                    # prevents thread/connection leaks while avoiding unbounded
                    # blocking on workers stuck in long LLM calls.
                    in_flight = [f for f in futures if f is not future and not f.done()]
                    if in_flight:
                        done, not_done = futures_wait(
                            in_flight, timeout=self._CRITICAL_ABORT_DRAIN_SECONDS
                        )
                        if not_done:
                            # Some workers didn't finish within the drain window.
                            # They will terminate on their own when their adapter
                            # timeout fires; emit an observability event for audit.
                            orphaned_task_names = [
                                futures[f][1].name for f in not_done if f in futures
                            ]
                            self.observe.emit(
                                "coordinator_critical_abort_orphaned_workers",
                                trace_id=(trace_context or {}).get("trace_id"),
                                root_trace_id=(trace_context or {}).get("root_trace_id"),
                                span_id=(trace_context or {}).get("span_id"),
                                parent_span_id=(trace_context or {}).get("parent_span_id"),
                                payload={
                                    "orphaned_workers": len(not_done),
                                    "orphaned_task_names": orphaned_task_names,
                                    "drain_timeout_seconds": self._CRITICAL_ABORT_DRAIN_SECONDS,
                                },
                            )
                    # Shutdown the pool. wait=True ensures the pool's internal
                    # thread queue is cleaned up; any still-running threads will
                    # finish naturally and release their resources.
                    pool.shutdown(wait=True, cancel_futures=True)
                    return [result for result in results_by_index if result is not None]
        finally:
            # Ensure pool is always shut down, even on non-critical paths.
            # shutdown() is idempotent; calling it twice is safe.
            pool.shutdown(wait=True, cancel_futures=True)
        return [result for result in results_by_index if result is not None]

    _RETRY_LANES: frozenset[str] = frozenset({"worker", "worker_heavy"})

    def _execute_worker(
        self,
        task: WorkerTask,
        trace_context: dict[str, Any] | None = None,
        *,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
        abort_event: threading.Event | None = None,
    ) -> WorkerResult:
        """Execute a single worker task via the LLM router.

        When ``abort_event`` is provided and set, the worker short-circuits
        before initiating any LLM call. This is checked at the top of the
        method and before each retry attempt, so a critical-error abort
        in a sibling worker prevents this worker from starting wasteful
        (and potentially expensive) LLM calls.
        """
        start = time.time()
        # Cooperative abort: if the coordinator already decided to cancel
        # (e.g. a sibling worker hit a critical error), skip the LLM call
        # entirely instead of consuming budget on doomed work.
        if abort_event is not None and abort_event.is_set():
            return WorkerResult(
                task_name=task.name,
                content="",
                duration_seconds=time.time() - start,
                error="cancelled:abort_event",
            )
        task_trace = child_trace_context(trace_context, artifact_id=task.name)
        kwargs: dict[str, Any] = {
            "lane": task.lane,
            "role": self._role_for_worker_task(task),
            "timeout": task.timeout_seconds
            if task.timeout_seconds is not None
            else self._timeout_for_worker_task(task),
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
            if override.get("timeout") is not None:
                kwargs["timeout"] = float(override["timeout"])
        attempts = 2 if task.lane in self._RETRY_LANES else 1
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            # Check abort before each attempt — prevents retrying a call when
            # the coordinator has already decided to abort the whole phase.
            if abort_event is not None and abort_event.is_set():
                return WorkerResult(
                    task_name=task.name,
                    content="",
                    duration_seconds=time.time() - start,
                    error=f"cancelled:abort_event_before_attempt_{attempt}",
                )
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
                    # Check abort before retrying — no point retrying if the
                    # phase is being torn down due to a sibling's critical error.
                    if abort_event is not None and abort_event.is_set():
                        return WorkerResult(
                            task_name=task.name,
                            content="",
                            duration_seconds=time.time() - start,
                            error=f"cancelled:abort_event_before_retry",
                        )
                    self.observe.emit(
                        "coordinator_worker_retry",
                        trace_id=task_trace["trace_id"],
                        root_trace_id=task_trace["root_trace_id"],
                        span_id=task_trace["span_id"],
                        parent_span_id=task_trace["parent_span_id"],
                        artifact_id=task.name,
                        payload={
                            "task_name": task.name,
                            "lane": task.lane,
                            "error": str(exc)[:300],
                            "attempt": attempt,
                        },
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

    def _run_langgraph_shadow(
        self,
        *,
        task_id: str,
        objective: str,
        research_tasks: list[WorkerTask],
        implementation_tasks: list[WorkerTask] | None,
        verification_tasks: list[WorkerTask] | None,
        lane_overrides: dict[str, dict[str, Any]] | None,
        start_phase: str | None,
        legacy_result: CoordinatorResult,
    ) -> None:
        runner = self.langgraph_shadow_runner
        if runner is None:
            return
        try:
            runner.run(
                task_id=task_id,
                objective=objective,
                research_tasks=research_tasks,
                implementation_tasks=implementation_tasks,
                verification_tasks=verification_tasks,
                lane_overrides=lane_overrides,
                start_phase=start_phase,
                legacy_result=legacy_result,
            )
        except Exception as exc:  # noqa: BLE001 - shadow mode cannot affect productive result
            logger.exception("LangGraph shadow runner failed")
            emit = getattr(self.observe, "emit", None)
            if callable(emit):
                emit(
                    "langgraph_shadow_failed",
                    payload={
                        "task_id": task_id,
                        "error_type": type(exc).__name__,
                        "objective_chars": len(objective or ""),
                    },
                )

    def _append_browser_evidence(
        self,
        *,
        task_id: str,
        objective: str,
        research_results: list[WorkerResult],
        trace_context: dict[str, Any] | None = None,
    ) -> list[WorkerResult]:
        collector = self.browser_evidence_collector
        if collector is None:
            return research_results
        start = time.time()
        try:
            report = collector.collect(
                task_id=task_id,
                objective=objective,
                research_results=research_results,
            )
        except Exception as exc:  # noqa: BLE001 - preserve coordinator progress, surface gap
            logger.exception("Coordinator browser evidence collection failed")
            evidence_trace = child_trace_context(trace_context, artifact_id="browser_evidence")
            emit = getattr(self.observe, "emit", None)
            if callable(emit):
                emit(
                    "coordinator_browser_evidence_failed",
                    trace_id=evidence_trace["trace_id"],
                    root_trace_id=evidence_trace["root_trace_id"],
                    span_id=evidence_trace["span_id"],
                    parent_span_id=evidence_trace["parent_span_id"],
                    job_id=evidence_trace["job_id"],
                    artifact_id=evidence_trace["artifact_id"],
                    payload={"task_id": task_id, "error_type": type(exc).__name__},
                )
            return [
                *research_results,
                WorkerResult(
                    task_name="browser_evidence",
                    content="",
                    duration_seconds=time.time() - start,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                ),
            ]
        if report is None:
            return research_results
        return [
            *research_results,
            WorkerResult(
                task_name="browser_evidence",
                content=report.content,
                duration_seconds=report.duration_seconds,
            ),
        ]

    def _synthesize(
        self,
        objective: str,
        research_results: list[WorkerResult],
        trace_context: dict[str, Any] | None = None,
        *,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
        critical_audit: dict[str, Any] | None = None,
    ) -> str:
        """Merge research findings into a coherent plan."""
        findings = self._phase_results_summary(
            research_results,
            limit=self.phase_input_summary_chars,
            trace_context=trace_context,
            lane_overrides=lane_overrides,
        )

        agent_context = ""
        if self.agent_registry:
            agent_lines = "\n".join(
                f"- {name}: domains={caps.get('domains', [])}, model={caps.get('model', '?')}, skills={caps.get('skills', [])}"
                for name, caps in self.agent_registry.items()
            )
            agent_context = f"\n\n## Registro de Subagentes Disponibles\n{agent_lines}"

        critical_context = ""
        if critical_audit:
            raw_error = str(critical_audit.get("raw_error") or "")
            critical_context = (
                "\n\n## Protocolo de Contención Self-Healing\n"
                "El pipeline lineal queda detenido. La meta principal de esta síntesis ya no es completar "
                "el objetivo original, sino diagnosticar el error crítico del worker, proponer una hipótesis "
                "de reparación inmediata y delegar una subtarea enfocada en corregir el entorno o dependencia "
                "antes de reintentar la misión principal.\n\n"
                "## Error Crítico Crudo Redactado\n"
                f"{raw_error}"
            )

        prompt = (
            "### Prompt: Síntesis y Orquestación del Enjambre\n\n"
            "Eres el agente coordinador central. Tu tarea es analizar los reportes técnicos unificados "
            "de los subagentes de investigación y consolidarlos en un plan de ejecución maestro y coherente.\n"
            "## Objetivo General:\n\n"
            f"**{objective}**{agent_context}\n"
            "## Evidencia Recopilada por los Workers:\n\n"
            f"**{findings}**{critical_context}\n"
            "## Reglas Críticas de Evaluación:\n\n"
            "* **Invariante de Evidencia:** No asumas que un paso intermedio fue exitoso si el reporte "
            "del subagente omitió los logs de confirmación. Evalúa las lagunas de información como "
            "riesgos técnicos activos.\n"
            "* **Aislamiento de Errores:** Si un reporte contiene la cadena `CRITICAL ERROR EN WORKER`, "
            "detén el pipeline asincrónico inmediatamente y prioriza una subtarea de diagnóstico y "
            "reparación (Self-Healing).\n\n"
            "**Formato del Plan Maestro:** Genera una secuencia numerada de pasos de ingeniería. "
            "Cada paso debe estar explícitamente delegado al subagente especializado idóneo de tu registro "
            "basándote en sus habilidades específicas, utilizando estrictamente este formato:\n"
            "`**Step N [nombre_del_agente]:** Descripción concreta del comando o edición a ejecutar.`"
        )

        try:
            synthesis_trace = child_trace_context(
                trace_context, artifact_id="coordinator_synthesis"
            )
            response = self.router.ask(
                prompt,
                lane="research",
                provider=(lane_overrides or {}).get("research", {}).get("provider"),
                model=(lane_overrides or {}).get("research", {}).get("model"),
                effort=(lane_overrides or {}).get("research", {}).get("effort"),
                role="coordinator_research",
                timeout=_lane_override_timeout(
                    lane_overrides,
                    "research",
                    default=self.default_research_timeout_seconds,
                ),
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
        phase_input_summary_chars: int = DEFAULT_PHASE_INPUT_SUMMARY_CHARS,
        degraded: bool = False,
    ) -> list[WorkerTask]:
        """Prepend compact artifact-mediated context to each task's instruction."""
        ref = input_artifact_ref or "none"
        summary = _compact_text(input_summary, limit=phase_input_summary_chars)
        # F3.3 (2026-06-12): degraded_compaction used to be an internal flag
        # only — workers consumed mechanically-cut context without knowing it.
        degraded_line = (
            "* **Advertencia de Contexto:** la destilación semántica de la fase previa "
            "falló y el resumen fue recortado mecánicamente (head+tail). Si te falta "
            "contexto, consulta el artefacto de referencia en scratch antes de asumir.\n"
            if degraded
            else ""
        )
        return [
            WorkerTask(
                name=t.name,
                instruction=(
                    "### Prompt: Contexto de Continuidad Operativa\n\n"
                    "## Contexto de la Misión\n\n"
                    f"* **Objetivo General del Dueño:** {objective}\n"
                    f"* **Artefacto de Referencia en Scratch:** {ref}\n"
                    f"{degraded_line}"
                    f"* **Estado Técnico Consolidado:** {summary or 'none'}\n\n"
                    "## Tu Tarea Específica:\n\n"
                    f"**{t.instruction}**\n"
                    "**Directriz Ejecutiva:** No operes de manera aislada. Utiliza los parámetros e "
                    "identificadores del estado consolidado para asegurar que tu código, parche o comando "
                    "de terminal encaje de forma exacta con las decisiones tomadas por las fases previas "
                    "del enjambre."
                ),
                lane=t.lane,
                assigned_agent=t.assigned_agent,
                timeout_seconds=t.timeout_seconds,
            )
            for t in tasks
        ]

    def _with_phase_timeout(
        self, tasks: list[WorkerTask], timeout_seconds: float
    ) -> list[WorkerTask]:
        return [
            WorkerTask(
                name=task.name,
                instruction=task.instruction,
                lane=task.lane,
                assigned_agent=task.assigned_agent,
                timeout_seconds=task.timeout_seconds
                if task.timeout_seconds is not None
                else timeout_seconds,
            )
            for task in tasks
        ]

    def _role_for_worker_task(self, task: WorkerTask) -> ProviderRole:
        if task.lane == "worker_heavy":
            return "heavy_coding"
        if task.lane == "worker":
            return "coordinator_worker"
        if task.lane == "verifier":
            return "coordinator_verification"
        if task.lane == "research":
            return "coordinator_research"
        return "coordinator_worker"

    def _timeout_for_worker_task(self, task: WorkerTask) -> float:
        if task.lane == "worker_heavy":
            return self.default_implementation_timeout_seconds
        if task.lane == "worker":
            return self.default_worker_timeout_seconds
        if task.lane == "verifier":
            return self.default_verification_timeout_seconds
        if task.lane == "research":
            return self.default_research_timeout_seconds
        return self.default_worker_timeout_seconds

    def _phase_results_summary(
        self,
        results: list[WorkerResult],
        *,
        limit: int,
        trace_context: dict[str, Any] | None = None,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        if not results:
            return "none"
        # AM-DISTILL (2026-06-12): each oversized worker result triggers an
        # LLM distillation call; they ran serially between phases. Summarize
        # per worker result concurrently (order preserved via submit order).
        if len(results) == 1:
            summaries = [
                self._worker_result_summary(
                    results[0],
                    limit=self.worker_result_summary_chars,
                    trace_context=trace_context,
                    lane_overrides=lane_overrides,
                )
            ]
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(results))) as pool:
                futures = [
                    pool.submit(
                        contextvars.copy_context().run,
                        partial(
                            self._worker_result_summary,
                            item,
                            limit=self.worker_result_summary_chars,
                            trace_context=trace_context,
                            lane_overrides=lane_overrides,
                        ),
                    )
                    for item in results
                ]
                summaries = [future.result() for future in futures]
        lines: list[str] = []
        critical_present = False
        for result, (summary, degraded) in zip(results, summaries):
            if _has_critical_worker_error(result):
                critical_present = True
            if degraded:
                result.degraded_compaction = True
            lines.append(f"- {result.task_name}: {summary}")
        joined = "\n".join(lines)
        if critical_present:
            return joined
        if len(joined) <= limit:
            return joined
        summary, _ = self._distill_text(
            joined,
            limit=limit,
            trace_context=trace_context,
            lane_overrides=lane_overrides,
        )
        return summary

    def _worker_result_summary(
        self,
        result: WorkerResult,
        *,
        limit: int,
        trace_context: dict[str, Any] | None = None,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, bool]:
        if result.error:
            text = f"ERROR: {result.error}"
        elif not result.content:
            return "empty result", False
        else:
            text = result.content
        redacted = redact_text(str(text), limit=0)
        if _has_critical_worker_error(result):
            return redacted, False
        return self._distill_text(
            redacted,
            limit=limit,
            trace_context=trace_context,
            lane_overrides=lane_overrides,
        )

    def _distill_text(
        self,
        text: str,
        *,
        limit: int,
        trace_context: dict[str, Any] | None = None,
        lane_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, bool]:
        redacted = redact_text(str(text or ""), limit=0)
        if len(redacted) <= limit:
            return redacted, False
        prompt = _distillation_prompt(redacted, limit=limit)
        try:
            distill_trace = child_trace_context(trace_context, artifact_id="semantic_distillation")
            response = self.router.ask(
                prompt,
                lane="research",
                provider=(lane_overrides or {}).get("research", {}).get("provider"),
                model=(lane_overrides or {}).get("research", {}).get("model"),
                effort=(lane_overrides or {}).get("research", {}).get("effort"),
                role="coordinator_research",
                timeout=_lane_override_timeout(
                    lane_overrides,
                    "research",
                    default=self.default_research_timeout_seconds,
                ),
                evidence_pack=attach_trace(
                    {"coordinator_phase": "semantic_distillation", "limit": limit},
                    distill_trace,
                ),
            )
            distilled = redact_text(str(response.content or ""), limit=0)
            if distilled and len(distilled) < limit:
                return distilled, False
        except Exception:
            logger.exception("Coordinator semantic distillation failed")
        return _mechanical_compact_text(redacted, limit=max(limit - 1, 1)), True

    def _complete_critical_worker_run(
        self,
        *,
        result: CoordinatorResult,
        objective: str,
        phase: str,
        critical_result: WorkerResult,
        collected_results: list[WorkerResult],
        scratch: Path,
        orchestration_run_id: str,
        trace_context: dict[str, Any],
        lane_overrides: dict[str, dict[str, Any]] | None,
        start_time: float,
    ) -> CoordinatorResult:
        audit = _critical_worker_audit(phase, critical_result)
        result.error = f"critical_worker_error:{critical_result.task_name}"
        result.audit = {**result.audit, **audit}
        self.observe.emit(
            "coordinator_critical_worker_error",
            trace_id=trace_context["trace_id"],
            root_trace_id=trace_context["root_trace_id"],
            span_id=trace_context["span_id"],
            parent_span_id=trace_context["parent_span_id"],
            job_id=trace_context["job_id"],
            artifact_id=critical_result.task_name,
            payload={
                "task_id": result.task_id,
                "phase": phase,
                "task_name": critical_result.task_name,
                "error": result.error,
            },
        )

        self._orchestration_begin_phase(orchestration_run_id, "synthesis", trace_context)
        self._f2_record_phase_started(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            phase="synthesis",
        )
        synthesis = self._synthesize(
            objective,
            collected_results,
            trace_context,
            lane_overrides=lane_overrides,
            critical_audit=audit,
        )
        result.synthesis = synthesis
        self._write_scratch_text(scratch, "synthesis.md", synthesis)
        synthesis_artifact_id = self._orchestration_record_text_artifact(
            orchestration_run_id,
            phase="synthesis",
            artifact_type="synthesis",
            content=synthesis,
            producer_role="coordinator_synthesis",
            consumer_role="coordinator_result",
            trace_context=trace_context,
        )
        self._orchestration_ack(synthesis_artifact_id, consumer_role="coordinator_result")
        self._orchestration_require_ack(synthesis_artifact_id, consumer_role="coordinator_result")
        self._orchestration_checkpoint(
            orchestration_run_id,
            phase="synthesis",
            reason="critical_worker_self_healing_synthesis",
            artifact_ids=[synthesis_artifact_id] if synthesis_artifact_id else [],
        )
        synthesis_payload = {
            "content_length": len(synthesis or ""),
            "critical_worker_error": True,
            "critical_phase": phase,
        }
        self._orchestration_finish_phase(
            orchestration_run_id,
            "synthesis",
            trace_context,
            payload=synthesis_payload,
        )
        self._f2_record_phase_completed(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            phase="synthesis",
            payload=synthesis_payload,
        )
        result.duration_seconds = time.time() - start_time
        self._f2_record_phase_failed(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            phase=phase,
            error=result.error,
        )
        self._f2_record_task_terminal(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            status="failed",
            result=result,
        )
        self._orchestration_complete_run(
            orchestration_run_id,
            status="failed",
            reason=result.error,
            trace_context=trace_context,
        )
        self.observe.emit(
            "coordinator_complete",
            trace_id=trace_context["trace_id"],
            root_trace_id=trace_context["root_trace_id"],
            span_id=trace_context["span_id"],
            parent_span_id=trace_context["parent_span_id"],
            job_id=trace_context["job_id"],
            artifact_id=trace_context["artifact_id"],
            payload={
                "task_id": result.task_id,
                "phases": list(result.phase_results.keys()),
                "duration": result.duration_seconds,
                "workers_total": sum(len(v) for v in result.phase_results.values()),
                "error": result.error,
            },
        )
        return result

    def _complete_worker_phase_failed_run(
        self,
        *,
        result: CoordinatorResult,
        phase: str,
        phase_results: list[WorkerResult],
        orchestration_run_id: str,
        trace_context: dict[str, Any],
        start_time: float,
    ) -> CoordinatorResult:
        result.error = _phase_worker_failure_error(phase, phase_results)
        result.duration_seconds = time.time() - start_time
        self._f2_record_phase_failed(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            phase=phase,
            error=result.error,
        )
        self._f2_record_task_terminal(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            status="failed",
            result=result,
        )
        self._orchestration_complete_run(
            orchestration_run_id,
            status="failed",
            reason=result.error,
            trace_context=trace_context,
        )
        self.observe.emit(
            "coordinator_phase_failed",
            trace_id=trace_context["trace_id"],
            root_trace_id=trace_context["root_trace_id"],
            span_id=trace_context["span_id"],
            parent_span_id=trace_context["parent_span_id"],
            job_id=trace_context["job_id"],
            artifact_id=trace_context["artifact_id"],
            payload={
                "task_id": result.task_id,
                "phase": phase,
                "error": result.error,
                **_phase_counts(phase_results),
            },
        )
        return result

    def _ensure_scratch(self, task_id: str) -> Path:
        """Create and return the scratch directory for a task."""
        scratch = self.scratch_root / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def detect_resume_phase(self, task_id: str) -> str:
        """Return the first phase whose completed artifacts are missing (F3.1).

        Scratch artifacts are written when a phase COMPLETES, so their
        presence proves the phase finished in a previous attempt.
        """
        if not self._load_scratch_results(task_id, "research"):
            return "research"
        if not self._load_scratch_text(task_id, "synthesis.md").strip():
            return "synthesis"
        if not self._load_scratch_results(task_id, "implementation"):
            return "implementation"
        return "verification"

    def _load_scratch_results(self, task_id: str, phase: str) -> list[WorkerResult]:
        phase_dir = self.scratch_root / task_id / phase
        if not phase_dir.is_dir():
            return []
        results: list[WorkerResult] = []
        for path in sorted(phase_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("Skipping corrupt scratch artifact %s", path)
                continue
            if not isinstance(data, dict):
                continue
            results.append(
                WorkerResult(
                    task_name=str(data.get("task_name") or path.stem),
                    content=str(data.get("content") or ""),
                    duration_seconds=float(data.get("duration_seconds") or 0.0),
                    error=str(data.get("error") or ""),
                    degraded_compaction=bool(data.get("degraded_compaction")),
                )
            )
        return results

    def _load_scratch_text(self, task_id: str, filename: str) -> str:
        try:
            return (self.scratch_root / task_id / filename).read_text(encoding="utf-8")
        except OSError:
            return ""

    def _emit_phase_resumed_from_scratch(
        self,
        trace_context: dict[str, Any],
        task_id: str,
        phase: str,
        artifact_count: int,
    ) -> None:
        self.observe.emit(
            "coordinator_phase_resumed_from_scratch",
            trace_id=trace_context["trace_id"],
            root_trace_id=trace_context["root_trace_id"],
            span_id=trace_context["span_id"],
            parent_span_id=trace_context["parent_span_id"],
            job_id=trace_context["job_id"],
            artifact_id=trace_context["artifact_id"],
            payload={"task_id": task_id, "phase": phase, "artifacts": artifact_count},
        )

    def _abort_requested(self, should_abort: Callable[[], bool] | None) -> bool:
        if should_abort is None:
            return False
        try:
            return bool(should_abort())
        except Exception:
            logger.debug("should_abort callback failed", exc_info=True)
            return False

    def _complete_cancelled_run(
        self,
        *,
        result: CoordinatorResult,
        next_phase: str,
        orchestration_run_id: str,
        trace_context: dict[str, Any],
        start_time: float,
    ) -> CoordinatorResult:
        """AM-CANCEL: stop at a phase boundary without starting the next phase."""
        result.error = f"cancelled_at_phase_boundary:{next_phase}"
        result.duration_seconds = time.time() - start_time
        self.observe.emit(
            "coordinator_cancelled",
            trace_id=trace_context["trace_id"],
            root_trace_id=trace_context["root_trace_id"],
            span_id=trace_context["span_id"],
            parent_span_id=trace_context["parent_span_id"],
            job_id=trace_context["job_id"],
            artifact_id=trace_context["artifact_id"],
            payload={
                "task_id": result.task_id,
                "next_phase": next_phase,
                "phases_completed": list(result.phase_results.keys()),
            },
        )
        self._f2_record_phase_blocked(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            phase=next_phase,
            reason=result.error,
        )
        self._f2_record_task_terminal(
            task_id=result.task_id,
            run_id=orchestration_run_id,
            status="failed",
            result=result,
        )
        self._orchestration_complete_run(
            orchestration_run_id,
            status="failed",
            reason=result.error,
            trace_context=trace_context,
        )
        return result

    def _prune_stale_scratch_dirs(self, *, keep_task_id: str) -> None:
        """Best-effort retention sweep of old task scratch dirs (F3.1).

        Bounded (at most _SCRATCH_PRUNE_MAX_DIRS per run) and failure-tolerant;
        runs in the coordinator worker thread at run() start, never in
        daemon.tick. The current task's dir is always kept.
        """
        if self.scratch_retention_days <= 0:
            return
        cutoff = time.time() - self.scratch_retention_days * 86_400
        try:
            candidates = sorted(self.scratch_root.iterdir())
        except OSError:
            return
        removed = 0
        for path in candidates:
            if removed >= _SCRATCH_PRUNE_MAX_DIRS:
                break
            if path.name == keep_task_id or not path.is_dir():
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
            except OSError:
                continue
        if removed:
            self.observe.emit(
                "coordinator_scratch_pruned",
                payload={"removed_dirs": removed, "retention_days": self.scratch_retention_days},
            )

    def _f2_record_task_started(
        self,
        *,
        task_id: str,
        run_id: str,
        start_phase: str | None,
    ) -> None:
        self._f2_record_checkpoint_event(
            task_id=task_id,
            run_id=run_id,
            phase="task",
            write_kind="coordinator_start",
            status="started",
            payload={
                "event": "coordinator_start",
                "start_phase": start_phase or "research",
            },
        )

    def _f2_record_phase_started(self, *, task_id: str, run_id: str, phase: str) -> None:
        self._f2_record_checkpoint_event(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_started",
            status="started",
            payload={"event": "phase_started", "phase": phase},
        )

    def _f2_record_phase_completed(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        results: list[WorkerResult] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        phase_payload = {"event": "phase_completed", "phase": phase, **dict(payload or {})}
        if results is not None:
            phase_payload.update(_phase_counts(results))
        self._f2_record_checkpoint_event(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_return",
            status="succeeded",
            payload=phase_payload,
        )

    def _f2_record_phase_failed(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        error: str,
    ) -> None:
        self._f2_record_checkpoint_event(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_error",
            status="failed",
            payload={"event": "phase_error", "phase": phase, "error": error},
        )

    def _f2_record_phase_blocked(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        reason: str,
    ) -> None:
        self._f2_record_checkpoint_event(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            write_kind="phase_blocked",
            status="blocked",
            payload={"event": "phase_blocked", "phase": phase, "reason": reason},
        )

    def _f2_record_task_terminal(
        self,
        *,
        task_id: str,
        run_id: str,
        status: str,
        result: CoordinatorResult,
    ) -> None:
        self._f2_record_checkpoint_event(
            task_id=task_id,
            run_id=run_id,
            phase="task",
            write_kind=f"task_{status}",
            status=status,
            payload={
                "event": f"task_{status}",
                "phases": list(result.phase_results.keys()),
                "duration_seconds": round(float(result.duration_seconds or 0.0), 6),
                "error": result.error,
            },
        )

    def _f2_record_checkpoint_event(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        write_kind: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        if self.f2_durability_store is None:
            return
        clean_payload = _f2_compact_payload(payload)
        try:
            write = self.f2_durability_store.append_checkpoint_write(
                task_id=task_id,
                run_id=run_id,
                phase=phase,
                write_kind=write_kind,
                payload=clean_payload,
            )
            latest = self.f2_durability_store.list_phase_checkpoints(
                task_id=task_id,
                run_id=run_id,
                phase=phase,
                order="phase_version_desc",
                limit=1,
            )
            phase_version = latest[0].phase_version + 1 if latest else 1
            self.f2_durability_store.create_phase_checkpoint(
                task_id=task_id,
                run_id=run_id,
                phase=phase,
                phase_version=phase_version,
                status=status,
                last_write_order=write.write_order,
                payload=clean_payload,
            )
        except Exception as exc:
            logger.exception(
                "F2 durability checkpoint write failed for task=%s run=%s phase=%s kind=%s",
                task_id,
                run_id,
                phase,
                write_kind,
            )
            self.observe.emit(
                "f2_durability_write_failed",
                payload={
                    "task_id": task_id,
                    "run_id": run_id,
                    "phase": phase,
                    "write_kind": write_kind,
                    "error": redact_text(str(exc), limit=F2_PAYLOAD_TEXT_LIMIT),
                },
            )
            raise

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
        status = (
            "failed"
            if results is not None and _phase_all_workers_failed(results)
            else "succeeded"
        )
        self.orchestration_store.finish_phase(
            run_id,
            phase,
            status=status,
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
        """Write worker results to scratch directory as JSON (atomically)."""
        phase_dir = scratch / phase
        phase_dir.mkdir(parents=True, exist_ok=True)
        for r in results:
            data = {
                "task_name": r.task_name,
                "content": r.content,
                "duration_seconds": r.duration_seconds,
                "error": r.error,
                "degraded_compaction": r.degraded_compaction,
            }
            _atomic_write_text(
                phase_dir / f"{r.task_name}.json",
                json.dumps(data, indent=2, ensure_ascii=False),
            )

    @staticmethod
    def _write_scratch_text(scratch: Path, filename: str, content: str) -> None:
        """Write a text file to the scratch directory (atomically)."""
        _atomic_write_text(scratch / filename, content)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and durably.

    Mirrors ``approval.py:_atomic_write_json`` (unique dot-prefixed tmp →
    fsync → ``os.replace``) and adds the parent-directory fsync that helper
    omits, so the rename itself survives a crash. Readers never observe a
    partial file: a crash between temp-write and rename leaves the target
    absent or intact, never half-written. The dot prefix keeps the tmp out
    of the ``*.json`` listing glob. On error the tmp is removed best-effort.
    """
    data = text.encode("utf-8")
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        # Write/fsync failed (e.g. ENOSPC): close the fd and remove the tmp so
        # a failed write never leaks an orphan .tmp (mirrors approval.py).
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    # Best-effort durability of the rename across power loss. A failure here
    # must NOT fail the caller: the target is already completely written and
    # atomically in place, so swallow OSError (some filesystems/sandboxes
    # disallow opening or fsync'ing a directory) rather than turning a
    # successful, durable write into a spurious run failure up the call stack.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _worker_result_payload(result: WorkerResult) -> dict[str, Any]:
    return {
        "task_name": result.task_name,
        "content": result.content,
        "duration_seconds": result.duration_seconds,
        "error": result.error,
        "degraded_compaction": result.degraded_compaction,
    }


def _lane_override_timeout(
    lane_overrides: dict[str, dict[str, Any]] | None,
    lane: str,
    *,
    default: float,
) -> float:
    value = (lane_overrides or {}).get(lane, {}).get("timeout")
    if value is None:
        return float(default)
    return float(value)


def _phase_results_summary(results: list[WorkerResult], *, limit: int) -> str:
    if not results:
        return "none"
    lines: list[str] = []
    for result in results:
        lines.append(
            f"- {result.task_name}: "
            f"{_worker_result_summary(result, limit=DEFAULT_WORKER_RESULT_SUMMARY_CHARS)}"
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
    # F3.2 (2026-06-12): standard marker — downstream phases must see how
    # much context they lost, not just that "something" was cut.
    suffix = f"... [truncated: kept {limit} of {len(clean)} chars]"
    return _head_tail_compact(clean, limit=limit, suffix=suffix)


def _mechanical_compact_text(text: str, *, limit: int) -> str:
    clean = str(text or "")
    if len(clean) <= limit:
        return clean
    return _head_tail_compact(clean, limit=limit, suffix=MECHANICAL_TRUNCATION_SIGNATURE)


def _head_tail_compact(text: str, *, limit: int, suffix: str) -> str:
    if limit <= len(suffix):
        return suffix[:limit]
    available = limit - len(suffix)
    head_chars = min(HEAD_TAIL_PRESERVE_CHARS, available // 2)
    tail_chars = min(HEAD_TAIL_PRESERVE_CHARS, available - head_chars)
    if head_chars + tail_chars < available:
        head_chars = available - tail_chars
    head = text[:head_chars].rstrip()
    tail = text[-tail_chars:].lstrip() if tail_chars else ""
    if tail:
        return f"{head}{suffix}{tail}"
    return f"{head}{suffix}"


def _distillation_prompt(text: str, *, limit: int) -> str:
    return (
        "### Prompt: Destilación de Contexto Crítico\n\n"
        "Actúa como un analizador de infraestructura y optimizador de contexto. Tu único objetivo es "
        f"condensar el contenido técnico provisto para que ocupe estrictamente menos de **{limit}** "
        "caracteres, eliminando el ruido conversacional pero **PRESERVANDO INTACTOS** los siguientes "
        "elementos clave:\n"
        "1. Rumbos y rutas de archivos completas (`/Users/...`, `claw_v2/...`).\n"
        "2. Códigos de error específicos, tracebacks de excepciones y respuestas de la terminal.\n"
        "3. Asignaciones de variables, nombres de funciones y tokens de configuración no sensibles.\n\n"
        "**Formato de salida:** Estructura la información usando viñetas densas e hiper-específicas. "
        "Prioriza la causa raíz del fallo y los parámetros reales observados. Ve directo al grano, "
        "sin preámbulos ni introducciones coloniales.\n"
        "## Contenido Técnico a Condensar:\n\n"
        f"**{text}**"
    )


def _has_critical_worker_error(result: WorkerResult) -> bool:
    return CRITICAL_WORKER_MARKER in f"{result.content or ''}\n{result.error or ''}"


def _critical_worker_result(results: list[WorkerResult]) -> WorkerResult | None:
    return next((result for result in results if _has_critical_worker_error(result)), None)


def _critical_worker_audit(phase: str, result: WorkerResult) -> dict[str, Any]:
    raw_error = redact_text(
        "\n".join(
            part
            for part in (
                f"task_name: {result.task_name}",
                f"phase: {phase}",
                f"error: {result.error}" if result.error else "",
                result.content or "",
            )
            if part
        ),
        limit=0,
    )
    return {
        "critical_worker_error": True,
        "phase": phase,
        "task_name": result.task_name,
        "raw_error": raw_error,
        "degraded_compaction": result.degraded_compaction,
    }


def _phase_counts(results: list[WorkerResult]) -> dict[str, Any]:
    error_count = sum(1 for item in results if item.error)
    return {
        "worker_count": len(results),
        "error_count": error_count,
        "ok_count": len(results) - error_count,
    }


def _phase_all_workers_failed(results: list[WorkerResult]) -> bool:
    return bool(results) and all(bool(item.error) for item in results)


def _phase_worker_failure_error(phase: str, results: list[WorkerResult]) -> str:
    details = "; ".join(
        f"{item.task_name}: {item.error}" for item in results if item.error
    ).strip()
    if not details:
        details = "all workers failed without error details"
    return f"{phase}_phase_failed: {redact_text(details, limit=500)}"


def _f2_compact_payload(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value, limit=F2_PAYLOAD_TEXT_LIMIT)
    if isinstance(value, dict):
        return {str(key)[:80]: _f2_compact_payload(item) for key, item in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_f2_compact_payload(item) for item in list(value)[:20]]
    return redact_text(str(value), limit=F2_PAYLOAD_TEXT_LIMIT)
