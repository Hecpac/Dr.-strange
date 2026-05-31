from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

from claw_v2.bot_helpers import (
    OwnerDelegationIntent,
    _build_checkpoint,
    _compact_summary,
    _default_step_budget,
    _extract_numbered_options,
    _extract_option_reference,
    _extract_pending_action_from_reply,
    _extract_ratio_context_from_text,
    _extract_verification_status,
    _infer_session_mode,
    _is_secret_shaped_token,
    _looks_like_proceed_request,
    _looks_like_ratio_reference_request,
    _normalize_command_text,
    _select_next_task_queue_item,
    is_destructive_or_external_objective,
)
from claw_v2.redaction import redact_sensitive


# Wave 0: pending_action freshness window. Short approvals resolve only
# against fresh context: 3 message turns OR 10 minutes, whichever expires
# first. Last-options remain on their older 30-minute window.
PENDING_ACTION_TTL_SECONDS = 10 * 60
PENDING_ACTION_MAX_MESSAGE_DELTA = 3
PENDING_ACTION_COHERENCE_THRESHOLD = 0.40


_REDACTED_MARKERS = ("[REDACTED]", "<REDACTED:")
_TOPIC_STOPWORDS = {
    "a", "al", "and", "de", "del", "el", "en", "for", "la", "las", "lo",
    "los", "me", "mi", "que", "the", "to", "tu", "un", "una", "y", "yo",
}
_PROFILE_CORRECTION_TAGS = ("profile", "correction", "user_direct")


def _profile_fact_slug(value: str) -> str:
    normalized = _normalize_command_text(value)
    tokens = re.findall(r"[a-z0-9]+", normalized)
    return "_".join(tokens[:8]) or "unknown"


def _clean_profile_fact_value(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value)).strip(" .,:;!?¿¡")
    return cleaned[:180]


def _extract_direct_profile_corrections(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """Detect direct user corrections that should survive beyond task outcome memory."""
    if not text or _contains_sensitive_redaction(text):
        return []
    facts: list[tuple[str, str, tuple[str, ...]]] = []
    for match in re.finditer(
        r"(?<!\w)(?P<object>[A-Za-z0-9][A-Za-z0-9_.-]*(?:\s+[A-Za-z0-9][A-Za-z0-9_.-]*){0,5})"
        r"\s+no\s+es\s+mi\s+(?P<kind>proyecto|p[aá]gina|web|sitio)\b",
        text,
        flags=re.IGNORECASE,
    ):
        object_name = _clean_profile_fact_value(match.group("object"))
        if not object_name:
            continue
        kind = _normalize_command_text(match.group("kind"))
        domain = "project" if kind == "proyecto" else "website"
        key = f"profile.{domain}.not_{_profile_fact_slug(object_name)}"
        facts.append(
            (
                key,
                f"{object_name} no es mi {match.group('kind').lower()}.",
                (*_PROFILE_CORRECTION_TAGS, domain),
            )
        )

    repo_match = re.search(
        r"\brepo(?:sitorio)?(?:\s+de\s+mi\s+(?:p[aá]gina|web|sitio))?\s+es\s+"
        r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+|[A-Za-z0-9][^.;\n]{1,120})",
        text,
        flags=re.IGNORECASE,
    )
    if repo_match:
        repo = _clean_profile_fact_value(repo_match.group("repo"))
        if repo:
            facts.append(
                (
                    "profile.website.repo",
                    f"El repo de mi página es {repo}.",
                    (*_PROFILE_CORRECTION_TAGS, "website", "repo"),
                )
            )

    for match in re.finditer(
        r"\bmi\s+(?P<kind>proyecto|p[aá]gina|web|sitio)\s+es\s+(?P<value>[^.;\n]{2,120})",
        text,
        flags=re.IGNORECASE,
    ):
        kind = _normalize_command_text(match.group("kind"))
        domain = "project" if kind == "proyecto" else "website"
        value = _clean_profile_fact_value(match.group("value"))
        if value:
            facts.append(
                (
                    f"profile.{domain}.current",
                    f"Mi {match.group('kind').lower()} es {value}.",
                    (*_PROFILE_CORRECTION_TAGS, domain),
                )
            )

    return facts


