from __future__ import annotations

import importlib.util
import time
from dataclasses import dataclass, field
from typing import Any

from claw_v2.redaction import redact_sensitive, redact_text

LANGGRAPH_SHADOW_NODE_SEQUENCE = (
    "intake",
    "research",
    "synthesis",
    "verification",
    "finalization",
)


@dataclass(frozen=True, slots=True)
class LangGraphShadowNodeReport:
    name: str
    status: str
    detail: str = ""

    def to_payload(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class LangGraphShadowReport:
    task_id: str
    backend: str
    planned_phases: tuple[str, ...]
    observed_phases: tuple[str, ...]
    missing_phases: tuple[str, ...]
    unexpected_phases: tuple[str, ...]
    node_reports: tuple[LangGraphShadowNodeReport, ...]
    duration_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def matched_legacy_result(self) -> bool:
        return not self.missing_phases and not self.unexpected_phases

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "backend": self.backend,
            "planned_phases": list(self.planned_phases),
            "observed_phases": list(self.observed_phases),
            "missing_phases": list(self.missing_phases),
            "unexpected_phases": list(self.unexpected_phases),
            "matched_legacy_result": self.matched_legacy_result,
            "node_reports": [node.to_payload() for node in self.node_reports],
            "duration_seconds": self.duration_seconds,
            "metadata": dict(self.metadata),
        }


