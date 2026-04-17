from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from claw_v2.bot_helpers import (
    _build_checkpoint,
    _compact_summary,
    _default_step_budget,
    _extract_numbered_options,
    _extract_option_reference,
    _extract_pending_action_from_reply,
    _extract_verification_status,
    _infer_session_mode,
    _looks_like_proceed_request,
    _select_next_task_queue_item,
)


@dataclass(slots=True)
class _BrainShortcut:
    text: str
    memory_text: str | None = None


class StateHandler:
    def __init__(self, *, brain_memory: Any, task_handler: Any) -> None:
        self._memory = brain_memory
        self._task_handler = task_handler

    def remember_user_turn_state(self, session_id: str, text: str) -> None:
        if not text or text.startswith("/"):
            return
        if _extract_option_reference(text) is not None or _looks_like_proceed_request(text):
            return
        inferred_mode = _infer_session_mode(text)
        current_goal = text.strip()
        if len(current_goal) < 8:
            current_goal = None
        elif len(current_goal) > 280:
            current_goal = current_goal[:277] + "..."
        current = self._memory.get_session_state(session_id)
        self._memory.update_session_state(
            session_id,
            mode=inferred_mode,
            current_goal=current_goal,
            pending_action=None,
            task_queue=[],
            steps_taken=0,
            verification_status="unknown",
            last_checkpoint={},
            step_budget=_default_step_budget(current.get("autonomy_mode", "assisted")),
        )

    def remember_assistant_turn_state(self, session_id: str, user_text: str, reply_text: str) -> None:
        state = self._memory.get_session_state(session_id)
        options = _extract_numbered_options(reply_text)
        pending_action = state.get("pending_action")
        extracted_pending_action = _extract_pending_action_from_reply(reply_text)
        if extracted_pending_action is not None:
            pending_action = extracted_pending_action
        if options:
            pending_action = None
        rolling_summary = _compact_summary(reply_text)
        verification_status = _extract_verification_status(reply_text) or state.get("verification_status", "unknown")
        checkpoint = _build_checkpoint(reply_text, pending_action=pending_action, verification_status=verification_status)
        steps_taken = state.get("steps_taken", 0)
        is_followup_selection = _extract_option_reference(user_text) is not None
        if state.get("mode") in {"coding", "research", "browse", "publish", "ops"} and not is_followup_selection:
            steps_taken += 1
        task_queue = state.get("task_queue") or []
        if isinstance(pending_action, str) and pending_action.strip():
            depends_on = self._task_handler.derive_task_dependencies(task_queue, summary=pending_action)
            task_queue = self._task_handler.upsert_task_queue_entry(
                task_queue,
                summary=pending_action,
                mode=_infer_session_mode(user_text, reply_text),
                status="pending",
                source="assistant",
                priority=1,
                depends_on=depends_on,
            )
        elif verification_status == "passed":
            task_queue = self._task_handler.mark_first_task_queue_entry(task_queue, from_status="in_progress", to_status="done")
            task_queue = self._task_handler.mark_first_task_queue_entry(task_queue, from_status="pending", to_status="done")
        elif verification_status == "failed":
            task_queue = self._task_handler.mark_first_task_queue_entry(task_queue, from_status="in_progress", to_status="blocked")
        self._memory.update_session_state(
            session_id,
            mode=_infer_session_mode(user_text, reply_text),
            pending_action=pending_action,
            task_queue=task_queue,
            steps_taken=steps_taken,
            verification_status=verification_status,
            last_options=options if options else state.get("last_options"),
            last_checkpoint=checkpoint,
            rolling_summary=rolling_summary,
        )

    def maybe_resolve_stateful_followup(self, text: str, *, session_id: str) -> str | _BrainShortcut | None:
        if not text or text.startswith("/"):
            return None
        state = self._memory.get_session_state(session_id)
        option_index = _extract_option_reference(text)
        if option_index is not None:
            options = state.get("last_options") or []
            if 1 <= option_index <= len(options):
                selected = options[option_index - 1]
                self._memory.update_session_state(
                    session_id,
                    pending_action=selected,
                )
                return _BrainShortcut(
                    text=(
                        f"El usuario seleccionó la opción {option_index}.\n"
                        f"Opción elegida: {selected}"
                    ),
                    memory_text=text,
                )
        if _looks_like_proceed_request(text):
            if state.get("verification_status") == "awaiting_approval":
                pending_approvals = state.get("pending_approvals") or []
                latest_pending = pending_approvals[-1] if pending_approvals else {}
                approval_id = latest_pending.get("approval_id") or (state.get("last_checkpoint") or {}).get("approval_id")
                if approval_id:
                    return (
                        "Hay una aprobación pendiente antes de continuar.\n"
                        "Usa `/task_pending` para ver el comando `/task_approve <approval_id> <token>`, "
                        f"o aborta con `/task_abort {approval_id}`."
                    )
                return "Hay una aprobación pendiente antes de continuar. Usa `/task_approve <approval_id> <token>`."
            if state.get("steps_taken", 0) >= state.get("step_budget", 0):
                checkpoint = state.get("last_checkpoint") or {}
                summary = checkpoint.get("summary") or state.get("rolling_summary") or "sin checkpoint"
                return (
                    "step budget agotado para esta tarea.\n"
                    f"Resumen actual: {summary}\n"
                    "Ajusta el objetivo o aumenta la autonomía para seguir."
                )
            pending_action = state.get("pending_action")
            if isinstance(pending_action, str) and pending_action.strip():
                task_queue = self._task_handler.mark_task_queue_in_progress(state.get("task_queue") or [], summary=pending_action)
                self._memory.update_session_state(session_id, task_queue=task_queue)
                checkpoint = state.get("last_checkpoint") or {}
                checkpoint_text = json.dumps(checkpoint, ensure_ascii=True, sort_keys=True) if checkpoint else "{}"
                return _BrainShortcut(
                    text=(
                        f"Continúa con esta acción pendiente: {pending_action}\n"
                        f"Checkpoint actual: {checkpoint_text}"
                    ),
                    memory_text=text,
                )
            task_queue = state.get("task_queue") or []
            current_mode = state.get("mode") or "chat"
            next_task = _select_next_task_queue_item(task_queue, preferred_mode=current_mode)
            if next_task is not None:
                task_queue = self._task_handler.mark_task_queue_in_progress(task_queue, task_id=next_task.get("task_id"))
                self._memory.update_session_state(session_id, task_queue=task_queue)
                checkpoint = state.get("last_checkpoint") or {}
                checkpoint_text = json.dumps(checkpoint, ensure_ascii=True, sort_keys=True) if checkpoint else "{}"
                return _BrainShortcut(
                    text=(
                        f"Continúa con este siguiente paso de la cola: {next_task['summary']}\n"
                        f"Checkpoint actual: {checkpoint_text}"
                    ),
                    memory_text=text,
                )
        return None
