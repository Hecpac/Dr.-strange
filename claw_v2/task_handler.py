from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from claw_v2.artifacts import (
    ExecutionArtifact,
    JobArtifact,
    OutcomeArtifact,
    PlanArtifact,
    VerificationArtifact,
    append_lifecycle_artifacts,
    new_artifact_id,
    planned_phases_for_mode,
)
from claw_v2.bot_helpers import (
    _build_coordinator_tasks,
    _coordinator_checkpoint,
    _evaluate_autonomy_policy,
    _extract_option_reference,
    _format_autonomy_policy_block,
    _format_coordinator_response,
    _format_task_approval_response,
    _infer_session_mode,
    _looks_like_proceed_request,
    _stable_task_id,
    _task_approval_summary,
)
from claw_v2.model_registry import model_overrides_from_state


class TaskHandler:
    def __init__(
        self,
        *,
        approvals: Any | None = None,
        coordinator: Any | None = None,
        observe: Any | None = None,
        task_ledger: Any | None = None,
        job_service: Any | None = None,
        get_session_state: Callable[[str], dict[str, Any]],
        update_session_state: Callable[..., Any],
        store_message: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self.approvals = approvals
        self.coordinator = coordinator
        self.observe = observe
        self.task_ledger = task_ledger
        self.job_service = job_service
        self._get_session_state = get_session_state
        self._update_session_state = update_session_state
        self._store_message = store_message
        self._task_threads: dict[str, threading.Thread] = {}
        self._cancelled_tasks: set[str] = set()
        self._task_lock = threading.Lock()

    def task_approve_response(self, approval_id: str, token: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            valid = self.approvals.approve(approval_id, token)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        if not valid:
            return "approval rejected"
        payload = self.approvals.read(approval_id)
        metadata = payload.get("metadata", {})
        if metadata.get("kind") != "coordinated_task":
            return "approval recorded"
        session_id = metadata.get("session_id")
        objective = metadata.get("objective")
        approved_actions = metadata.get("approved_actions") or []
        if not isinstance(session_id, str) or not isinstance(objective, str):
            return "approval recorded, but task metadata is incomplete"
        self._remove_pending_task_approval(session_id, approval_id)
        return self.coordinated_task_response(
            session_id,
            objective,
            forced=True,
            approved_actions=tuple(str(action) for action in approved_actions),
        )

    def task_abort_response(self, approval_id: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            payload = self.approvals.read(approval_id)
            self.approvals.reject(approval_id)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        metadata = payload.get("metadata", {})
        if metadata.get("kind") != "coordinated_task":
            return "task rejected"
        session_id = metadata.get("session_id")
        if isinstance(session_id, str):
            self._remove_pending_task_approval(session_id, approval_id)
            self._update_session_state(
                session_id,
                pending_action=None,
                verification_status="blocked",
                last_checkpoint={
                    "summary": "Coordinated task rejected before approval.",
                    "verification_status": "blocked",
                    "reason": "task_rejected",
                },
            )
        return "coordinated task rejected"

    def maybe_run_coordinated_task(self, session_id: str, text: str) -> str | None:
        if self.coordinator is None or not text or text.startswith("/"):
            return None
        state = self._get_session_state(session_id)
        if state.get("autonomy_mode") != "autonomous":
            return None
        if _extract_option_reference(text) is not None or _looks_like_proceed_request(text):
            return None
        mode = _infer_session_mode(text)
        policy = _evaluate_autonomy_policy(
            text,
            mode=mode,
            forced=False,
            autonomy_mode=str(state.get("autonomy_mode") or "assisted"),
        )
        if not policy["allowed"] and policy["reason"] == "sensitive_action":
            self._update_session_state(
                session_id,
                last_checkpoint={
                    "summary": policy["summary"],
                    "verification_status": "blocked",
                    "reason": policy["reason"],
                },
                verification_status="blocked",
                pending_action=None,
            )
            return _format_autonomy_policy_block(policy)
        if mode not in {"coding", "research"}:
            return None
        return self.start_autonomous_task(session_id, text, mode=mode)

    def start_autonomous_task(self, session_id: str, objective: str, *, mode: str | None = None) -> str:
        if self.coordinator is None:
            return "coordinator unavailable"
        mode = mode or _infer_session_mode(objective)
        task_id = f"{session_id}:{time.time_ns()}"
        checkpoint = {
            "summary": f"Autonomous task started: {objective[:180]}",
            "verification_status": "running",
            "task_id": task_id,
        }
        state = self._get_session_state(session_id)
        queue = self.upsert_task_queue_entry(
            state.get("task_queue") or [],
            summary=objective,
            mode=mode,
            status="in_progress",
            source="coordinator",
            priority=0,
            depends_on=self.derive_task_dependencies(state.get("task_queue") or [], summary=objective),
        )
        active_object = dict(state.get("active_object") or {})
        active_object["active_task"] = {
            "task_id": task_id,
            "objective": objective,
            "mode": mode,
            "status": "running",
            "started_at": time.time(),
        }
        self._update_session_state(
            session_id,
            mode=mode,
            pending_action=None,
            task_queue=queue,
            verification_status="running",
            last_checkpoint=checkpoint,
            active_object=active_object,
        )
        self._emit(
            "autonomous_task_started",
            {
                "session_id": session_id,
                "task_id": task_id,
                "objective": objective,
                "mode": mode,
            },
        )
        self._record_ledger_task_started(
            task_id=task_id,
            session_id=session_id,
            objective=objective,
            mode=mode,
            route=active_object.get("last_channel_route") if isinstance(active_object.get("last_channel_route"), dict) else {},
        )
        job_id = self._enqueue_autonomous_job(
            task_id=task_id,
            session_id=session_id,
            objective=objective,
            mode=mode,
            route=active_object.get("last_channel_route") if isinstance(active_object.get("last_channel_route"), dict) else {},
            reason="task_started",
        )
        thread = threading.Thread(
            target=self._run_autonomous_task,
            args=(session_id, task_id, objective, mode, job_id),
            daemon=True,
            name=f"autonomous-task-{task_id[-8:]}",
        )
        with self._task_lock:
            self._task_threads[task_id] = thread
        thread.start()
        return (
            f"Tarea autónoma iniciada: `{task_id}`\n"
            f"Modo: {mode}\n"
            "Estado: running\n"
            "Voy a ejecutar, verificar y cerrar con reporte final. Usa `/task_loop` para ver el estado."
        )

    def coordinated_task_response(
        self,
        session_id: str,
        objective: str,
        *,
        forced: bool,
        approved_actions: tuple[str, ...] = (),
    ) -> str:
        if self.coordinator is None:
            return "coordinator unavailable"
        mode = _infer_session_mode(objective)
        state = self._get_session_state(session_id)
        policy = _evaluate_autonomy_policy(
            objective,
            mode=mode,
            forced=forced,
            autonomy_mode=str(state.get("autonomy_mode") or "assisted"),
            approved_actions=approved_actions,
        )
        if not policy["allowed"]:
            if policy["reason"] == "approval_required_action":
                approval_actions = tuple(str(action) for action in policy.get("matched_approval_actions", ()))
                pending = self.approvals.create(
                    action="coordinated_task",
                    summary=_task_approval_summary(objective, approval_actions=approval_actions),
                    metadata={
                        "kind": "coordinated_task",
                        "session_id": session_id,
                        "objective": objective,
                        "mode": mode,
                        "forced": forced,
                        "approved_actions": list(approval_actions),
                    },
                )
                self._update_session_state(
                    session_id,
                    pending_action=f"/task_approve {pending.approval_id} <token>",
                    verification_status="awaiting_approval",
                    pending_approvals=self._updated_pending_task_approvals(
                        session_id,
                        {
                            "approval_id": pending.approval_id,
                            "action": "coordinated_task",
                            "summary": pending.summary,
                            "approve_command": f"/task_approve {pending.approval_id} {pending.token}",
                            "abort_command": f"/task_abort {pending.approval_id}",
                        },
                    ),
                    last_checkpoint={
                        "summary": str(policy["summary"]),
                        "verification_status": "awaiting_approval",
                        "reason": str(policy["reason"]),
                        "approval_id": pending.approval_id,
                    },
                )
                return _format_task_approval_response(policy, pending)
            self._update_session_state(
                session_id,
                last_checkpoint={
                    "summary": policy["summary"],
                    "verification_status": "blocked",
                    "reason": policy["reason"],
                },
                verification_status="blocked",
                pending_action=None,
            )
            return _format_autonomy_policy_block(policy)
        task_id = f"{session_id}:{time.time_ns()}"
        return self._run_coordinated_task(
            session_id,
            objective,
            mode=mode,
            forced=forced,
            task_id=task_id,
        )

    def _run_coordinated_task(
        self,
        session_id: str,
        objective: str,
        *,
        mode: str,
        forced: bool,
        task_id: str,
    ) -> str:
        research_tasks, implementation_tasks, verification_tasks = _build_coordinator_tasks(mode, objective)
        result = self.coordinator.run(
            task_id,
            objective,
            research_tasks,
            implementation_tasks=implementation_tasks,
            verification_tasks=verification_tasks,
            lane_overrides=self._lane_model_overrides(session_id),
        )
        checkpoint = _coordinator_checkpoint(result, objective=objective)
        current_queue = self._get_session_state(session_id).get("task_queue") or []
        self._update_session_state(
            session_id,
            mode=mode,
            pending_action=checkpoint.get("pending_action"),
            task_queue=self.upsert_task_queue_entry(
                current_queue,
                summary=checkpoint.get("pending_action") or checkpoint.get("summary") or objective,
                mode=mode,
                status="pending" if checkpoint.get("pending_action") else checkpoint.get("verification_status", "unknown"),
                source="coordinator",
                priority=0,
                depends_on=self.derive_task_dependencies(
                    self._get_session_state(session_id).get("task_queue") or [],
                    summary=checkpoint.get("pending_action") or checkpoint.get("summary") or objective,
                ),
            ) if checkpoint.get("pending_action") or checkpoint.get("summary") else current_queue,
            verification_status=checkpoint.get("verification_status", "unknown"),
            last_checkpoint=checkpoint,
        )
        return _format_coordinator_response(result, checkpoint=checkpoint, forced=forced)

    def _run_autonomous_task(
        self,
        session_id: str,
        task_id: str,
        objective: str,
        mode: str,
        job_id: str | None = None,
    ) -> None:
        try:
            if self._is_cancelled(task_id):
                self._mark_cancelled_task_state(session_id, task_id, objective, reason="cancelled_before_start")
                self._cancel_autonomous_job(task_id, reason="cancelled_before_start", job_id=job_id)
                return
            if not self._claim_autonomous_job(
                task_id=task_id,
                session_id=session_id,
                objective=objective,
                mode=mode,
                job_id=job_id,
            ):
                return
            response = self._run_coordinated_task(
                session_id,
                objective,
                mode=mode,
                forced=False,
                task_id=task_id,
            )
            if self._is_cancelled(task_id):
                self._mark_cancelled_task_state(session_id, task_id, objective, reason="cancelled_during_run")
                self._cancel_autonomous_job(task_id, reason="cancelled_during_run", job_id=job_id)
                return
            state = self._get_session_state(session_id)
            active_object = dict(state.get("active_object") or {})
            active_task = dict(active_object.get("active_task") or {})
            if active_task.get("task_id") == task_id:
                active_task["status"] = "completed"
                active_task["completed_at"] = time.time()
                active_object["active_task"] = active_task
                self._update_session_state(session_id, active_object=active_object)
            if self._store_message is not None:
                self._store_message(session_id, "assistant", response[:4000])
            completed_state = self._get_session_state(session_id)
            completed_checkpoint = completed_state.get("last_checkpoint") or {}
            verification_status = str(completed_state.get("verification_status") or "unknown")
            terminal_status = "failed" if verification_status == "failed" else "succeeded"
            checkpoint_error = str(completed_checkpoint.get("error") or "")
            self._complete_autonomous_job(
                task_id=task_id,
                job_id=job_id,
                result={
                    "session_id": session_id,
                    "verification_status": verification_status,
                    "summary": str(completed_checkpoint.get("summary") or objective),
                    "terminal_status": terminal_status,
                },
            )
            if self.task_ledger is not None:
                artifacts = self._completion_artifacts(
                    task_id=task_id,
                    session_id=session_id,
                    checkpoint=completed_checkpoint,
                    verification_status=verification_status,
                    response_preview=response[:1000],
                    terminal_status=terminal_status,
                )
                self.task_ledger.mark_terminal(
                    task_id,
                    status=terminal_status,
                    summary=str(completed_checkpoint.get("summary") or objective),
                    verification_status=verification_status,
                    error=checkpoint_error if terminal_status == "failed" else "",
                    artifacts=artifacts,
                )
            self._emit(
                "autonomous_task_completed" if terminal_status == "succeeded" else "autonomous_task_failed",
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "objective": objective,
                    "response": response,
                    "verification_status": verification_status,
                    "terminal_status": terminal_status,
                    **({"error": checkpoint_error} if terminal_status == "failed" and checkpoint_error else {}),
                },
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._fail_autonomous_job(
                task_id=task_id,
                job_id=job_id,
                error=error,
                checkpoint={"operation": "coordinator", "mode": mode, "session_id": session_id},
            )
            checkpoint = {
                "summary": f"Autonomous task failed: {error}",
                "verification_status": "failed",
                "reason": "autonomous_task_exception",
                "task_id": task_id,
            }
            state = self._get_session_state(session_id)
            active_object = dict(state.get("active_object") or {})
            active_task = dict(active_object.get("active_task") or {})
            if active_task.get("task_id") == task_id:
                active_task["status"] = "failed"
                active_task["error"] = error
                active_task["completed_at"] = time.time()
                active_object["active_task"] = active_task
            self._update_session_state(
                session_id,
                verification_status="failed",
                pending_action=None,
                last_checkpoint=checkpoint,
                active_object=active_object,
            )
            response = f"Tarea autónoma falló: `{task_id}`\nError: {error}"
            if self._store_message is not None:
                self._store_message(session_id, "assistant", response[:4000])
            if self.task_ledger is not None:
                artifacts = self._outcome_artifacts(
                    task_id=task_id,
                    session_id=session_id,
                    status="failed",
                    summary=f"Autonomous task failed: {error}",
                    objective=objective,
                    mode=mode,
                    error=error,
                    verification_status="failed",
                    extra={"response_preview": response[:1000]},
                )
                self.task_ledger.mark_terminal(
                    task_id,
                    status="failed",
                    summary=f"Autonomous task failed: {error}",
                    error=error,
                    verification_status="failed",
                    artifacts=artifacts,
                )
            self._emit(
                "autonomous_task_failed",
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "objective": objective,
                    "error": error,
                    "response": response,
                },
            )
        finally:
            with self._task_lock:
                self._task_threads.pop(task_id, None)
                self._cancelled_tasks.discard(task_id)

    def wait_for_task(self, task_id: str, timeout: float = 5.0) -> bool:
        with self._task_lock:
            thread = self._task_threads.get(task_id)
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def resume_interrupted_autonomous_tasks(self, *, limit: int = 20) -> int:
        if self.task_ledger is None:
            return 0
        count = 0
        for record in self.task_ledger.list(statuses=("running",), limit=limit):
            if not self._is_resumable_record(record, automatic=True):
                continue
            if self._has_live_task_thread(record.task_id):
                continue
            self._resume_autonomous_record(record, reason="startup_recovery")
            count += 1
        return count

    def resume_task_response(self, session_id: str, task_id: str) -> str:
        if self.task_ledger is None:
            return "task ledger unavailable"
        record = self.task_ledger.get(task_id)
        if record is None:
            return f"task {task_id} not found"
        if record.status == "succeeded":
            return f"task {task_id} already succeeded"
        if self._has_live_task_thread(task_id):
            return f"task {task_id} is already running"
        if not self._is_resumable_record(record, automatic=False):
            return f"task {task_id} is not resumable"
        self._resume_autonomous_record(record, reason="manual_resume", requested_by_session=session_id)
        return f"Tarea reanudada: `{task_id}`\nEstado: running"

    def cancel_task_response(self, session_id: str, task_id: str) -> str:
        if self.task_ledger is None:
            return "task ledger unavailable"
        record = self.task_ledger.get(task_id)
        if record is None:
            return f"task {task_id} not found"
        if record.status == "cancelled":
            return f"task {task_id} already cancelled"
        live_thread = self._has_live_task_thread(task_id)
        if record.status not in {"queued", "running"} and not live_thread:
            return f"task {task_id} is already terminal: {record.status}"
        with self._task_lock:
            self._cancelled_tasks.add(task_id)
        self._mark_cancelled_task_state(
            record.session_id or session_id,
            task_id,
            record.objective,
            reason=f"cancelled_by:{session_id}",
        )
        if not live_thread:
            with self._task_lock:
                self._cancelled_tasks.discard(task_id)
        return f"Tarea cancelada: `{task_id}`"

    def _resume_autonomous_record(
        self,
        record: Any,
        *,
        reason: str,
        requested_by_session: str | None = None,
    ) -> None:
        mode = record.mode or _infer_session_mode(record.objective)
        metadata = dict(record.metadata or {})
        metadata["autonomous"] = True
        metadata["resume_reason"] = reason
        metadata["resume_count"] = int(metadata.get("resume_count") or 0) + 1
        metadata["last_resumed_at"] = time.time()
        if requested_by_session:
            metadata["requested_by_session"] = requested_by_session
        with self._task_lock:
            self._cancelled_tasks.discard(record.task_id)
        if self.task_ledger is not None:
            base_artifacts = self._ensure_plan_artifact(
                record.artifacts,
                task_id=record.task_id,
                session_id=record.session_id,
                objective=record.objective,
                mode=mode,
            )
            artifacts = append_lifecycle_artifacts(
                base_artifacts,
                ExecutionArtifact(
                    artifact_id=new_artifact_id("execution"),
                    task_id=record.task_id,
                    session_id=record.session_id,
                    status="resumed",
                    runtime=record.runtime,
                    provider=record.provider,
                    model=record.model,
                    reason=reason,
                ),
            )
            self.task_ledger.create(
                task_id=record.task_id,
                session_id=record.session_id,
                objective=record.objective,
                mode=mode,
                runtime=record.runtime,
                provider=record.provider,
                model=record.model,
                status="running",
                route=record.route,
                metadata=metadata,
                artifacts=artifacts,
            )
        state = self._get_session_state(record.session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["active_task"] = {
            "task_id": record.task_id,
            "objective": record.objective,
            "mode": mode,
            "status": "running",
            "resumed_at": metadata["last_resumed_at"],
            "resume_reason": reason,
        }
        self._update_session_state(
            record.session_id,
            mode=mode,
            pending_action=None,
            verification_status="running",
            last_checkpoint={
                "summary": f"Autonomous task resumed: {record.objective[:180]}",
                "verification_status": "running",
                "task_id": record.task_id,
                "reason": reason,
            },
            active_object=active_object,
        )
        self._emit(
            "autonomous_task_resumed",
            {
                "session_id": record.session_id,
                "task_id": record.task_id,
                "objective": record.objective,
                "reason": reason,
                "resume_count": metadata["resume_count"],
            },
        )
        thread = threading.Thread(
            target=self._run_autonomous_task,
            args=(
                record.session_id,
                record.task_id,
                record.objective,
                mode,
                self._enqueue_autonomous_job(
                    task_id=record.task_id,
                    session_id=record.session_id,
                    objective=record.objective,
                    mode=mode,
                    route=record.route,
                    reason=reason,
                    reclaim_running=True,
                ),
            ),
            daemon=True,
            name=f"autonomous-resume-{record.task_id[-8:]}",
        )
        with self._task_lock:
            self._task_threads[record.task_id] = thread
        thread.start()

    def _mark_cancelled_task_state(self, session_id: str, task_id: str, objective: str, *, reason: str) -> None:
        state = self._get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_task = dict(active_object.get("active_task") or {})
        if active_task.get("task_id") == task_id:
            active_task["status"] = "cancelled"
            active_task["cancelled_at"] = time.time()
            active_task["cancel_reason"] = reason
            active_object["active_task"] = active_task
        self._update_session_state(
            session_id,
            verification_status="cancelled",
            pending_action=None,
            last_checkpoint={
                "summary": f"Autonomous task cancelled: {objective[:180]}",
                "verification_status": "cancelled",
                "reason": reason,
                "task_id": task_id,
            },
            active_object=active_object,
        )
        if self.task_ledger is not None:
            artifacts = self._outcome_artifacts(
                task_id=task_id,
                session_id=session_id,
                status="cancelled",
                summary=f"Autonomous task cancelled: {objective[:180]}",
                objective=objective,
                mode=_infer_session_mode(objective),
                error=reason,
                verification_status="cancelled",
                extra={"cancel_reason": reason},
            )
            self.task_ledger.mark_terminal(
                task_id,
                status="cancelled",
                summary=f"Autonomous task cancelled: {objective[:180]}",
                error=reason,
                verification_status="cancelled",
                artifacts=artifacts,
            )
        self._cancel_autonomous_job(task_id, reason=reason)
        self._emit(
            "autonomous_task_cancelled",
            {
                "session_id": session_id,
                "task_id": task_id,
                "objective": objective,
                "reason": reason,
            },
        )

    def _has_live_task_thread(self, task_id: str) -> bool:
        with self._task_lock:
            thread = self._task_threads.get(task_id)
        return bool(thread and thread.is_alive())

    def _is_cancelled(self, task_id: str) -> bool:
        with self._task_lock:
            return task_id in self._cancelled_tasks

    @staticmethod
    def _is_resumable_record(record: Any, *, automatic: bool) -> bool:
        metadata = dict(record.metadata or {})
        if automatic and metadata.get("autonomous") is not True:
            return False
        if record.runtime != "coordinator":
            return False
        if record.status == "succeeded":
            return False
        return record.status in {"queued", "running", "failed", "timed_out", "cancelled", "lost"}

    @staticmethod
    def _ensure_plan_artifact(
        artifacts: dict[str, Any] | None,
        *,
        task_id: str,
        session_id: str,
        objective: str,
        mode: str,
    ) -> dict[str, Any]:
        payload = dict(artifacts or {})
        lifecycle = payload.get("lifecycle") if isinstance(payload.get("lifecycle"), dict) else {}
        if isinstance(lifecycle, dict) and isinstance(lifecycle.get("plan"), dict):
            return payload
        return append_lifecycle_artifacts(
            payload,
            PlanArtifact(
                artifact_id=new_artifact_id("plan"),
                task_id=task_id,
                session_id=session_id,
                objective=objective,
                mode=mode,
                planned_phases=planned_phases_for_mode(mode),
            ),
        )

    def _current_task_artifacts(self, task_id: str) -> dict[str, Any]:
        if self.task_ledger is None:
            return {}
        record = self.task_ledger.get(task_id)
        return dict(record.artifacts or {}) if record is not None else {}

    def _completion_artifacts(
        self,
        *,
        task_id: str,
        session_id: str,
        checkpoint: dict[str, Any],
        verification_status: str,
        response_preview: str,
        terminal_status: str = "succeeded",
    ) -> dict[str, Any]:
        summary = str(checkpoint.get("summary") or "")
        artifacts = append_lifecycle_artifacts(
            self._current_task_artifacts(task_id),
            VerificationArtifact(
                artifact_id=new_artifact_id("verification"),
                task_id=task_id,
                session_id=session_id,
                status=verification_status,
                summary=summary,
                pending_action=str(checkpoint.get("pending_action") or ""),
            ),
            OutcomeArtifact(
                artifact_id=new_artifact_id("outcome"),
                task_id=task_id,
                session_id=session_id,
                status=terminal_status,
                summary=summary,
                error=str(checkpoint.get("error") or "") if terminal_status == "failed" else "",
                verification_status=verification_status,
            ),
        )
        artifacts["response_preview"] = response_preview
        job_state = "completed" if terminal_status == "succeeded" else "failed"
        return self._with_job_artifact(task_id, session_id, job_state, artifacts)

    def _outcome_artifacts(
        self,
        *,
        task_id: str,
        session_id: str,
        status: str,
        summary: str,
        verification_status: str,
        error: str = "",
        objective: str | None = None,
        mode: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base_artifacts = self._current_task_artifacts(task_id)
        if objective is not None:
            base_artifacts = self._ensure_plan_artifact(
                base_artifacts,
                task_id=task_id,
                session_id=session_id,
                objective=objective,
                mode=mode or _infer_session_mode(objective),
            )
        artifacts = append_lifecycle_artifacts(
            base_artifacts,
            OutcomeArtifact(
                artifact_id=new_artifact_id("outcome"),
                task_id=task_id,
                session_id=session_id,
                status=status,
                summary=summary,
                error=error,
                verification_status=verification_status,
            ),
        )
        artifacts.update(dict(extra or {}))
        return self._with_job_artifact(task_id, session_id, status, artifacts)

    def _initial_task_artifacts(
        self,
        *,
        task_id: str,
        session_id: str,
        objective: str,
        mode: str,
        runtime: str,
        provider: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        artifacts = append_lifecycle_artifacts(
            {},
            PlanArtifact(
                artifact_id=new_artifact_id("plan"),
                task_id=task_id,
                session_id=session_id,
                objective=objective,
                mode=mode,
                planned_phases=planned_phases_for_mode(mode),
            ),
            ExecutionArtifact(
                artifact_id=new_artifact_id("execution"),
                task_id=task_id,
                session_id=session_id,
                status="running",
                runtime=runtime,
                provider=provider,
                model=model,
                reason="task_started",
            ),
        )
        return self._with_job_artifact(task_id, session_id, "running", artifacts)

    @staticmethod
    def _with_job_artifact(
        task_id: str,
        session_id: str,
        lifecycle_status: str,
        artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        lifecycle = dict(artifacts.get("lifecycle") or {})
        artifact_ids = list(lifecycle.get("artifact_ids") or [])
        return append_lifecycle_artifacts(
            artifacts,
            JobArtifact(
                artifact_id=new_artifact_id("job"),
                task_id=task_id,
                session_id=session_id,
                lifecycle_status=lifecycle_status,
                artifact_ids=artifact_ids,
            ),
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        task_id = payload.get("task_id")
        artifact_id = payload.get("artifact_id")
        self.observe.emit(
            event_type,
            lane="coordinator",
            job_id=str(task_id) if task_id else None,
            artifact_id=str(artifact_id) if artifact_id else None,
            payload=payload,
        )

    def _record_ledger_task_started(
        self,
        *,
        task_id: str,
        session_id: str,
        objective: str,
        mode: str,
        route: dict[str, Any],
    ) -> None:
        if self.task_ledger is None:
            return
        provider, model = self._provider_model_for_mode(session_id, mode)
        runtime = "coordinator"
        self.task_ledger.create(
            task_id=task_id,
            session_id=session_id,
            objective=objective,
            mode=mode,
            runtime=runtime,
            provider=provider,
            model=model,
            status="running",
            route=route,
            metadata={"autonomous": True},
            artifacts=self._initial_task_artifacts(
                task_id=task_id,
                session_id=session_id,
                objective=objective,
                mode=mode,
                runtime=runtime,
                provider=provider,
                model=model,
            ),
        )

    @staticmethod
    def _resume_key_for_task(task_id: str) -> str:
        return f"coordinator:{task_id}"

    def _enqueue_autonomous_job(
        self,
        *,
        task_id: str,
        session_id: str,
        objective: str,
        mode: str,
        route: dict[str, Any],
        reason: str,
        reclaim_running: bool = False,
    ) -> str | None:
        if self.job_service is None:
            return None
        provider, model = self._provider_model_for_mode(session_id, mode)
        job = self.job_service.enqueue(
            kind="coordinator.autonomous_task",
            payload={
                "task_id": task_id,
                "session_id": session_id,
                "objective": objective,
                "mode": mode,
            },
            resume_key=self._resume_key_for_task(task_id),
            metadata={
                "runtime": "coordinator",
                "provider": provider,
                "model": model,
                "route": dict(route or {}),
                "reason": reason,
            },
        )
        if reclaim_running and job.status == "running":
            retried = self.job_service.fail(
                job.job_id,
                error=f"reclaiming interrupted autonomous task: {reason}",
                retry=True,
                retry_delay_seconds=0,
                checkpoint={"task_id": task_id, "session_id": session_id, "reason": reason},
            )
            if retried is not None:
                job = retried
        self._update_task_job_metadata(task_id, job.job_id)
        return job.job_id

    def _claim_autonomous_job(
        self,
        *,
        task_id: str,
        session_id: str,
        objective: str,
        mode: str,
        job_id: str | None,
    ) -> bool:
        if self.job_service is None or job_id is None:
            return True
        claimed = self.job_service.claim(job_id, worker_id="coordinator")
        if claimed is not None:
            self.job_service.checkpoint(
                job_id,
                {
                    "operation": "coordinator",
                    "task_id": task_id,
                    "session_id": session_id,
                    "objective": objective,
                    "mode": mode,
                },
            )
            return True
        record = self.job_service.get(job_id)
        if record is not None and record.status == "cancelled":
            self._mark_cancelled_task_state(session_id, task_id, objective, reason=record.error or "job_cancelled")
            return False
        if record is not None and record.status in {"completed", "failed"}:
            self._emit(
                "autonomous_task_job_skipped",
                {
                    "session_id": session_id,
                    "task_id": task_id,
                    "job_id": job_id,
                    "job_status": record.status,
                },
            )
            return False
        return True

    def _complete_autonomous_job(self, *, task_id: str, job_id: str | None, result: dict[str, Any]) -> None:
        if self.job_service is None:
            return
        record_id = job_id or self._active_job_id_for_task(task_id)
        if record_id is not None:
            self.job_service.complete(record_id, result=result)

    def _fail_autonomous_job(
        self,
        *,
        task_id: str,
        job_id: str | None,
        error: str,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        if self.job_service is None:
            return
        record_id = job_id or self._active_job_id_for_task(task_id)
        if record_id is not None:
            self.job_service.fail(record_id, error=error, retry=False, checkpoint=checkpoint)

    def _cancel_autonomous_job(self, task_id: str, *, reason: str, job_id: str | None = None) -> None:
        if self.job_service is None:
            return
        record_id = job_id or self._active_job_id_for_task(task_id)
        if record_id is not None:
            self.job_service.cancel(record_id, reason=reason)

    def _active_job_id_for_task(self, task_id: str) -> str | None:
        if self.job_service is None:
            return None
        record = self.job_service.get_active_by_resume_key(self._resume_key_for_task(task_id))
        return record.job_id if record is not None else None

    def _update_task_job_metadata(self, task_id: str, job_id: str) -> None:
        if self.task_ledger is None:
            return
        record = self.task_ledger.get(task_id)
        if record is None:
            return
        metadata = dict(record.metadata or {})
        metadata["generic_job_id"] = job_id
        self.task_ledger.create(
            task_id=record.task_id,
            session_id=record.session_id,
            objective=record.objective,
            mode=record.mode,
            runtime=record.runtime,
            provider=record.provider,
            model=record.model,
            status=record.status,
            notify_policy=record.notify_policy,
            route=record.route,
            metadata=metadata,
            artifacts=record.artifacts,
        )

    def _provider_model_for_mode(self, session_id: str, mode: str) -> tuple[str | None, str | None]:
        lane = "research" if mode == "research" else "worker"
        overrides = model_overrides_from_state(self._get_session_state(session_id))
        if lane in overrides:
            override = overrides[lane]
            return override.provider, override.model
        router = getattr(self.coordinator, "router", None)
        config = getattr(router, "config", None)
        if config is None:
            return None, None
        try:
            return str(config.provider_for_lane(lane)), str(config.model_for_lane(lane))
        except Exception:
            return None, None

    def _lane_model_overrides(self, session_id: str) -> dict[str, dict[str, Any]]:
        overrides = model_overrides_from_state(self._get_session_state(session_id))
        return {lane: override.to_dict() for lane, override in overrides.items()}

    def _updated_pending_task_approvals(self, session_id: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
        state = self._get_session_state(session_id)
        pending = [item for item in (state.get("pending_approvals") or []) if item.get("approval_id") != entry.get("approval_id")]
        pending.append(entry)
        return pending[-5:]

    def _remove_pending_task_approval(self, session_id: str, approval_id: str) -> None:
        state = self._get_session_state(session_id)
        pending = [item for item in (state.get("pending_approvals") or []) if item.get("approval_id") != approval_id]
        self._update_session_state(session_id, pending_approvals=pending)

    def task_queue_transition_response(self, session_id: str, task_id: str, *, to_status: str) -> str:
        from claw_v2.bot_helpers import _select_next_task_queue_item
        state = self._get_session_state(session_id)
        task_queue = state.get("task_queue") or []
        updated = self.set_task_queue_status(task_queue, task_id=task_id, to_status=to_status)
        if updated == task_queue:
            return f"task {task_id} not found"
        next_pending = _select_next_task_queue_item(updated, preferred_mode=state.get("mode") or "chat")
        self._update_session_state(
            session_id,
            task_queue=updated,
            pending_action=next_pending.get("summary") if next_pending else "",
        )
        return json.dumps(updated, indent=2, sort_keys=True)

    @staticmethod
    def upsert_task_queue_entry(
        queue: list[dict[str, Any]],
        *,
        summary: str,
        mode: str,
        status: str,
        source: str,
        priority: int,
        depends_on: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        compact = " ".join(summary.split()).strip()
        if not compact:
            return queue
        existing = next((item for item in queue if item.get("summary") == compact), None)
        updated = [item for item in queue if item.get("summary") != compact]
        task_id = _stable_task_id(compact, mode=mode, source=source)
        effective_status = status
        if existing is not None and status == "pending" and existing.get("status") in {"in_progress", "done", "blocked"}:
            effective_status = str(existing.get("status"))
        updated.append(
            {
                "task_id": task_id,
                "summary": compact,
                "mode": mode,
                "status": effective_status,
                "source": source,
                "priority": priority,
                "depends_on": list(depends_on or []),
            }
        )
        updated.sort(key=lambda item: (int(item.get("priority", 9)), item.get("task_id", "")))
        return updated[-8:]

    @staticmethod
    def mark_task_queue_in_progress(
        queue: list[dict[str, Any]],
        *,
        task_id: str | None = None,
        summary: str | None = None,
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        promoted = False
        for item in queue:
            current = dict(item)
            matches = False
            if task_id and current.get("task_id") == task_id:
                matches = True
            elif summary and current.get("summary") == summary:
                matches = True
            if not promoted and matches and current.get("status") == "pending":
                current["status"] = "in_progress"
                promoted = True
            updated.append(current)
        return updated

    @staticmethod
    def mark_first_task_queue_entry(
        queue: list[dict[str, Any]],
        *,
        from_status: str,
        to_status: str,
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        transitioned = False
        for item in queue:
            current = dict(item)
            if not transitioned and current.get("status") == from_status:
                current["status"] = to_status
                transitioned = True
            updated.append(current)
        return updated

    @staticmethod
    def set_task_queue_status(
        queue: list[dict[str, Any]],
        *,
        task_id: str,
        to_status: str,
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        changed = False
        for item in queue:
            current = dict(item)
            if current.get("task_id") == task_id:
                current["status"] = to_status
                changed = True
            updated.append(current)
        if changed:
            updated.sort(key=lambda item: (int(item.get("priority", 9)), item.get("task_id", "")))
        return updated

    @staticmethod
    def derive_task_dependencies(queue: list[dict[str, Any]], *, summary: str) -> list[str]:
        compact = " ".join(summary.split()).strip()
        in_progress = next(
            (
                item for item in queue
                if item.get("status") == "in_progress"
                and item.get("summary")
                and item.get("summary") != compact
            ),
            None,
        )
        if in_progress is None:
            return []
        task_id = in_progress.get("task_id")
        return [str(task_id)] if isinstance(task_id, str) and task_id else []