def _contains_sensitive_redaction(text: str) -> bool:
    redacted = str(redact_sensitive(text, limit=0))
    return redacted != text or any(marker in redacted for marker in _REDACTED_MARKERS)


def _topic_tokens(text: str) -> list[str]:
    normalized = _normalize_command_text(text)
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]*", normalized)
    return [tok for tok in tokens if len(tok) > 2 and tok not in _TOPIC_STOPWORDS]


def _topic_cosine(left: str, right: str) -> float:
    left_tokens = _topic_tokens(left)
    right_tokens = _topic_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    left_counts: dict[str, float] = {}
    right_counts: dict[str, float] = {}
    for tok in left_tokens:
        left_counts[tok] = left_counts.get(tok, 0.0) + 1.0
    for tok in right_tokens:
        right_counts[tok] = right_counts.get(tok, 0.0) + 1.0
    overlap = set(left_counts) & set(right_counts)
    dot = sum(left_counts[tok] * right_counts[tok] for tok in overlap)
    norm_left = sum(value * value for value in left_counts.values()) ** 0.5
    norm_right = sum(value * value for value in right_counts.values()) ** 0.5
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


@dataclass(slots=True)
class _BrainShortcut:
    text: str
    memory_text: str | None = None


@dataclass(slots=True)
class DelegatedObjectiveResolution:
    """Result of resolving an owner-delegation intent against context."""

    objective: str | None
    resolution_source: str | None  # which slot the objective came from
    mode: str
    is_risky: bool
    selected_option_index: int | None = None
    clarifying_question: str | None = None
    pending_options: list[str] | None = None


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
        self._persist_direct_profile_corrections(session_id, user_text)
        options = _extract_numbered_options(reply_text)
        pending_action = state.get("pending_action")
        extracted_pending_action = _extract_pending_action_from_reply(reply_text)
        pending_action_source: str | None = None
        if extracted_pending_action is not None:
            pending_action = extracted_pending_action
            pending_action_source = "assistant_explicit_step"
        # PR 0D: when the assistant ends with a proposal question
        # ("¿lo arranco?", "¿procedo?", etc.), capture the proposal body as
        # pending_action so the next "ok"/"hazlo tú"/"córrelo tú" has a
        # concrete target to resolve. Previously pending_action was only
        # set when the reply literally contained "siguiente paso:" —
        # which the brain almost never wrote, leaving the DB column at
        # 0/35 non-empty per the 2026-05-16 audit.
        if not (isinstance(pending_action, str) and pending_action.strip()):
            if self._looks_like_proposal_question(reply_text) and not self._looks_like_ledger_status_choice(reply_text):
                proposal = self._summarize_proposal(reply_text)
                if proposal:
                    pending_action = proposal
                    pending_action_source = "assistant_proposal_question"
        if options:
            pending_action = ""
            pending_action_source = None
        # PR 0D: refuse to persist secret-shaped pending_action so a
        # later "ok" cannot replay a token as an objective.
        if isinstance(pending_action, str) and pending_action.strip():
            if _is_secret_shaped_token(pending_action) or _contains_sensitive_redaction(pending_action):
                self._emit(
                    "resolver_state_skipped_sensitive",
                    {"session_id": session_id, "slot": "pending_action"},
                )
                pending_action = ""
                pending_action_source = None
        active_object = dict(state.get("active_object") or {})
        if options:
            active_object["last_options_meta"] = {
                "created_at": time.time(),
                "source": "assistant_numbered_options",
                "topic": _compact_summary(user_text, limit=140) or "",
            }
        if isinstance(pending_action, str) and pending_action.strip() and pending_action_source:
            last_message_id = 0
            try:
                last_message_id = int(self._memory.last_message_id(session_id))
            except Exception:
                last_message_id = 0
            active_object["pending_action_meta"] = {
                "created_at": time.time(),
                "created_message_id": last_message_id,
                "max_message_delta": PENDING_ACTION_MAX_MESSAGE_DELTA,
                "source": pending_action_source,
                "ttl_seconds": PENDING_ACTION_TTL_SECONDS,
                "tier_hint": "unknown",
                "topic": _compact_summary(user_text, limit=140) or "",
            }
            active_object["last_actionable_proposal"] = {
                "objective": pending_action[:500],
                "source": pending_action_source,
                "created_at": time.time(),
                "created_message_id": last_message_id,
            }
        elif not (isinstance(pending_action, str) and pending_action.strip()):
            # Clear stale meta when pending_action is cleared, otherwise the
            # next reload would resurrect an orphaned meta block.
            active_object.pop("pending_action_meta", None)
        last_turn_summary = _compact_summary(reply_text)
        # The assistant's own visible reply is NOT an authoritative verifier
        # signal. session_state.verification_status is owned by the verifier /
        # ledger terminal writers (bot._update_skill_active_task, task_handler);
        # deriving it here would launder a self-claim into a verifier-backed
        # field that source-blind consumers (idle_executor stall logic,
        # morning_brief, task_handler) read as ground truth. So we keep the
        # persisted column at its prior authoritative value and use the
        # reply-derived verdict only as a *self-reported* in-session signal for
        # local task_queue bookkeeping.
        verification_status = state.get("verification_status", "unknown")
        self_reported_status = _extract_verification_status(reply_text)
        checkpoint = _build_checkpoint(reply_text, pending_action=pending_action, verification_status=verification_status)
        if self_reported_status is not None:
            checkpoint["self_reported_status"] = self_reported_status
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
        elif self_reported_status == "passed":
            task_queue = self._task_handler.mark_first_task_queue_entry(task_queue, from_status="in_progress", to_status="done")
            task_queue = self._task_handler.mark_first_task_queue_entry(task_queue, from_status="pending", to_status="done")
        elif self_reported_status == "failed":
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
            last_turn_summary=last_turn_summary,
            active_object=active_object,
        )
        # PR 0D: emit per-slot persistence telemetry so audits can detect
        # write-paths failing to populate slots (cf. 2026-05-16 finding #7).
        if isinstance(pending_action, str) and pending_action.strip():
            self._emit(
                "pending_action_persisted",
                {
                    "session_id": session_id,
                    "source": pending_action_source,
                    "length": len(pending_action),
                },
            )
        if isinstance(task_queue, list) and task_queue:
            self._emit(
                "task_queue_persisted",
                {"session_id": session_id, "entries": len(task_queue)},
            )
        if options:
            self._emit(
                "last_options_persisted",
                {"session_id": session_id, "options": len(options)},
            )

    def _persist_direct_profile_corrections(self, session_id: str, user_text: str) -> None:
        facts = _extract_direct_profile_corrections(user_text)
        if not facts:
            return
        persisted = 0
        for key, value, tags in facts:
            try:
                self._memory.delete_fact(key)
                self._memory.store_fact(
                    key,
                    value,
                    source="direct_user_correction",
                    source_trust="trusted",
                    confidence=0.98,
                    entity_tags=tags,
                    agent_name="profile",
                )
                persisted += 1
            except Exception:
                self._emit(
                    "profile_correction_persist_failed",
                    {"session_id": session_id, "key": key},
                )
        if persisted:
            self._emit(
                "profile_correction_persisted",
                {"session_id": session_id, "facts": persisted},
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
                if not self._pending_action_still_fresh(state, session_id=session_id):
                    self._expire_pending_action(
                        session_id,
                        state,
                        reason="pending_action_stale",
                    )
                    self._emit(
                        "stale_pending_action_rejected",
                        {"session_id": session_id, "source": "pending_action"},
                    )
                    return (
                        "La acción pendiente ya no está vigente. "
                        "Dime en una frase qué acción quieres que ejecute ahora."
                    )
                if not self._pending_action_is_coherent(
                    state,
                    session_id=session_id,
                    approval_text=text,
                ):
                    self._emit(
                        "pending_action_coherence_failed",
                        {"session_id": session_id, "source": "pending_action"},
                    )
                    return (
                        "Tengo una acción pendiente, pero ya no coincide con el tema actual. "
                        "Confirma en una frase la acción exacta que quieres que ejecute."
                    )
                if is_destructive_or_external_objective(pending_action):
                    self._emit(
                        "implicit_approval_requires_explicit_approval",
                        {
                            "session_id": session_id,
                            "source": "pending_action",
                            "pending_action_preview": pending_action[:160],
                        },
                    )
                    return (
                        "Esa acción toca algo externo, destructivo o irreversible. "
                        "No la ejecuto con un ok corto. Confirma explícitamente el alcance "
                        f"si quieres que proceda: {pending_action[:220]}"
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

    def _pending_action_still_fresh(
        self,
        state: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> bool:
        """True iff pending_action has fresh meta (≤ TTL and turn count).

        Pending actions written before PR 0D do not carry a meta block;
        those are treated as fresh (legacy compatibility), so the resolver
        does not break existing sessions on first reload after upgrade.
        Once a new pending_action is persisted with meta, the TTL kicks in.
        """
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return True
        meta = active_object.get("pending_action_meta")
        if not isinstance(meta, dict):
            return True
        created_at = meta.get("created_at")
        try:
            age = time.time() - float(created_at)
        except (TypeError, ValueError):
            return True
        ttl = meta.get("ttl_seconds")
        try:
            ttl_value = float(ttl) if ttl is not None else PENDING_ACTION_TTL_SECONDS
        except (TypeError, ValueError):
            ttl_value = PENDING_ACTION_TTL_SECONDS
        if age > ttl_value:
            return False
        if session_id:
            created_message_id = meta.get("created_message_id")
            try:
                created_id = int(created_message_id)
            except (TypeError, ValueError):
                created_id = 0
            if created_id > 0:
                try:
                    current_id = int(self._memory.last_message_id(session_id))
                except Exception:
                    current_id = created_id
                try:
                    max_delta = int(meta.get("max_message_delta") or PENDING_ACTION_MAX_MESSAGE_DELTA)
                except (TypeError, ValueError):
                    max_delta = PENDING_ACTION_MAX_MESSAGE_DELTA
                if current_id - created_id > max_delta:
                    return False
        return True

    def _expire_pending_action(
        self,
        session_id: str,
        state: dict[str, Any],
        *,
        reason: str,
    ) -> None:
        active_object = dict(state.get("active_object") or {})
        active_object.pop("pending_action_meta", None)
        checkpoint = dict(state.get("last_checkpoint") or {})
        checkpoint.update(
            {
                "summary": "Pending action expired before approval.",
                "verification_status": "blocked",
                "reason": reason,
            }
        )
        self._memory.update_session_state(
            session_id,
            pending_action="",
            active_object=active_object,
            last_checkpoint=checkpoint,
            verification_status="blocked",
        )

    def _pending_action_is_coherent(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        approval_text: str,
    ) -> bool:
        active_object = state.get("active_object") or {}
        meta = active_object.get("pending_action_meta") if isinstance(active_object, dict) else None
        if not isinstance(meta, dict):
            return True
        pending_action = str(state.get("pending_action") or "").strip()
        topic = str(meta.get("topic") or "").strip()
        pending_topic = " ".join(part for part in (topic, pending_action) if part)
        if len(_topic_tokens(pending_topic)) < 2:
            return True
        current_topic = self._current_conversation_topic(
            state,
            session_id=session_id,
            approval_text=approval_text,
        )
        if len(_topic_tokens(current_topic)) < 2:
            return True
        score = _topic_cosine(pending_topic, current_topic)
        self._emit(
            "pending_action_coherence_checked",
            {
                "session_id": session_id,
                "score": round(score, 3),
                "threshold": PENDING_ACTION_COHERENCE_THRESHOLD,
            },
        )
        return score >= PENDING_ACTION_COHERENCE_THRESHOLD

    def _current_conversation_topic(
        self,
        state: dict[str, Any],
        *,
        session_id: str,
        approval_text: str,
    ) -> str:
        chunks: list[str] = []
        current_goal = str(state.get("current_goal") or "").strip()
        if current_goal and current_goal != approval_text.strip():
            chunks.append(current_goal)
        reply_context = self._reply_context_text(state)
        if reply_context:
            chunks.append(reply_context)
        try:
            messages = self._memory.get_recent_messages(session_id, limit=6)
        except Exception:
            messages = []
        assistant_seen = 0
        for message in reversed(messages):
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                if _looks_like_proceed_request(content):
                    continue
                chunks.append(content)
            elif role == "assistant" and assistant_seen < 2:
                chunks.append(content)
                assistant_seen += 1
        return "\n".join(chunks)

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

    def resolve_delegated_objective(
        self,
        *,
        session_id: str,
        text: str,
        intent: OwnerDelegationIntent,
    ) -> DelegatedObjectiveResolution:
        """Resolve "córrelo tú" / "decide tú" / "te toca a ti" against context.

        Resolution order (first non-empty wins):
          1. intent.explicit_action_hint (inline object in the user message)
          2. session_state.pending_action
          3. first in-progress / pending entry in task_queue
          4. session_state.last_checkpoint.pending_action
          5. session_state.active_object.active_task.objective
          6. session_state.last_options (for decision delegation)
          7. recent assistant proposal in memory
          8. session_state.current_goal

        For decision delegation: when options are present and ALL are safe,
        deterministically picks index 0. When any option is risky, returns a
        clarifying question instead.

        When the objective cannot be resolved, returns a single concrete
        clarifying question — never "elige tú" / "decide tú".
        """
        state = self._get_state(session_id)
        self._emit(
            "resolver_state_reloaded",
            {
                "session_id": session_id,
                "has_pending_action": bool((state.get("pending_action") or "").strip()),
                "task_queue_entries": len(state.get("task_queue") or [])
                if isinstance(state.get("task_queue"), list)
                else 0,
                "last_options_count": len(state.get("last_options") or [])
                if isinstance(state.get("last_options"), list)
                else 0,
            },
        )

        # 1. Explicit hint in current text.
        if intent.explicit_action_hint:
            objective = intent.explicit_action_hint.strip()
            return DelegatedObjectiveResolution(
                objective=objective,
                resolution_source="user_text_inline_hint",
                mode=_infer_session_mode(objective),
                is_risky=is_destructive_or_external_objective(objective),
            )

        # Decision delegation goes through last_options first.
        if intent.is_decision_delegation:
            options_resolution = self._resolve_decision_from_options(state)
            if options_resolution is not None:
                return options_resolution

        # 2. pending_action — freshness-gated. A pending_action older than
        # PENDING_ACTION_TTL_SECONDS is treated as stale and ignored so
        # an old "ok" cannot replay obsolete proposals.
        pending = (state.get("pending_action") or "").strip()
        if pending and not _contains_sensitive_redaction(pending) and not _is_secret_shaped_token(pending):
            if self._pending_action_still_fresh(state, session_id=session_id):
                return DelegatedObjectiveResolution(
                    objective=pending,
                    resolution_source="session_state.pending_action",
                    mode=_infer_session_mode(pending),
                    is_risky=is_destructive_or_external_objective(pending),
                )
            self._expire_pending_action(
                session_id,
                state,
                reason="pending_action_stale",
            )
            self._emit(
                "resolver_state_stale_ignored",
                {"session_id": session_id, "slot": "pending_action"},
            )

        # 3. task_queue active/pending entry
        queue = state.get("task_queue") or []
        if isinstance(queue, list):
            preferred_mode = str(state.get("mode") or "chat")
            next_item = _select_next_task_queue_item(queue, preferred_mode=preferred_mode)
            if next_item is not None:
                summary = str(next_item.get("summary") or "").strip()
                if summary and not _contains_sensitive_redaction(summary):
                    return DelegatedObjectiveResolution(
                        objective=summary,
                        resolution_source="session_state.task_queue",
                        mode=str(next_item.get("mode") or preferred_mode),
                        is_risky=is_destructive_or_external_objective(summary),
                    )

        # 4. last_checkpoint pending_action
        checkpoint = state.get("last_checkpoint") or {}
        if isinstance(checkpoint, dict):
            cp_pending = str(checkpoint.get("pending_action") or "").strip()
            if cp_pending and not _contains_sensitive_redaction(cp_pending):
                return DelegatedObjectiveResolution(
                    objective=cp_pending,
                    resolution_source="session_state.last_checkpoint.pending_action",
                    mode=_infer_session_mode(cp_pending),
                    is_risky=is_destructive_or_external_objective(cp_pending),
                )

        # 5. active_object.active_task.objective
        active_object = state.get("active_object") or {}
        if isinstance(active_object, dict):
            active_task = active_object.get("active_task") or {}
            if isinstance(active_task, dict):
                task_obj = str(active_task.get("objective") or "").strip()
                if task_obj:
                    return DelegatedObjectiveResolution(
                        objective=task_obj,
                        resolution_source="session_state.active_object.active_task",
                        mode=str(active_task.get("mode") or "chat"),
                        is_risky=is_destructive_or_external_objective(task_obj),
                    )

        # 6. recent assistant proposal
        reply_context_proposal = self._extract_proposal_from_reply_context(state)
        if reply_context_proposal:
            return DelegatedObjectiveResolution(
                objective=reply_context_proposal,
                resolution_source="reply_context",
                mode=_infer_session_mode(reply_context_proposal),
                is_risky=is_destructive_or_external_objective(reply_context_proposal),
            )

        # 7. recent assistant proposal
        proposal = self._extract_proposal_from_recent_assistant(session_id)
        if proposal:
            return DelegatedObjectiveResolution(
                objective=proposal,
                resolution_source="recent_assistant_proposal",
                mode=_infer_session_mode(proposal),
                is_risky=is_destructive_or_external_objective(proposal),
            )

        # 8. current_goal
        current_goal = (state.get("current_goal") or "").strip()
        if current_goal and len(current_goal) >= 8 and current_goal != text.strip():
            return DelegatedObjectiveResolution(
                objective=current_goal,
                resolution_source="session_state.current_goal",
                mode=_infer_session_mode(current_goal),
                is_risky=is_destructive_or_external_objective(current_goal),
            )

        recent_user_goal = self._extract_recent_user_goal(session_id, exclude=text)
        if recent_user_goal:
            return DelegatedObjectiveResolution(
                objective=recent_user_goal,
                resolution_source="recent_user_goal",
                mode=_infer_session_mode(recent_user_goal),
                is_risky=is_destructive_or_external_objective(recent_user_goal),
            )

        # Nothing resolved — return one concrete clarifying question.
        return DelegatedObjectiveResolution(
            objective=None,
            resolution_source=None,
            mode="chat",
            is_risky=False,
            clarifying_question=(
                "Lo tomo como tuyo, pero necesito una linea concreta: "
                "¿que accion quieres que ejecute? (1 frase imperativa)"
            ),
        )

    def _resolve_decision_from_options(
        self, state: dict[str, Any]
    ) -> DelegatedObjectiveResolution | None:
        options = state.get("last_options") or []
        if not isinstance(options, list) or not options:
            return None
        if not self._last_options_still_valid(state):
            return None
        safe_options: list[str] = []
        any_risky = False
        for raw in options:
            text = str(raw).strip()
            if not text:
                continue
            if is_destructive_or_external_objective(text):
                any_risky = True
            safe_options.append(text)
        if not safe_options:
            return None
        if any_risky:
            preview = "\n".join(f"  {idx + 1}. {opt}" for idx, opt in enumerate(safe_options[:4]))
            return DelegatedObjectiveResolution(
                objective=None,
                resolution_source="last_options_risky",
                mode="chat",
                is_risky=True,
                clarifying_question=(
                    "Hay opciones con efectos externos o destructivos. "
                    "Confirma explicitamente cual ejecuto:\n" + preview
                ),
                pending_options=safe_options,
            )
        chosen = safe_options[0]
        return DelegatedObjectiveResolution(
            objective=chosen,
            resolution_source="last_options_deterministic",
            mode=_infer_session_mode(chosen),
            is_risky=False,
            selected_option_index=0,
            pending_options=safe_options,
        )

    def _get_state(self, session_id: str) -> dict[str, Any]:
        getter = getattr(self._memory, "get_session_state", None)
        if not callable(getter):
            return {}
        try:
            state = getter(session_id)
        except Exception:
            return {}
        return state if isinstance(state, dict) else {}

    def _extract_recent_user_goal(self, session_id: str, *, exclude: str) -> str | None:
        get_recent = getattr(self._memory, "get_recent_messages", None)
        if not callable(get_recent):
            return None
        try:
            messages = get_recent(session_id, limit=8)
        except Exception:
            return None
        excluded = _normalize_command_text(exclude).strip()
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            content = str(message.get("content") or "").strip()
            normalized = _normalize_command_text(content).strip()
            if not content or normalized == excluded:
                continue
            if _looks_like_proceed_request(content):
                continue
            if len(content) >= 12:
                return content[:320]
        return None

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
    _CONTEXTUAL_PROPOSAL_QUESTION_RE = re.compile(
        r"¿\s*(?:sigo|contin[uú]o|procedo|avanzo|voy|arranco)\b[^\n?]{1,260}\?\s*$",
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
        if self._CONTEXTUAL_PROPOSAL_QUESTION_RE.search(tail):
            return True
        # Some proposals close with the question mid-paragraph; scan the
        # whole tail block too.
        tail_block = text[-500:]
        return bool(
            self._PROPOSAL_QUESTION_RE.search(tail_block)
            or self._CONTEXTUAL_PROPOSAL_QUESTION_RE.search(tail_block)
        )

    @staticmethod
    def _looks_like_ledger_status_choice(text: str) -> bool:
        normalized = _normalize_command_text(text)
        return (
            "estatus rapido del ledger" in normalized
            or "resumen del dia" in normalized
            or "activa:" in normalized
        ) and (
            "voy ahora con eso" in normalized
            or "retome alguna otra de las que quedaron perdidas" in normalized
        )

    def _extract_proposal_from_reply_context(self, state: dict[str, Any]) -> str | None:
        text = self._reply_context_text(state)
        if not text:
            return None
        pending = _extract_pending_action_from_reply(text)
        if pending:
            return pending
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
            pending = _extract_pending_action_from_reply(content)
            if pending:
                return pending
            if self._looks_like_proposal_question(content):
                return self._summarize_proposal(content)
        return None

    def _summarize_proposal(self, text: str) -> str:
        contextual_question = self._extract_contextual_proposal_question(text)
        # Strip the closing question, keep the substantive body.
        cleaned = self._PROPOSAL_QUESTION_RE.sub("", text).strip()
        cleaned = self._CONTEXTUAL_PROPOSAL_QUESTION_RE.sub("", cleaned).strip()
        if not cleaned:
            cleaned = text.strip()
        # Collapse whitespace, cap length so it fits inside a brain hint.
        compact = " ".join(cleaned.split())
        if contextual_question:
            contextual_compact = " ".join(contextual_question.strip().strip("¿?").split())
            if compact and compact != " ".join(text.strip().split()):
                compact = f"{contextual_compact}. Contexto previo: {compact}"
            else:
                compact = contextual_compact
        if len(compact) > 320:
            compact = compact[:317] + "..."
        return compact

    def _extract_contextual_proposal_question(self, text: str) -> str | None:
        if not text:
            return None
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates = []
        if lines:
            candidates.append(lines[-1])
        candidates.append(text[-500:])
        for candidate in candidates:
            match = self._CONTEXTUAL_PROPOSAL_QUESTION_RE.search(candidate)
            if match:
                return match.group(0)
        return None

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