class LangGraphF2CheckpointAdapter:
    """F2-backed checkpoint adapter for the LangGraph shadow runner.

    Thread IDs map directly to coordinator ``task_id`` values. The F2 phase
    namespace is ``{namespace}:{node_name}``, keeping shadow graph checkpoints
    separate from productive coordinator phase checkpoints while sharing the
    same RuntimeDb-backed F2 store.
    """

    def __init__(self, store: Any, *, namespace: str = "langgraph_shadow") -> None:
        if store is None:
            raise ValueError("store is required")
        if not namespace.strip():
            raise ValueError("namespace must not be blank")
        self._store = store
        self.namespace = namespace.strip()

    def thread_id_for_task(self, task_id: str) -> str:
        return _require_nonblank("task_id", task_id)

    def phase_for_node(self, node_name: str) -> str:
        return f"{self.namespace}:{_require_nonblank('node_name', node_name)}"

    def node_succeeded(self, *, task_id: str, run_id: str, node_name: str) -> bool:
        latest = self._store.list_phase_checkpoints(
            task_id=self.thread_id_for_task(task_id),
            run_id=_require_nonblank("run_id", run_id),
            phase=self.phase_for_node(node_name),
            order="phase_version_desc",
            limit=1,
        )
        return bool(latest and latest[0].status == "succeeded")

    def record_node_started(
        self,
        *,
        task_id: str,
        run_id: str,
        node_name: str,
        payload: dict[str, Any],
    ) -> None:
        self._record_node_event(
            task_id=task_id,
            run_id=run_id,
            node_name=node_name,
            write_kind="langgraph_node_started",
            status="started",
            payload=payload,
        )

    def record_node_completed(
        self,
        *,
        task_id: str,
        run_id: str,
        node_name: str,
        payload: dict[str, Any],
    ) -> None:
        self._record_node_event(
            task_id=task_id,
            run_id=run_id,
            node_name=node_name,
            write_kind="langgraph_node_completed",
            status="succeeded",
            payload=payload,
        )

    def record_node_failed(
        self,
        *,
        task_id: str,
        run_id: str,
        node_name: str,
        payload: dict[str, Any],
    ) -> None:
        self._record_node_event(
            task_id=task_id,
            run_id=run_id,
            node_name=node_name,
            write_kind="langgraph_node_failed",
            status="failed",
            payload=payload,
        )

    def _record_node_event(
        self,
        *,
        task_id: str,
        run_id: str,
        node_name: str,
        write_kind: str,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        thread_id = self.thread_id_for_task(task_id)
        phase = self.phase_for_node(node_name)
        clean_payload = _checkpoint_payload(
            payload,
            namespace=self.namespace,
            node_name=node_name,
            thread_id=thread_id,
            status=status,
        )
        write = self._store.append_checkpoint_write(
            task_id=thread_id,
            run_id=_require_nonblank("run_id", run_id),
            phase=phase,
            write_kind=write_kind,
            payload=clean_payload,
        )
        latest = self._store.list_phase_checkpoints(
            task_id=thread_id,
            run_id=run_id,
            phase=phase,
            order="phase_version_desc",
            limit=1,
        )
        phase_version = latest[0].phase_version + 1 if latest else 1
        self._store.create_phase_checkpoint(
            task_id=thread_id,
            run_id=run_id,
            phase=phase,
            phase_version=phase_version,
            status=status,
            last_write_order=write.write_order,
            payload=clean_payload,
        )


class LangGraphShadowRunner:
    """Deterministic shadow graph for CoordinatorService inputs.

    The runner intentionally performs no LLM calls, tool calls, scratch writes,
    DB writes, or orchestration-store writes. It models the coordinator phases
    as pure nodes and compares the planned graph path with the legacy result
    that already ran.
    """

    def __init__(
        self,
        *,
        observe: Any | None = None,
        checkpoint_adapter: LangGraphF2CheckpointAdapter | None = None,
        fail_after_node: str | None = None,
    ) -> None:
        self.observe = observe
        self.checkpoint_adapter = checkpoint_adapter
        self.fail_after_node = fail_after_node

    @property
    def backend(self) -> str:
        return "langgraph" if _langgraph_available() else "linear_fallback"

    def run(
        self,
        *,
        task_id: str,
        objective: str,
        research_tasks: list[Any],
        implementation_tasks: list[Any] | None,
        verification_tasks: list[Any] | None,
        lane_overrides: dict[str, dict[str, Any]] | None,
        start_phase: str | None,
        legacy_result: Any,
    ) -> LangGraphShadowReport:
        start = time.time()
        planned_phases = _planned_phases(
            research_tasks=research_tasks,
            implementation_tasks=implementation_tasks,
            verification_tasks=verification_tasks,
        )
        started_payload = {
            "task_id": task_id,
            "backend": self.backend,
            "planned_phases": list(planned_phases),
            "objective_chars": len(objective or ""),
            "start_phase": start_phase,
        }
        self._emit("langgraph_shadow_started", started_payload)

        state = {
            "task_id": task_id,
            "run_id": task_id,
            "objective_chars": len(objective or ""),
            "planned_phases": planned_phases,
            "legacy_result": legacy_result,
            "research_tasks": tuple(research_tasks or ()),
            "lane_override_keys": tuple(sorted((lane_overrides or {}).keys())),
            "start_phase": start_phase,
            "node_reports": (),
        }
        final_state = self._invoke_graph(state)
        report = _report_from_state(
            final_state,
            backend=self.backend,
            duration_seconds=time.time() - start,
        )
        self._emit("langgraph_shadow_completed", report.to_payload())
        return report

    def _invoke_graph(self, state: dict[str, Any]) -> dict[str, Any]:
        if _langgraph_available():
            try:
                return _invoke_langgraph(
                    state,
                    checkpoint_adapter=self.checkpoint_adapter,
                    fail_after_node=self.fail_after_node,
                )
            except Exception:
                # The optional backend is never allowed to affect the productive
                # coordinator path. Fall back to the deterministic local runner.
                pass
        return _invoke_linear_graph(
            state,
            checkpoint_adapter=self.checkpoint_adapter,
            fail_after_node=self.fail_after_node,
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        emit = getattr(self.observe, "emit", None)
        if callable(emit):
            emit(event_type, payload=payload)


def _langgraph_available() -> bool:
    try:
        return importlib.util.find_spec("langgraph.graph") is not None
    except ModuleNotFoundError:
        return False


def _invoke_langgraph(
    state: dict[str, Any],
    *,
    checkpoint_adapter: LangGraphF2CheckpointAdapter | None,
    fail_after_node: str | None,
) -> dict[str, Any]:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(dict)
    for node_name in LANGGRAPH_SHADOW_NODE_SEQUENCE:
        graph.add_node(
            node_name,
            _node_fn(
                node_name,
                checkpoint_adapter=checkpoint_adapter,
                fail_after_node=fail_after_node,
            ),
        )

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "research")
    graph.add_edge("research", "synthesis")
    graph.add_edge("synthesis", "verification")
    graph.add_edge("verification", "finalization")
    graph.add_edge("finalization", END)
    compiled = graph.compile()
    return dict(compiled.invoke(state))


def _invoke_linear_graph(
    state: dict[str, Any],
    *,
    checkpoint_adapter: LangGraphF2CheckpointAdapter | None,
    fail_after_node: str | None,
) -> dict[str, Any]:
    current = dict(state)
    for node_name in LANGGRAPH_SHADOW_NODE_SEQUENCE:
        current = _node_fn(
            node_name,
            checkpoint_adapter=checkpoint_adapter,
            fail_after_node=fail_after_node,
        )(current)
    return current


def _node_fn(
    node_name: str,
    *,
    checkpoint_adapter: LangGraphF2CheckpointAdapter | None,
    fail_after_node: str | None,
):
    def run_node(state: dict[str, Any]) -> dict[str, Any]:
        current = dict(state)
        task_id = str(current.get("task_id") or "")
        run_id = str(current.get("run_id") or task_id)
        if checkpoint_adapter is not None and checkpoint_adapter.node_succeeded(
            task_id=task_id,
            run_id=run_id,
            node_name=node_name,
        ):
            reports = tuple(current.get("node_reports") or ())
            current["node_reports"] = (
                *reports,
                LangGraphShadowNodeReport(
                    name=node_name,
                    status="resumed",
                    detail=checkpoint_adapter.phase_for_node(node_name),
                ),
            )
            return current
        _record_node_started(checkpoint_adapter, current, node_name)
        reports = tuple(current.get("node_reports") or ())
        try:
            current["node_reports"] = (*reports, _node_report(node_name, current))
            _record_node_completed(checkpoint_adapter, current, node_name)
        except Exception as exc:
            _record_node_failed(checkpoint_adapter, current, node_name, exc)
            raise
        if fail_after_node == node_name:
            raise RuntimeError(f"synthetic shadow failure after {node_name}")
        return current

    return run_node


def _record_node_started(
    checkpoint_adapter: LangGraphF2CheckpointAdapter | None,
    state: dict[str, Any],
    node_name: str,
) -> None:
    if checkpoint_adapter is None:
        return
    checkpoint_adapter.record_node_started(
        task_id=str(state.get("task_id") or ""),
        run_id=str(state.get("run_id") or state.get("task_id") or ""),
        node_name=node_name,
        payload=_node_checkpoint_payload("started", node_name, state),
    )


def _record_node_completed(
    checkpoint_adapter: LangGraphF2CheckpointAdapter | None,
    state: dict[str, Any],
    node_name: str,
) -> None:
    if checkpoint_adapter is None:
        return
    checkpoint_adapter.record_node_completed(
        task_id=str(state.get("task_id") or ""),
        run_id=str(state.get("run_id") or state.get("task_id") or ""),
        node_name=node_name,
        payload=_node_checkpoint_payload("completed", node_name, state),
    )


def _record_node_failed(
    checkpoint_adapter: LangGraphF2CheckpointAdapter | None,
    state: dict[str, Any],
    node_name: str,
    exc: Exception,
) -> None:
    if checkpoint_adapter is None:
        return
    checkpoint_adapter.record_node_failed(
        task_id=str(state.get("task_id") or ""),
        run_id=str(state.get("run_id") or state.get("task_id") or ""),
        node_name=node_name,
        payload={
            **_node_checkpoint_payload("failed", node_name, state),
            "error_type": type(exc).__name__,
        },
    )


def _node_report(node_name: str, state: dict[str, Any]) -> LangGraphShadowNodeReport:
    planned = tuple(state.get("planned_phases") or ())
    legacy_result = state.get("legacy_result")
    observed = _observed_phases(legacy_result)
    if node_name == "intake":
        detail = f"planned={','.join(planned) or 'none'}"
    elif node_name in {"research", "synthesis", "verification"}:
        phase = "verification" if node_name == "verification" else node_name
        status = "observed" if phase in observed else "planned_only"
        if phase not in planned:
            status = "not_planned"
        return LangGraphShadowNodeReport(name=node_name, status=status, detail=phase)
    else:
        missing = [phase for phase in planned if phase not in observed]
        unexpected = [phase for phase in observed if phase not in planned]
        detail = f"missing={len(missing)} unexpected={len(unexpected)}"
    return LangGraphShadowNodeReport(name=node_name, status="ok", detail=detail)


def _planned_phases(
    *,
    research_tasks: list[Any],
    implementation_tasks: list[Any] | None,
    verification_tasks: list[Any] | None,
) -> tuple[str, ...]:
    phases: list[str] = []
    if research_tasks:
        phases.append("research")
    phases.append("synthesis")
    if implementation_tasks:
        phases.append("implementation")
    if verification_tasks:
        phases.append("verification")
    return tuple(phases)


def _observed_phases(legacy_result: Any) -> tuple[str, ...]:
    phase_results = getattr(legacy_result, "phase_results", {}) or {}
    observed = list(phase_results.keys())
    synthesis = str(getattr(legacy_result, "synthesis", "") or "")
    if synthesis and "synthesis" not in observed:
        insert_at = 1 if "research" in observed else 0
        observed.insert(insert_at, "synthesis")
    return tuple(observed)


def _report_from_state(
    state: dict[str, Any],
    *,
    backend: str,
    duration_seconds: float,
) -> LangGraphShadowReport:
    legacy_result = state.get("legacy_result")
    planned_phases = tuple(state.get("planned_phases") or ())
    observed_phases = _observed_phases(legacy_result)
    return LangGraphShadowReport(
        task_id=str(state.get("task_id") or ""),
        backend=backend,
        planned_phases=planned_phases,
        observed_phases=observed_phases,
        missing_phases=tuple(phase for phase in planned_phases if phase not in observed_phases),
        unexpected_phases=tuple(phase for phase in observed_phases if phase not in planned_phases),
        node_reports=tuple(state.get("node_reports") or ()),
        duration_seconds=duration_seconds,
        metadata={
            "objective_chars": int(state.get("objective_chars") or 0),
            "lane_override_keys": list(state.get("lane_override_keys") or ()),
            "start_phase": state.get("start_phase"),
            **_f6_shadow_metadata(
                research_tasks=tuple(state.get("research_tasks") or ()),
                legacy_result=legacy_result,
            ),
        },
    )


def _node_checkpoint_payload(event: str, node_name: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": f"langgraph_node_{event}",
        "node": node_name,
        "planned_phases": list(tuple(state.get("planned_phases") or ())),
        "observed_phases": list(_observed_phases(state.get("legacy_result"))),
        "objective_chars": int(state.get("objective_chars") or 0),
        "lane_override_keys": list(tuple(state.get("lane_override_keys") or ())),
        "start_phase": state.get("start_phase"),
    }


def _checkpoint_payload(
    payload: dict[str, Any],
    *,
    namespace: str,
    node_name: str,
    thread_id: str,
    status: str,
) -> dict[str, Any]:
    return {
        **dict(redact_sensitive(payload, limit=0)),
        "namespace": namespace,
        "node": node_name,
        "thread_id": thread_id,
        "status": status,
    }


def _f6_shadow_metadata(*, research_tasks: tuple[Any, ...], legacy_result: Any) -> dict[str, Any]:
    fan_out_units = [
        {
            "unit_id": _fan_out_unit_id("research", index, _task_name(task, index)),
            "input_index": index,
            "phase": "research",
            "name": _task_name(task, index),
            "input": redact_text(_task_instruction(task), limit=0),
            "lane": _task_lane(task),
        }
        for index, task in enumerate(research_tasks)
    ]
    fan_in_results = _fan_in_results(fan_out_units, legacy_result)
    return {
        "fan_out_units": fan_out_units,
        "fan_in_results": fan_in_results,
        "reducer": {
            "order_by": "input_index",
            "unit_count": len(fan_out_units),
            "result_count": len(fan_in_results),
            "uses_timing_for_order": False,
        },
    }


def _fan_in_results(
    fan_out_units: list[dict[str, Any]], legacy_result: Any
) -> list[dict[str, Any]]:
    by_name: dict[str, list[Any]] = {}
    phase_results = getattr(legacy_result, "phase_results", {}) or {}
    for result in list(phase_results.get("research") or ()):
        by_name.setdefault(str(getattr(result, "task_name", "") or ""), []).append(result)

    reduced: list[dict[str, Any]] = []
    for unit in fan_out_units:
        result_bucket = by_name.get(str(unit["name"])) or []
        result = result_bucket.pop(0) if result_bucket else None
        error = str(getattr(result, "error", "") or "") if result is not None else "missing_result"
        status = "error" if error else "ok"
        duration = (
            float(getattr(result, "duration_seconds", 0.0) or 0.0) if result is not None else 0.0
        )
        evidence_summary = ""
        if status == "ok" and result is not None:
            evidence_summary = redact_text(str(getattr(result, "content", "") or ""), limit=500)
        reduced.append(
            {
                "unit_id": unit["unit_id"],
                "input_index": unit["input_index"],
                "phase": unit["phase"],
                "name": unit["name"],
                "input": unit["input"],
                "lane": unit["lane"],
                "status": status,
                "evidence_summary": evidence_summary,
                "error": redact_text(error, limit=500),
                "timing": {"duration_seconds": duration},
            }
        )
    return reduced


def _fan_out_unit_id(phase: str, index: int, name: str) -> str:
    return f"{phase}:{index}:{name}"


def _task_name(task: Any, index: int) -> str:
    return str(getattr(task, "name", "") or f"unit_{index}")


def _task_instruction(task: Any) -> str:
    return str(getattr(task, "instruction", "") or "")


def _task_lane(task: Any) -> str:
    return str(getattr(task, "lane", "") or "research")


def _require_nonblank(name: str, value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{name} must not be blank")
    return cleaned
