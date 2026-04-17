from __future__ import annotations

import json
import time
from typing import Any, Callable

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


class TaskHandler:
    def __init__(
        self,
        *,
        approvals: Any | None = None,
        coordinator: Any | None = None,
        get_session_state: Callable[[str], dict[str, Any]],
        update_session_state: Callable[..., Any],
    ) -> None:
        self.approvals = approvals
        self.coordinator = coordinator
        self._get_session_state = get_session_state
        self._update_session_state = update_session_state

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
        policy = _evaluate_autonomy_policy(text, mode=mode, forced=False)
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
        return self.coordinated_task_response(session_id, text, forced=False)

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
        policy = _evaluate_autonomy_policy(
            objective,
            mode=mode,
            forced=forced,
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
        research_tasks, implementation_tasks, verification_tasks = _build_coordinator_tasks(mode, objective)
        result = self.coordinator.run(
            task_id,
            objective,
            research_tasks,
            implementation_tasks=implementation_tasks,
            verification_tasks=verification_tasks,
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
