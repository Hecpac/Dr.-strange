from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from claw_v2.bot_helpers import (
    _build_checkpoint,
    _compact_summary,
    _default_step_budget,
    _extract_numbered_options,
    _extract_option_reference,
    _extract_pending_action_from_reply,
    _extract_ratio_context_from_text,
    _extract_verification_status,
    _infer_session_mode,
    _looks_like_proceed_request,
    _looks_like_ratio_reference_request,
    _select_next_task_queue_item,
)
from claw_v2.redaction import redact_sensitive


_REDACTED_MARKERS = ("[REDACTED]", "<REDACTED:")


def _contains_sensitive_redaction(text: str) -> bool:
    redacted = str(redact_sensitive(text, limit=0))
    return redacted != text or any(marker in redacted for marker in _REDACTED_MARKERS)


@dataclass(slots=True)
class _BrainShortcut:
    text: str
    memory_text: str | None = None


class StateHandler:
    def __init__(self, *, brain_memory: Any, task_handler: Any, observe: Any | None = None) -> None:
        self._memory = brain_memory
        self._task_handler = task_handler
        self._observe = observe

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._observe is None:
            return
        try:
            self._observe.emit(event_type, payload=payload)
        except Exception:
            return

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
            pending_action="",
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
            pending_action = ""
        active_object = dict(state.get("active_object") or {})
        if options:
            active_object["last_options_meta"] = {
                "created_at": time.time(),
                "source": "assistant_numbered_options",
                "topic": _compact_summary(user_text, limit=140) or "",
            }
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
            existing_entry = next(
                (
                    item
                    for item in task_queue
                    if isinstance(item, dict)
                    and item.get("summary") == pending_action
                    and item.get("source") == "sanitizer_recovery"
                ),
                None,
            )
            task_queue = self._task_handler.upsert_task_queue_entry(
                task_queue,
                summary=pending_action,
                mode=_infer_session_mode(user_text, reply_text),
                status="pending",
                source="sanitizer_recovery" if existing_entry is not None else "assistant",
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
            active_object=active_object,
        )

    def maybe_resolve_stateful_followup(self, text: str, *, session_id: str) -> str | _BrainShortcut | None:
        if not text or text.startswith("/"):
            return None
        state = self._memory.get_session_state(session_id)
        ratio_shortcut = self._maybe_resolve_ratio_followup(text, session_id=session_id, state=state)
        if ratio_shortcut is not None:
            return ratio_shortcut
        option_index = _extract_option_reference(text)
        if option_index is not None:
            options = state.get("last_options") or []
            if 1 <= option_index <= len(options):
                if not self._last_options_still_valid(state):
                    self._emit(
                        "stale_options_rejected",
                        {
                            "session_id": session_id,
                            "option_index": option_index,
                            "options_count": len(options),
                        },
                    )
                    return (
                        f"No tengo una lista de opciones vigente para elegir la {option_index}. "
                        "Reenvíame las opciones o dime el objetivo concreto."
                    )
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
            self._emit(
                "stale_options_rejected",
                {
                    "session_id": session_id,
                    "option_index": option_index,
                    "options_count": len(options) if isinstance(options, list) else 0,
                },
            )
            return (
                f"No tengo una opción {option_index} vigente. "
                "Reenvíame las opciones o dime el objetivo concreto."
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
                if _contains_sensitive_redaction(pending_action):
                    return self._reject_sensitive_continuation(
                        session_id,
                        source="pending_action",
                    )
                self._emit(
                    "approval_detected",
                    {"session_id": session_id, "source": "pending_action", "text_preview": text[:80]},
                )
                self._emit(
                    "pending_action_detected",
                    {"session_id": session_id, "pending_action_preview": pending_action[:160]},
                )
                task_queue = self._task_handler.mark_task_queue_in_progress(state.get("task_queue") or [], summary=pending_action)
                self._memory.update_session_state(session_id, task_queue=task_queue)
                checkpoint = state.get("last_checkpoint") or {}
                checkpoint_text = json.dumps(checkpoint, ensure_ascii=True, sort_keys=True) if checkpoint else "{}"
                self._emit(
                    "pending_action_execution_started",
                    {"session_id": session_id, "pending_action_preview": pending_action[:160]},
                )
                return _BrainShortcut(
                    text=(
                        f"Continúa con esta acción pendiente: {pending_action}\n"
                        f"Mensaje de aprobación del usuario: {text}\n"
                        f"Checkpoint actual: {checkpoint_text}"
                    ),
                    memory_text=text,
                )
            task_queue = state.get("task_queue") or []
            current_mode = state.get("mode") or "chat"
            next_task = _select_next_task_queue_item(task_queue, preferred_mode=current_mode)
            if next_task is not None:
                next_summary = str(next_task.get("summary") or "")
                if _contains_sensitive_redaction(next_summary):
                    blocked_queue = self._task_handler.mark_first_task_queue_entry(
                        task_queue,
                        from_status="pending",
                        to_status="blocked",
                    )
                    return self._reject_sensitive_continuation(
                        session_id,
                        source="task_queue",
                        task_queue=blocked_queue,
                    )
                task_queue = self._task_handler.mark_task_queue_in_progress(task_queue, task_id=next_task.get("task_id"))
                self._memory.update_session_state(session_id, task_queue=task_queue)
                checkpoint = state.get("last_checkpoint") or {}
                checkpoint_text = json.dumps(checkpoint, ensure_ascii=True, sort_keys=True) if checkpoint else "{}"
                self._emit(
                    "pending_action_execution_started",
                    {"session_id": session_id, "pending_action_preview": str(next_task.get("summary") or "")[:160]},
                )
                return _BrainShortcut(
                    text=(
                        f"Continúa con este siguiente paso de la cola: {next_task['summary']}\n"
                        f"Mensaje de aprobación del usuario: {text}\n"
                        f"Checkpoint actual: {checkpoint_text}"
                    ),
                    memory_text=text,
                )
            proposal = self._extract_proposal_from_reply_context(state)
            proposal_source = "reply_context" if proposal else None
            if not proposal:
                proposal = self._extract_proposal_from_recent_assistant(session_id)
                if proposal:
                    proposal_source = "recent_assistant"
            if proposal:
                if _contains_sensitive_redaction(proposal):
                    return self._reject_sensitive_continuation(
                        session_id,
                        source=proposal_source or "proposal",
                    )
                self._memory.update_session_state(session_id, pending_action=proposal)
                checkpoint = state.get("last_checkpoint") or {}
                checkpoint_text = json.dumps(checkpoint, ensure_ascii=True, sort_keys=True) if checkpoint else "{}"
                self._emit(
                    "continuation_resolved",
                    {
                        "session_id": session_id,
                        "source": proposal_source,
                        "proposal_preview": proposal[:160],
                        "user_text_preview": text[:80],
                    },
                )
                self._emit(
                    "pending_action_execution_started",
                    {"session_id": session_id, "pending_action_preview": proposal[:160]},
                )
                return _BrainShortcut(
                    text=(
                        f"Continúa con esta acción propuesta previamente: {proposal}\n"
                        f"Mensaje de aprobación del usuario: {text}\n"
                        f"Origen del contexto: {proposal_source}\n"
                        f"Checkpoint actual: {checkpoint_text}"
                    ),
                    memory_text=text,
                )
            self._emit(
                "clarification_requested_after_context_lookup",
                {"session_id": session_id, "reason": "proceed_without_pending_action"},
            )
            return "¿Qué acción concreta quieres que ejecute?"
        return None

    def _reject_sensitive_continuation(
        self,
        session_id: str,
        *,
        source: str,
        task_queue: list[dict[str, Any]] | None = None,
    ) -> str:
        checkpoint = {
            "summary": "Continuation rejected because pending context contained redacted sensitive material.",
            "verification_status": "blocked",
            "reason": "sensitive_context_redacted",
            "source": source,
        }
        update: dict[str, Any] = {
            "pending_action": "",
            "verification_status": "blocked",
            "last_checkpoint": checkpoint,
        }
        if task_queue is not None:
            update["task_queue"] = task_queue
        self._memory.update_session_state(session_id, **update)
        self._emit(
            "sensitive_continuation_rejected",
            {"session_id": session_id, "source": source},
        )
        return "La acción pendiente contiene un valor sensible redactado. Reenvíame el objetivo concreto sin tokens ni secretos."

    def _last_options_still_valid(self, state: dict[str, Any]) -> bool:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return False
        meta = active_object.get("last_options_meta") or {}
        if not isinstance(meta, dict):
            return False
        created_at = meta.get("created_at")
        try:
            age = time.time() - float(created_at)
        except (TypeError, ValueError):
            return False
        return age <= 30 * 60

    def _maybe_resolve_ratio_followup(
        self,
        text: str,
        *,
        session_id: str,
        state: dict[str, Any],
    ) -> _BrainShortcut | None:
        if not _looks_like_ratio_reference_request(text) and not self._looks_like_generic_two_artifact_request(text):
            return None
        context = self._ratio_context_from_state(state)
        source = "session_state"
        if len(context) < 2:
            reply_context = self._reply_context_text(state)
            context = _extract_ratio_context_from_text(reply_context)
            source = "reply_to" if context else source
        if len(context) < 2:
            recent_context = self._ratio_context_from_recent_messages(session_id)
            if len(recent_context) >= 2:
                context = recent_context
                source = "recent_messages"
        if len(context) < 2:
            self._emit(
                "clarification_requested_after_context_lookup",
                {"session_id": session_id, "reason": "ratio_context_not_found"},
            )
            return None
        selected = context[:2]
        self._emit(
            "contextual_reference_detected",
            {
                "session_id": session_id,
                "reference_type": "ratio_pair",
                "source": source,
                "resolved_count": len(selected),
            },
        )
        if source in {"session_state", "recent_messages", "reply_to"}:
            self._emit(
                "pending_artifacts_resolved",
                {
                    "session_id": session_id,
                    "source": source,
                    "artifact_labels": selected,
                },
            )
        return _BrainShortcut(
            text=(
                "El usuario pidió los 2 ratios. No lo trates como selección de opción 2.\n"
                f"Contexto resuelto desde {source}: {', '.join(selected)}.\n"
                "Envía ambos artifacts si están disponibles; si falta un archivo, di exactamente cuál falta después de consultar contexto."
            ),
            memory_text=text,
        )

    def _looks_like_generic_two_artifact_request(self, text: str) -> bool:
        normalized = " ".join(text.lower().replace("í", "i").split())
        return normalized in {
            "dame los 2",
            "dame los dos",
            "mandame los 2",
            "mandame los dos",
            "enviame los 2",
            "enviame los dos",
            "pasame los 2",
            "pasame los dos",
        }

    def _reply_context_text(self, state: dict[str, Any]) -> str:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return ""
        reply_context = active_object.get("reply_context") or {}
        if not isinstance(reply_context, dict):
            return ""
        return str(reply_context.get("text") or "")

    # Proposal/question patterns the assistant uses when offering to execute
    # something. When Hector replies "Procede"/"Sí"/"Dale" to a message that
    # ends with one of these, treat it as approval to execute the proposal.
    _PROPOSAL_QUESTION_RE = re.compile(
        r"¿\s*(?:lo\s+arranco|lo\s+hago|lo\s+ejecuto|lo\s+lanzo|lo\s+disparo"
        r"|procedo|continuo|continúo|sigo|avanzo|voy|arranco|"
        r"lo\s+intento|lo\s+pruebo|lo\s+corro)\s*\??\s*[\?!\.]*\s*$",
        re.IGNORECASE | re.MULTILINE,
    )

    def _looks_like_proposal_question(self, text: str) -> bool:
        if not text:
            return False
        # Look at the last non-empty line — proposals typically close with the
        # question on its own line.
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return False
        tail = lines[-1]
        if self._PROPOSAL_QUESTION_RE.search(tail):
            return True
        # Some proposals close with the question mid-paragraph; scan the
        # whole tail block too.
        return bool(self._PROPOSAL_QUESTION_RE.search(text[-400:]))

    def _extract_proposal_from_reply_context(self, state: dict[str, Any]) -> str | None:
        text = self._reply_context_text(state)
        if not text:
            return None
        if not self._looks_like_proposal_question(text):
            return None
        return self._summarize_proposal(text)

    def _extract_proposal_from_recent_assistant(self, session_id: str) -> str | None:
        get_recent = getattr(self._memory, "get_recent_messages", None)
        if not callable(get_recent):
            return None
        try:
            messages = get_recent(session_id, limit=8)
        except Exception:
            return None
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            content = str(message.get("content") or "")
            if self._looks_like_proposal_question(content):
                return self._summarize_proposal(content)
        return None

    def _summarize_proposal(self, text: str) -> str:
        # Strip the closing question, keep the substantive body.
        cleaned = self._PROPOSAL_QUESTION_RE.sub("", text).strip()
        if not cleaned:
            cleaned = text.strip()
        # Collapse whitespace, cap length so it fits inside a brain hint.
        compact = " ".join(cleaned.split())
        if len(compact) > 320:
            compact = compact[:317] + "..."
        return compact

    def _ratio_context_from_state(self, state: dict[str, Any]) -> list[str]:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return []
        chunks: list[str] = []
        pending_action = state.get("pending_action")
        if isinstance(pending_action, str):
            chunks.append(pending_action)
        for key in ("pending_artifacts", "recent_artifacts", "artifacts"):
            value = active_object.get(key)
            if isinstance(value, list):
                chunks.extend(json.dumps(item, ensure_ascii=False) for item in value)
            elif isinstance(value, dict):
                chunks.append(json.dumps(value, ensure_ascii=False))
            elif isinstance(value, str):
                chunks.append(value)
        return _extract_ratio_context_from_text("\n".join(chunks))

    def _ratio_context_from_recent_messages(self, session_id: str) -> list[str]:
        get_recent = getattr(self._memory, "get_recent_messages", None)
        if not callable(get_recent):
            return []
        try:
            messages = get_recent(session_id, limit=12)
        except Exception:
            return []
        chunks = []
        for message in reversed(messages):
            if message.get("role") != "assistant":
                continue
            content = str(message.get("content") or "")
            if "ratio" in content.lower() or "9:16" in content or "1:1" in content or "9x16" in content.lower() or "1x1" in content.lower():
                chunks.append(content)
            if len(chunks) >= 4:
                break
        return _extract_ratio_context_from_text("\n".join(reversed(chunks)))
