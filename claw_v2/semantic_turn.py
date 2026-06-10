from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from claw_v2.bot_helpers import (
    _extract_option_reference,
    _looks_like_proceed_request,
    _normalize_command_text,
    detect_meta_introspection_request,
    detect_owner_delegation,
    detect_telegram_imperative,
    has_explicit_implementation_request,
)


SemanticIntent = Literal[
    "new_task",
    "continue_active_mission",
    "approval_response",
    "correction_or_behavior_instruction",
    "question",
    "debug_request",
]


@dataclass(frozen=True, slots=True)
class SemanticTurn:
    intent: SemanticIntent
    objective: str | None
    confidence: float
    clear_goal: bool
    explicit_authorization: bool
    explicit_continuation: bool
    debug_mode: bool
    reasons: tuple[str, ...] = ()


_APPROVAL_ONLY = {
    "aprobada",
    "aprobado",
    "apruebalo",
    "apruebala",
    "aprobalo",
    "aprobala",
    "autorizado",
    "autorizada",
    "confirmado",
    "confirmada",
    "confirmo",
    "approved",
    "approve",
    "yes approved",
}

_QUESTION_PREFIXES = (
    "que ",
    "qué ",
    "cual ",
    "cuál ",
    "como ",
    "cómo ",
    "cuando ",
    "cuándo ",
    "donde ",
    "dónde ",
    "por que ",
    "por qué ",
    "porque ",
    "why ",
    "what ",
    "how ",
    "when ",
    "where ",
)

_DEBUG_MARKERS = (
    "debug",
    "audita",
    "auditar",
    "auditoria",
    "auditoría",
    "audit ",
    "logs",
    "trace",
    "traza",
    "trazas",
    "observability",
    "observe_stream",
)

_CORRECTION_MARKERS = (
    "cuando te diga",
    "cuando te pida",
    "no vuelvas",
    "no respondas",
    "no me digas",
    "debes entender",
    "tienes que entender",
    "tenes que entender",
    "corrige tu comportamiento",
    "aprende que",
    "recuerda que",
    "no continuemos",
    "no sigamos",
    "no avancemos",
    "paremos",
    "detengamos",
    "dejemos esto",
)

_NEW_TASK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:crea|crear|creame|créame|genera|generame|haz|hazme|prepara|armame|arma)\b.+\b(?:mision|misión|tarea|task|smoke|prueba|test|plan|replay|cuaderno|notebook|notebooklm|podcast|noticias|barrido|research|audit|auditoria|mcp|mcps)\b",
        r"\b(?:implementa|parchea|corrige|arregla|agrega|modifica|actualiza|regenera|completa|termina|finaliza)\b",
        r"\b(?:verifica|comprueba|valida)\b.+\b(?:daemon|servicio|launchd|runtime|bot|cuaderno|notebook|notebooklm|tarea|task)\b",
        r"\b(?:verifica|comprueba|valida)\b.+\b(?:cifras|fuentes?|fuente primaria|websearch|estadisticas|estadísticas|drafts?|asset|publicar)\b",
        r"\b(?:afina|afinar|afinalo|refina|refinar|refinalo|mejora|mejorar|mejoralo|optimiza|optimizar|pule|pulir|ajusta|ajustar)\b.+\b(?:crea|crear|cree|genera|generar|genere)\b.+\b(?:imagen(?:es)?|assets?|grid|carrusel(?:es)?|portadas?)\b",
        r"\b(?:crea|crear|cree|genera|generar|genere)\b.+\b(?:imagen(?:es)?|assets?|grid|carrusel(?:es)?|portadas?)\b",
        r"\b(?:investiga|revisa|audita)\b.+\b(?:bug|fallo|falla|logs?|trazas?|router|runtime|telegram|test|smoke|mcp|mcps)\b",
        r"\b(?:create|generate|prepare|implement|patch|fix|add|modify|update|complete|finish|review|investigate|audit)\b",
    )
)

_CONTEXTUAL_CONTINUATION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"^(?:publicalo|publicala|publicalos|publicalas|publica\s+(?:esto|eso|lo|la))$",
        r"^(?:lee|leer|read)\s+(?:los?\s+)?(?:docs?|documentos?)$",
        r"^listo\s+(?:logueado|loggeado|logged\s+in)$",
        r"^(?:arranca|arrancar|empieza|inicia)\s+con\s+(?:el\s+)?plan$",
        r"^(?:ok|okay|dale|va|listo)\s+\d+$",
    )
)


