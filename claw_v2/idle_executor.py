from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.bot_helpers import is_destructive_or_external_objective
from claw_v2.telemetry import append_jsonl, now_iso
from claw_v2.task_ledger import TERMINAL_STATUSES


IDLE_EXECUTOR_ENV_FLAG = "CLAW_IDLE_EXECUTOR_ENABLED"
IDLE_EXECUTOR_EVENT_FILE = "idle_executor.jsonl"
IDLE_EXECUTOR_MAX_STALL_COUNT = 3


@dataclass(frozen=True)
class IdleCandidate:
    source: str
    objective: str
    session_id: str
    task_id: str | None = None
    verification_status: str = "unknown"
    mode: str = "chat"
    status: str = "unknown"


@dataclass(frozen=True)
class IdleExecutorResult:
    candidate: IdleCandidate | None = None
    telemetry_only: bool = True
    advanced: bool = False
    circuit_broke: bool = False
    user_message: str | None = None
    event_names: tuple[str, ...] = field(default_factory=tuple)


class IdleOwnershipExecutor:
    """Wave 0 idle ownership executor.

    By default this is telemetry-only. When `CLAW_IDLE_EXECUTOR_ENABLED=1`, it
    can start safe pending local tasks or resume durable autonomous coordinator
    tasks, while preserving policy blocks and the stall circuit breaker.
    """

    def __init__(
        self,
        *,
        memory: Any,
        task_ledger: Any | None = None,
        job_service: Any | None = None,
        task_handler: Any | None = None,
        observe: Any | None = None,
        telemetry_root: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._memory = memory
        self._task_ledger = task_ledger
        self._job_service = job_service
        self._task_handler = task_handler
        self._observe = observe
        self._telemetry_root = Path(telemetry_root).expanduser() if telemetry_root else None
        self._env = env

    def inspect_turn(self, *, session_id: str) -> IdleExecutorResult:
        candidate = self._find_candidate(session_id=session_id)
        if candidate is None:
            return IdleExecutorResult()
        if is_destructive_or_external_objective(candidate.objective):
            self._emit(
                "idle_executor_blocked",
                {
                    "session_id": session_id,
                    "source": candidate.source,
                    "task_id": candidate.task_id,
                    "reason": "risky_or_external_objective",
                },
            )
            return IdleExecutorResult(candidate=candidate, event_names=("idle_executor_blocked",))
        circuit = self._maybe_circuit_break(candidate)
        if circuit is not None:
            return circuit
        payload = self._candidate_payload(candidate)
        self._emit("idle_executor_would_advance", payload)
        self._write_jsonl("idle_executor_would_advance", payload)
        if not self._enabled:
            return IdleExecutorResult(
                candidate=candidate,
                telemetry_only=True,
                event_names=("idle_executor_would_advance",),
            )
        return self._advance_candidate(candidate, previous_events=("idle_executor_would_advance",))

    @property
    def _enabled(self) -> bool:
        source = self._env if self._env is not None else os.environ
        return str(source.get(IDLE_EXECUTOR_ENV_FLAG, "0")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _find_candidate(self, *, session_id: str) -> IdleCandidate | None:
        state = self._safe_state(session_id)
        ledger_candidate = self._candidate_from_ledger(session_id)
        if ledger_candidate is not None:
            return ledger_candidate
        queue = state.get("task_queue") or []
        if isinstance(queue, list):
            for item in queue:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "").lower()
                if status not in {"pending", "in_progress", "running"}:
                    continue
                objective = str(item.get("summary") or "").strip()
                if not objective:
                    continue
                return IdleCandidate(
                    source="session_state.task_queue",
                    session_id=session_id,
                    task_id=_optional_str(item.get("task_id")),
                    objective=objective,
                    mode=str(item.get("mode") or state.get("mode") or "chat"),
                    verification_status=str(state.get("verification_status") or "unknown"),
                    status=status or "unknown",
                )
        pending = str(state.get("pending_action") or "").strip()
        if pending:
            return IdleCandidate(
                source="session_state.pending_action",
                session_id=session_id,
                objective=pending,
                mode=str(state.get("mode") or "chat"),
                verification_status=str(state.get("verification_status") or "unknown"),
                status="pending",
            )
        active_object = state.get("active_object") or {}
        if isinstance(active_object, dict):
            active_task = active_object.get("active_task") or {}
            if isinstance(active_task, dict):
                objective = str(active_task.get("objective") or "").strip()
                status = str(active_task.get("status") or "").lower()
                task_id = _optional_str(active_task.get("task_id"))
                if objective and status in {"pending", "running", "in_progress"}:
                    if task_id and self._stale_terminal_record(task_id) is not None:
                        self._clear_stale_active_task(session_id, task_id)
                        return None
                    return IdleCandidate(
                        source="session_state.active_task",
                        session_id=session_id,
                        task_id=task_id,
                        objective=objective,
                        mode=str(active_task.get("mode") or state.get("mode") or "chat"),
                        verification_status=str(state.get("verification_status") or "unknown"),
                        status=status or "unknown",
                    )
        _ = self._job_service  # reserved for the execution phase; keeps constructor stable.
        return None

    def _candidate_from_ledger(self, session_id: str) -> IdleCandidate | None:
        if self._task_ledger is None:
            return None
        try:
            records = self._task_ledger.list(
                session_id=session_id,
                statuses=("queued", "running"),
                limit=10,
            )
        except Exception:
            return None
        for record in records:
            metadata = dict(getattr(record, "metadata", {}) or {})
            if (
                getattr(record, "runtime", None) != "coordinator"
                or metadata.get("autonomous") is not True
            ):
                continue
            objective = str(getattr(record, "objective", "") or "").strip()
            if not objective:
                continue
            return IdleCandidate(
                source="agent_tasks",
                session_id=session_id,
                task_id=_optional_str(getattr(record, "task_id", None)),
                objective=objective,
                mode=str(getattr(record, "mode", "") or "chat"),
                verification_status=str(getattr(record, "verification_status", "") or "unknown"),
                status=str(getattr(record, "status", "") or "unknown"),
            )
        return None

    def _maybe_circuit_break(self, candidate: IdleCandidate) -> IdleExecutorResult | None:
        if not candidate.task_id:
            self._record_would_advance(candidate)
            return None
        state = self._safe_state(candidate.session_id)
        active_object = dict(state.get("active_object") or {})
        idle_state = dict(active_object.get("idle_executor") or {})
        if idle_state.get("advanced") is not True:
            self._record_would_advance(candidate)
            return None
        previous_task_id = idle_state.get("task_id")
        previous_verification = idle_state.get("verification_status")
        count = int(idle_state.get("unchanged_count") or 0)
        if (
            previous_task_id == candidate.task_id
            and previous_verification == candidate.verification_status
        ):
            next_count = count + 1
        else:
            next_count = 1
        if next_count < IDLE_EXECUTOR_MAX_STALL_COUNT:
            return None
        payload = {
            **self._candidate_payload(candidate),
            "reason": "idle_executor_stall",
            "unchanged_count": next_count,
        }
        self._suspend_candidate(candidate)
        self._emit("idle_executor_circuit_broke", payload)
        self._write_jsonl("idle_executor_circuit_broke", payload)
        return IdleExecutorResult(
            candidate=candidate,
            telemetry_only=not self._enabled,
            circuit_broke=True,
            user_message=(
                "Bloqueé el avance automático porque la misma tarea no cambió "
                "de verificación tras 3 intentos del idle executor."
            ),
            event_names=("idle_executor_circuit_broke",),
        )

    def _record_would_advance(self, candidate: IdleCandidate) -> None:
        state = self._safe_state(candidate.session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["idle_executor_last_candidate"] = {
            "task_id": candidate.task_id,
            "verification_status": candidate.verification_status,
            "source": candidate.source,
            "status": candidate.status,
            "updated_at": time.time(),
        }
        self._memory.update_session_state(candidate.session_id, active_object=active_object)

    def _record_advance_state(
        self, candidate: IdleCandidate, *, advanced: bool, result: str
    ) -> None:
        state = self._safe_state(candidate.session_id)
        active_object = dict(state.get("active_object") or {})
        previous = dict(active_object.get("idle_executor") or {})
        previous_task_id = previous.get("task_id")
        previous_verification = previous.get("verification_status")
        previous_advanced = previous.get("advanced") is True
        previous_count = int(previous.get("unchanged_count") or 0)
        if (
            advanced
            and previous_advanced
            and previous_task_id == candidate.task_id
            and previous_verification == candidate.verification_status
        ):
            count = previous_count + 1
        elif advanced:
            count = 1
        else:
            count = 0
        active_object["idle_executor"] = {
            "task_id": candidate.task_id,
            "verification_status": candidate.verification_status,
            "status": candidate.status,
            "source": candidate.source,
            "unchanged_count": count,
            "advanced": advanced,
            "result": result,
            "updated_at": time.time(),
        }
        self._memory.update_session_state(candidate.session_id, active_object=active_object)

    def _advance_candidate(
        self,
        candidate: IdleCandidate,
        *,
        previous_events: tuple[str, ...],
    ) -> IdleExecutorResult:
        if self._task_handler is None:
            return self._blocked_result(
                candidate,
                previous_events=previous_events,
                reason="task_handler_unavailable",
                message="No pude avanzar la tarea: el ejecutor de tareas no está disponible.",
            )
        if candidate.source == "agent_tasks":
            return self._resume_ledger_candidate(candidate, previous_events=previous_events)
        if candidate.source in {"session_state.task_queue", "session_state.pending_action"}:
            if candidate.source == "session_state.task_queue" and candidate.status != "pending":
                return self._blocked_result(
                    candidate,
                    previous_events=previous_events,
                    reason="non_durable_in_progress_queue_item",
                    message=(
                        "No avancé la cola automáticamente porque el item ya está en progreso "
                        "pero no encontré una tarea durable de coordinator para retomarlo."
                    ),
                )
            return self._start_candidate(candidate, previous_events=previous_events)
        return self._blocked_result(
            candidate,
            previous_events=previous_events,
            reason="unsupported_candidate_source",
            message=f"No pude avanzar la tarea: fuente no soportada ({candidate.source}).",
        )

    def _resume_ledger_candidate(
        self,
        candidate: IdleCandidate,
        *,
        previous_events: tuple[str, ...],
    ) -> IdleExecutorResult:
        if not candidate.task_id:
            return self._blocked_result(
                candidate,
                previous_events=previous_events,
                reason="missing_task_id",
                message="No pude retomar la tarea: falta task_id durable.",
            )
        resume = getattr(self._task_handler, "resume_idle_autonomous_task", None)
        if not callable(resume):
            return self._blocked_result(
                candidate,
                previous_events=previous_events,
                reason="idle_resume_unavailable",
                message="No pude retomar la tarea: el TaskHandler no expone reanudación idle.",
            )
        try:
            result = resume(candidate.session_id, candidate.task_id)
        except Exception as exc:
            return self._blocked_result(
                candidate,
                previous_events=previous_events,
                reason="idle_resume_failed",
                message=f"No pude retomar la tarea: {exc}",
            )
        return self._result_from_advance_response(
            candidate, result, previous_events=previous_events
        )

    def _start_candidate(
        self,
        candidate: IdleCandidate,
        *,
        previous_events: tuple[str, ...],
    ) -> IdleExecutorResult:
        start = getattr(self._task_handler, "start_autonomous_task", None)
        if not callable(start):
            return self._blocked_result(
                candidate,
                previous_events=previous_events,
                reason="idle_start_unavailable",
                message="No pude iniciar la tarea: el TaskHandler no expone start_autonomous_task.",
            )
        try:
            response = start(
                candidate.session_id,
                candidate.objective,
                mode=candidate.mode if candidate.mode in {"coding", "research"} else None,
                source_text=f"idle_executor:{candidate.source}",
                task_kind="idle_executor_advance",
                risk_tier="tier_1",
                delegation_metadata={
                    "source": "idle_executor",
                    "candidate_source": candidate.source,
                },
            )
        except Exception as exc:
            return self._blocked_result(
                candidate,
                previous_events=previous_events,
                reason="idle_start_failed",
                message=f"No pude iniciar la tarea: {exc}",
            )
        advanced = isinstance(response, str) and "Tarea autónoma iniciada" in response
        payload = {
            **self._candidate_payload(candidate),
            "result": "started" if advanced else "not_started",
            "response_preview": str(response)[:500],
            "telemetry_only": False,
        }
        event_name = "idle_executor_did_advance" if advanced else "idle_executor_blocked"
        reason = "started_autonomous_task" if advanced else "start_autonomous_task_rejected"
        payload["reason"] = reason
        self._record_advance_state(candidate, advanced=advanced, result=reason)
        self._emit(event_name, payload)
        self._write_jsonl(event_name, payload)
        return IdleExecutorResult(
            candidate=candidate,
            telemetry_only=False,
            advanced=advanced,
            user_message=str(response),
            event_names=(*previous_events, event_name),
        )

    def _result_from_advance_response(
        self,
        candidate: IdleCandidate,
        result: Any,
        *,
        previous_events: tuple[str, ...],
    ) -> IdleExecutorResult:
        if isinstance(result, dict):
            advanced = bool(result.get("advanced"))
            reason = str(result.get("reason") or ("resumed" if advanced else "not_resumed"))
            message = str(result.get("message") or "")
        else:
            text = str(result)
            advanced = "reanud" in text.lower() or "resum" in text.lower()
            reason = "resumed" if advanced else "not_resumed"
            message = text
        event_name = "idle_executor_did_advance" if advanced else "idle_executor_noop"
        payload = {
            **self._candidate_payload(candidate),
            "reason": reason,
            "message_preview": message[:500],
            "telemetry_only": False,
        }
        self._record_advance_state(candidate, advanced=advanced, result=reason)
        self._emit(event_name, payload)
        self._write_jsonl(event_name, payload)
        return IdleExecutorResult(
            candidate=candidate,
            telemetry_only=False,
            advanced=advanced,
            user_message=message or None,
            event_names=(*previous_events, event_name),
        )

    def _blocked_result(
        self,
        candidate: IdleCandidate,
        *,
        previous_events: tuple[str, ...] = (),
        reason: str,
        message: str,
    ) -> IdleExecutorResult:
        payload = {
            **self._candidate_payload(candidate),
            "reason": reason,
            "message_preview": message[:500],
            "telemetry_only": False,
        }
        self._record_advance_state(candidate, advanced=False, result=reason)
        self._emit("idle_executor_blocked", payload)
        self._write_jsonl("idle_executor_blocked", payload)
        return IdleExecutorResult(
            candidate=candidate,
            telemetry_only=False,
            advanced=False,
            user_message=message,
            event_names=(*previous_events, "idle_executor_blocked"),
        )

    def _suspend_candidate(self, candidate: IdleCandidate) -> None:
        state = self._safe_state(candidate.session_id)
        queue = state.get("task_queue") or []
        if isinstance(queue, list):
            updated_queue: list[dict[str, Any]] = []
            for item in queue:
                if not isinstance(item, dict):
                    updated_queue.append(item)
                    continue
                if candidate.task_id and item.get("task_id") == candidate.task_id:
                    updated_queue.append(
                        {
                            **item,
                            "status": "blocked",
                            "blocked_reason": "idle_executor_stall",
                        }
                    )
                else:
                    updated_queue.append(item)
            self._memory.update_session_state(
                candidate.session_id,
                task_queue=updated_queue,
                verification_status="blocked",
            )
        if self._task_ledger is not None and candidate.task_id:
            try:
                self._task_ledger.mark_terminal(
                    candidate.task_id,
                    status="failed",
                    summary=f"Idle executor stalled: {candidate.objective[:180]}",
                    error="idle_executor_stall",
                    verification_status="blocked",
                    artifacts={"idle_executor": self._candidate_payload(candidate)},
                )
            except Exception:
                return

    def _safe_state(self, session_id: str) -> dict[str, Any]:
        try:
            state = self._memory.get_session_state(session_id)
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    def _stale_terminal_record(self, task_id: str) -> Any | None:
        if self._task_ledger is None:
            return None
        get = getattr(self._task_ledger, "get", None)
        if not callable(get):
            return None
        try:
            record = get(task_id)
        except Exception:
            return None
        status = str(getattr(record, "status", "") or "").lower() if record is not None else ""
        return record if status in TERMINAL_STATUSES else None

    def _clear_stale_active_task(self, session_id: str, task_id: str) -> None:
        state = self._safe_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_task = active_object.get("active_task") or {}
        if not isinstance(active_task, dict) or str(active_task.get("task_id") or "") != task_id:
            return
        merge_active_object = getattr(self._memory, "merge_active_object", None)
        try:
            if callable(merge_active_object):
                merge_active_object(session_id, {}, remove=("active_task",))
            else:
                active_object.pop("active_task", None)
                self._memory.update_session_state(session_id, active_object=active_object)
        except Exception:
            return
        self._emit(
            "idle_executor_stale_active_task_cleared",
            {
                "session_id": session_id,
                "task_id": task_id,
                "reason": "ledger_terminal",
            },
        )

    def _candidate_payload(self, candidate: IdleCandidate) -> dict[str, Any]:
        return {
            "event": "idle_executor",
            "ts": now_iso(),
            "session_id": candidate.session_id,
            "source": candidate.source,
            "task_id": candidate.task_id,
            "objective_preview": candidate.objective[:240],
            "mode": candidate.mode,
            "verification_status": candidate.verification_status,
            "status": candidate.status,
            "enabled": self._enabled,
            "telemetry_only": not self._enabled,
        }

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if self._observe is None:
            return
        try:
            self._observe.emit(event_name, payload=payload)
        except Exception:
            return

    def _write_jsonl(self, event_name: str, payload: dict[str, Any]) -> None:
        if self._telemetry_root is None:
            return
        append_jsonl(
            self._telemetry_root / IDLE_EXECUTOR_EVENT_FILE,
            {"event_type": event_name, **payload},
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