def classify_semantic_turn(text: str) -> SemanticTurn:
    stripped = (text or "").strip()
    normalized = _normalize_command_text(stripped).strip()
    compact = re.sub(r"[^a-z0-9\s]+", " ", normalized)
    compact = re.sub(r"\s+", " ", compact).strip()
    reasons: list[str] = []

    if not stripped:
        return SemanticTurn(
            intent="question",
            objective=None,
            confidence=0.4,
            clear_goal=False,
            explicit_authorization=False,
            explicit_continuation=False,
            debug_mode=False,
            reasons=("empty_turn",),
        )

    debug_mode = _asks_for_debug_or_audit(normalized)
    explicit_continuation = _looks_like_proceed_request(stripped)
    explicit_authorization = _is_explicit_authorization(compact)
    option_reference = _extract_option_reference(stripped)
    option_combo = _looks_like_option_combo(compact)
    telegram_intent = detect_telegram_imperative(stripped)
    owner_delegation = detect_owner_delegation(stripped)
    meta_intent = detect_meta_introspection_request(stripped)

    if _looks_like_new_task(normalized, stripped):
        reasons.append("clear_goal_action_pattern")
        if debug_mode:
            reasons.append("debug_words_inside_new_task")
        return SemanticTurn(
            intent="new_task",
            objective=stripped,
            confidence=0.93,
            clear_goal=True,
            explicit_authorization=False,
            explicit_continuation=False,
            debug_mode=debug_mode,
            reasons=tuple(reasons),
        )

    if explicit_authorization:
        return SemanticTurn(
            intent="approval_response",
            objective=None,
            confidence=0.92,
            clear_goal=False,
            explicit_authorization=True,
            explicit_continuation=explicit_continuation,
            debug_mode=debug_mode,
            reasons=("approval_phrase",),
        )

    if option_reference is not None or option_combo:
        return SemanticTurn(
            intent="continue_active_mission",
            objective=None,
            confidence=0.9,
            clear_goal=False,
            explicit_authorization=False,
            explicit_continuation=True,
            debug_mode=debug_mode,
            reasons=("option_reference",) if option_reference is not None else ("option_combo",),
        )

    if (
        explicit_continuation
        or (telegram_intent is not None and telegram_intent.intent == "task.continue_active_mission")
        or (owner_delegation is not None and owner_delegation.requires_resolution)
        or _looks_like_contextual_continuation(normalized)
    ):
        return SemanticTurn(
            intent="continue_active_mission",
            objective=None,
            confidence=0.9,
            clear_goal=False,
            explicit_authorization=False,
            explicit_continuation=True,
            debug_mode=debug_mode,
            reasons=("continuation_phrase",),
        )

    if debug_mode or (meta_intent is not None and meta_intent.kind == "audit"):
        return SemanticTurn(
            intent="debug_request",
            objective=stripped,
            confidence=0.86,
            clear_goal=bool(stripped),
            explicit_authorization=False,
            explicit_continuation=False,
            debug_mode=True,
            reasons=("debug_or_audit_request",),
        )

    if _looks_like_correction(normalized):
        return SemanticTurn(
            intent="correction_or_behavior_instruction",
            objective=stripped,
            confidence=0.82,
            clear_goal=False,
            explicit_authorization=False,
            explicit_continuation=False,
            debug_mode=debug_mode,
            reasons=("behavior_instruction",),
        )

    if _looks_like_question(stripped, normalized) or (meta_intent is not None and meta_intent.kind == "meta"):
        return SemanticTurn(
            intent="question",
            objective=stripped,
            confidence=0.78,
            clear_goal=False,
            explicit_authorization=False,
            explicit_continuation=False,
            debug_mode=debug_mode,
            reasons=("question_shape",),
        )

    return SemanticTurn(
        intent="question",
        objective=stripped,
        confidence=0.52,
        clear_goal=False,
        explicit_authorization=False,
        explicit_continuation=False,
        debug_mode=debug_mode,
        reasons=("default_chat_or_question",),
    )


def _looks_like_new_task(normalized: str, original: str) -> bool:
    if not normalized or original.startswith("/"):
        return False
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if "mision durable" in normalized or "mission durable" in normalized or "durable mission" in normalized:
        return True
    if has_explicit_implementation_request(original):
        return True
    if _looks_like_question(original, normalized):
        return False
    return any(pattern.search(normalized) for pattern in _NEW_TASK_PATTERNS)


def _is_explicit_authorization(compact: str) -> bool:
    if compact in _APPROVAL_ONLY:
        return True
    return bool(
        re.fullmatch(
            r"(?:te\s+)?(?:autorizo|apruebo|confirmo)(?:\s+(?:esto|eso|la accion|el paso))?",
            compact,
        )
    )


def _looks_like_option_combo(compact: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:opcion\s+)?[a-e](?:\s+(?:(?:y|and|e|\+|,)\s+)?(?:opcion\s+)?[a-e])+",
            compact,
        )
    )


def _looks_like_contextual_continuation(normalized: str) -> bool:
    if not normalized:
        return False
    normalized = re.sub(r"\s+", " ", normalized).strip(" \t\n\r.,;:!?¿¡")
    return any(pattern.search(normalized) for pattern in _CONTEXTUAL_CONTINUATION_PATTERNS)


def _looks_like_question(original: str, normalized: str) -> bool:
    if "?" in original or "¿" in original:
        return True
    return normalized.startswith(_QUESTION_PREFIXES)


def _asks_for_debug_or_audit(normalized: str) -> bool:
    return any(marker in normalized for marker in _DEBUG_MARKERS)


def _looks_like_correction(normalized: str) -> bool:
    return any(marker in normalized for marker in _CORRECTION_MARKERS)
