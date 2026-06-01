"""Module-level helper functions and constants extracted from bot.py."""
from __future__ import annotations

import contextlib
import re
import unicodedata
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from dataclasses import dataclass

from claw_v2.coordinator import CoordinatorResult, WorkerTask
from claw_v2.tracing import new_trace_context
from claw_v2.turn_context import (  # P0-B: re-export the turn_id helpers from bot_helpers for callers that already pull from this module.
    current_turn_id,
    new_turn_id,
    turn_id_context,
)

_CRITICAL_WORKER_ERROR_PREFIX = "critical_worker_error:"
_CRITICAL_WORKER_VISIBLE_MESSAGE = (
    "No pude avanzar la tarea porque el subagente experimentó un error crítico en el entorno local. "
    "Activé la bitácora de contingencia técnica y dejé registrada la reparación necesaria."
)

__all__ = [
    "current_meta_introspection_kind",
    "current_turn_id",
    "meta_introspection_context",
    "new_turn_id",
    "turn_id_context",
    "MetaIntrospectionIntent",
    "OwnerDelegationIntent",
    "TelegramImperativeIntent",
    "detect_meta_introspection_request",
    "detect_owner_delegation",
    "detect_telegram_imperative",
    "has_explicit_implementation_request",
    "is_destructive_or_external_objective",
    "looks_like_actionable_telegram_message",
    "_AUTH_DOMAINS",
    "_AUTONOMY_ACTION_PATTERNS",
    "_AUTONOMY_MODES",
    "_AUTONOMY_POLICY_MATRIX",
    "_AUTONOMY_TASK_ACTION_PATTERNS",
    "_BROWSE_SHORTCUT_TOKENS",
    "_COMPUTER_ACTION_TOKENS",
    "_COMPUTER_READ_TOKENS",
    "_HOST_URL_RE",
    "_JS_RENDERED_DOMAINS",
    "_LINK_ANALYSIS_SECTION_TITLES",
    "_LINK_ANALYSIS_SHORTCUT_TOKENS",
    "_LOGIN_WALL_SIGNALS",
    "_NLM_ACTION_TOKENS",
    "_NLM_ARTIFACT_KINDS",
    "_NLM_CREATE_RE",
    "_OPTION_ORDINALS",
    "_PROCEED_TOKENS",
    "_RAW_MARKUP_SIGNALS",
    "_RUNTIME_CAPABILITY_SECTION_TITLES",
    "_SCHEME_URL_RE",
    "_TWEET_ANALYSIS_SHORTCUT_TOKENS",
    "_agent_summary",
    "_autonomy_policy_payload",
    "_build_checkpoint",
    "_build_coordinator_tasks",
    "_bulletize_link_analysis_body",
    "_bulletize_runtime_capability_body",
    "_classify_task_actions",
    "_compact_summary",
    "_computer_instruction_requires_actions",
    "_coordinator_checkpoint",
    "_default_pull_request_body",
    "_default_pull_request_title",
    "_default_step_budget",
    "_enforce_link_analysis_sections",
    "_enforce_runtime_capability_sections",
    "_enrich_tweet_urls",
    "_evaluate_autonomy_policy",
    "_extract_link_analysis_context",
    "_extract_nlm_artifact_kind",
    "_extract_nlm_create_topic",
    "_extract_numbered_options",
    "_extract_option_reference",
    "_extract_pending_action_from_reply",
    "_extract_ratio_context_from_text",
    "_extract_title_from_url",
    "_extract_url_candidate",
    "_extract_verification_status",
    "_filter_task_queue_by_mode",
    "_format_autonomy_policy_block",
    "_format_chrome_cdp_error",
    "_format_computer_pending_summary",
    "_format_coordinator_response",
    "_format_link_analysis_prompt",
    "_format_runtime_capability_prompt",
    "_format_task_approval_response",
    "_format_tweet_analysis_prompt",
    "_format_worker_results",
    "_extract_url_candidates",
    "_has_link_analysis_sections",
    "_has_runtime_capability_sections",
    "_has_url_query",
    "_infer_session_mode",
    "_is_local_url",
    "_is_login_wall",
    "_is_tweet_url",
    "_is_url_echo",
    "_is_usable_browse_content",
    "_jina_read",
    "_looks_like_api_key",
    "_looks_like_autonomy_grant",
    "_looks_like_computer_read_request",
    "_looks_like_proceed_request",
    "_looks_like_ratio_reference_request",
    "_looks_like_raw_markup",
    "_looks_like_runtime_capability_question",
    "_looks_like_standalone_url",
    "_looks_like_tweet_followup_request",
    "_matched_named_actions",
    "_matched_policy_actions",
    "_needs_real_browser",
    "_normalize_command_text",
    "_normalize_url",
    "_nested_url_candidates",
    "_parse_autonomy_mode",
    "_parse_float",
    "_parse_non_negative_int",
    "_parse_positive_int",
    "_parse_toggle",
    "_policy_for_mode",
    "_read_env_var_from_shell_files",
    "_record_summary",
    "_run_summary",
    "_select_navigation_strategy",
    "_select_next_task_queue_item",
    "_sanitize_chat_response",
    "_chat_response_has_internal_leak",
    "_strip_url_permission_deferrals",
    "_stable_task_id",
    "_strip_url_punctuation",
    "_summarize_prefetched_link_content",
    "_task_approval_summary",
    "_task_queue_item_ready",
    "_tweet_fxtwitter_read",
    "_tweet_oembed_fallback",
    "verify_brain_tooluse",
    "_help_response",
]


_META_INTROSPECTION_CONTEXT: ContextVar[str | None] = ContextVar(
    "claw_meta_introspection_context", default=None
)

_VERIFICATION_STATUS_RE = re.compile(
    r"(?:verificado|verified|verification\s+status)[\s:*\-–—]*"
    r"(ok|passed|pending|failed|none|unknown)\b",
    re.IGNORECASE,
)


@contextlib.contextmanager
def meta_introspection_context(kind: str) -> Iterator[None]:
    """Mark the current turn as routed by ``meta_introspection_guard``.

    While active, the evidence-gate skips ledger row creation and the
    user-visible reply replacement so brain answers to critiques, audits, or
    clarification asks pass through intact. A dedicated observability event
    (`evidence_gate_skipped_meta`) still fires so the self-improvement loop
    keeps the signal.
    """
    token = _META_INTROSPECTION_CONTEXT.set(kind)
    try:
        yield
    finally:
        _META_INTROSPECTION_CONTEXT.reset(token)


def current_meta_introspection_kind() -> str | None:
    return _META_INTROSPECTION_CONTEXT.get()


_BROWSE_SHORTCUT_TOKENS = (
    "abre",
    "abrir",
    "open",
    "revisa",
    "revisalo",
    "review",
    "check",
    "lee",
    "read",
    "analiza",
    "analyze",
    "visita",
    "visit",
    "navega",
    "browse",
)
_TWEET_ANALYSIS_SHORTCUT_TOKENS = (
    "revisa",
    "revisalo",
    "review",
    "check",
    "lee",
    "read",
    "analiza",
    "analyze",
)
_LINK_ANALYSIS_SHORTCUT_TOKENS = _TWEET_ANALYSIS_SHORTCUT_TOKENS
_COMPUTER_ACTION_TOKENS = (
    "haz click",
    "haz clic",
    "dale click",
    "dale clic",
    "click on",
    "click en",
    "clic en",
    "scroll down",
    "scroll up",
    "scroll to",
    "desplaza hacia",
    "desplazate hacia",
    "type in",
    "type into",
    "escribe en",
    "press enter",
    "press tab",
    "presiona enter",
    "presiona tab",
    "selecciona el",
    "selecciona la",
    "selecciona un",
    "drag and drop",
    "arrastra el",
    "arrastra la",
    "abre el menu",
    "open the menu",
)
_COMPUTER_READ_TOKENS = (
    "revisa la pagina actual",
    "revisa la pantalla",
    "revisa esta pagina",
    "revisa la campana",
    "revisa la campaña",
    "que ves",
    "que hay en la pantalla",
    "dime que ves",
    "describe la pantalla",
)
_NLM_CREATE_RE = re.compile(
    r"^\s*(?:por favor\s+)?(?:(?:ahora|ya|tambi[eé]n|tb)\s+)?"
    r"(?:(?:cr[eé]a(?:me)?)|(?:genera(?:me)?)|(?:haz(?:me)?)|"
    r"(?:armame|arma)|(?:prep[aá]rame|prepara)|(?:p[oó]nme|pon)|"
    r"(?:lanza(?:me)?)|(?:dispara(?:me)?)|"
    r"quiero|necesito|dame|produce(?:me)?)\s+"
    r"(?:un\s+|el\s+|otro\s+)?(?:cuaderno|notebook|nb)(?:\s+(?:en\s+notebooklm))?"
    r"[,;:]?(?:\s+(?:sobre|de(?:l)?|acerca\s+de|para|con))?[,;:]?\s+(.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_NLM_VOICE_PREFIX_RE = re.compile(r"^\s*\[\s*nota\s+de\s+voz\s*\]\s*:?\s*", re.IGNORECASE)
_NLM_ARTIFACT_KINDS = {
    "podcast": "podcast",
    "audio del cuaderno": "podcast",
    "audio del notebook": "podcast",
    "resumen en audio": "podcast",
    "infografia": "infographic",
    "infographic": "infographic",
    "infografica": "infographic",
}
_NLM_ACTION_TOKENS = (
    "crea",
    "creame",
    "genera",
    "generame",
    "haz",
    "hazme",
    "quiero",
    "necesito",
    "armame",
    "arma",
    "preparame",
    "prepara",
    "ponme",
    "pon",
    "lanza",
    "lanzame",
    "dispara",
    "disparame",
    "dame",
    "produce",
    "produceme",
)
_NLM_META_DISCUSSION_PHRASES = (
    "cuando te pido",
    "cuando te diga",
    "cuando te pida",
    "como te pido",
    "como te diga",
    "si te pido",
    "si te digo",
    "no me digas",
    "no me pidas",
    "no quiero",
    "tienes que entender",
    "tienes que saber",
    "tenes que entender",
    "tenes que saber",
    "deberias entender",
    "deberias saber",
    "explica",
    "explicame",
    "porque salio",
    "por que salio",
    "porque sigues",
    "por que sigues",
    "revisa los errores",
    "revisa el error",
    "el error",
    "los errores",
    "que paso con",
    "que pasa con",
    "ampliar el vocabulario",
    "haz los fixes",
    "los fixes",
    "fix permanente",
    "fixes que sean",
)
_NLM_NOTEBOOK_REFS = ("cuaderno", "notebook", "notebooklm", " nb ")
_SCHEME_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()]+)", re.IGNORECASE)
_HOST_URL_RE = re.compile(
    r"(?P<url>(?<!@)(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})(?::\d+)?(?:[/?#][^\s<>()]*)?)",
    re.IGNORECASE,

)
_RUNTIME_CAPABILITY_SECTION_TITLES = (
    "Implementado hoy",
    "Parcial",
    "Sugerencia",
)
_LINK_ANALYSIS_SECTION_TITLES = (
    "Fuente",
    "Aplicación sugerida",
)
_AUTONOMY_MODES = ("manual", "assisted", "autonomous")
_PROCEED_TOKENS = (
    "procede",
    "proceder",
    "ejecuta",
    "ejecutalo",
    "ejecutala",
    "continue",
    "continua",
    "continuar",
    "sigue",
    "seguir",
    "dale",
    "hazlo",
    "haz la prueba",
    "haz prueba",
    "haz esa prueba",
    "haz esa",
    "hazlo ya",
    "hagamoslo",
    "hagamoslo con",
    "hagamos eso",
    "ok",
    "okay",
    "si",
    "sí",
    "yes",
    "asi",
    "vale",
    "avanza",
    "adelante",
)
_OPTION_ORDINALS = {
    "a": 1,
    "b": 2,
    "c": 3,
    "d": 4,
    "e": 5,
    "primera": 1,
    "first": 1,
    "segunda": 2,
    "second": 2,
    "tercera": 3,
    "third": 3,
    "cuarta": 4,
    "fourth": 4,
    "quinta": 5,
    "fifth": 5,
}
_AUTONOMY_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "deploy": (r"\bdeploy\b", r"\bdespliega\b", r"\bproduction\b", r"\bprod\b"),
    "publish": (r"\bpublica\b", r"\bpublish\b", r"\btweet\b", r"\bpost\b"),
    "destructive": (r"\bdelete\b", r"\bborra\b", r"\belimina\b", r"\brm\s+-", r"\bdrop\s+table\b", r"\btruncate\b"),
}
_AUTONOMY_TASK_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "inspect": (r"\brevisa\b", r"\binspect\b", r"\banaliza\b", r"\bdebug\b", r"\bcheck\b"),
    "edit": (r"\bcorrige\b", r"\barregla\b", r"\bfix\b", r"\bimplementa\b", r"\bedit\b", r"\bpatch\b"),
    "test": (r"\btest\b", r"\bpytest\b", r"\bverifica\b", r"\bverify\b"),
    "commit": (r"\bcommit\b", r"\bcommitea\b"),
    "push": (r"\bgit\s+push\b", r"\bpush\b", r"\bpushea\b", r"\bempuja\b"),
    "research": (r"\binvestiga\b", r"\bresearch\b", r"\bfind\b", r"\bgather\b"),
    "summarize": (r"\bresume\b", r"\bsummariza\b", r"\bsummary\b", r"\bsintetiza\b"),

}
_AUTONOMY_POLICY_MATRIX: dict[str, dict[str, Any]] = {
    "manual": {
        "automatic_coordinator_modes": [],
        "blocked_actions": ("deploy", "publish", "destructive"),
        "approval_required_actions": ("commit", "push"),
        "allowed_task_actions": (),
        "notes": [
            "No coordinated execution runs automatically in manual mode.",
            "Use this mode when you want explicit confirmation before non-trivial work.",
        ],
    },
    "assisted": {
        "automatic_coordinator_modes": [],
        "blocked_actions": ("deploy", "publish", "destructive"),
        "approval_required_actions": ("commit", "push"),
        "allowed_task_actions": ("inspect", "edit", "test", "research", "summarize"),
        "notes": [
            "Coordinated runs require explicit `/task_run` in assisted mode.",
            "Commit and git push require confirmation. Publish, deploy, and destructive actions remain blocked.",
        ],
    },
    "autonomous": {
        "automatic_coordinator_modes": ["coding", "research"],
        "blocked_actions": ("deploy", "publish", "destructive"),
        "approval_required_actions": (),
        "allowed_task_actions": ("inspect", "edit", "test", "commit", "push", "research", "summarize"),
        "notes": [
            "Autonomous coordinator runs are limited to coding and research.",
            "Operational/browser/authenticated flows stay outside the autonomous coordinator path.",
            "Commit and git push are allowed for development tasks after local verification. Publish, deploy, and destructive actions remain blocked.",
        ],
    },
}


def _parse_toggle(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "true", "1", "yes"}:
        return True
    if normalized in {"off", "false", "0", "no"}:
        return False
    raise ValueError("toggle must be one of: on, off")


def _parse_autonomy_mode(value: str) -> str:
    normalized = _normalize_command_text(value).strip()
    if normalized not in _AUTONOMY_MODES:
        raise ValueError("autonomy mode must be one of: manual, assisted, autonomous")
    return normalized


def _default_step_budget(autonomy_mode: str) -> int:
    if autonomy_mode == "manual":
        return 1
    if autonomy_mode == "autonomous":
        return 4
    return 2


def _strip_voice_prefix(text: str) -> str:
    return _NLM_VOICE_PREFIX_RE.sub("", text, count=1)


def _extract_nlm_create_topic(text: str) -> str | None:
    cleaned = _strip_voice_prefix(text).strip()
    if _looks_like_nlm_meta_discussion(cleaned):
        return None
    match = _NLM_CREATE_RE.match(cleaned)
    if not match:
        return None
    topic = match.group(1).strip()
    return topic or None


def _extract_nlm_artifact_kind(text: str) -> str | None:
    cleaned = _strip_voice_prefix(text)
    normalized = _normalize_command_text(cleaned)
    if _looks_like_nlm_meta_discussion(cleaned):
        return None
    if not any(token in normalized for token in _NLM_ACTION_TOKENS):
        return None
    word_count = len(normalized.split())
    has_notebook_ref = any(ref.strip() in normalized for ref in _NLM_NOTEBOOK_REFS)
    # Long messages without an explicit notebook reference are almost always
    # meta-discussion or unrelated content that happens to mention "podcast".
    if word_count > 30 and not has_notebook_ref:
        return None
    for token, kind in _NLM_ARTIFACT_KINDS.items():
        if token in normalized:
            idx = normalized.find(token)
            preceding = normalized[max(0, idx - 40):idx]
            if any(meta in preceding for meta in (
                "cuando te pido",
                "cuando te diga",
                "cuando te pida",
                "como te pido",
                "si te pido",
                "te pido",
            )):
                continue
            return kind
    return None


def _looks_like_nlm_meta_discussion(text: str) -> bool:
    """Reject messages that *talk about* notebook/podcast intents but are not
    direct requests. Examples: "cuando te pido podcast hazlo asi",
    "no me digas que no podes crear el cuaderno", "explicame porque sale el
    error al crear el cuaderno".
    """
    normalized = _normalize_command_text(text)
    return any(phrase in normalized for phrase in _NLM_META_DISCUSSION_PHRASES)


def _normalize_command_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


@dataclass(frozen=True)
class MetaIntrospectionIntent:
    """Result of detecting a non-actionable meta/reflective/audit prompt.

    `kind` is one of:
        - "meta":  reflective question about the bot's behavior or
                   clarification of intent.
        - "audit": request to inspect logs / traces / past failures.
        - "non_actionable_token": input looks like an opaque secret-shaped
                   token rather than an objective.
    """

    kind: str
    normalized_text: str
    reason: str


# Patterns are matched against `_normalize_command_text(text)`, which
# lowercases and strips diacritics. Use ASCII-only literals.
_META_INTROSPECTION_PATTERNS_ES: tuple[str, ...] = (
    # "por que" (interrogative) and "porque" (conjunction/typo) both reach
    # here — accept both because users mix them freely in chat.
    r"\b(?:por que|porque) no (?:completas|completaste|terminas|terminaste|haces|hiciste|puedes|pudiste|frenas|paraste|cierras|cerraste)\b",
    r"\b(?:por que|porque) (?:fallaste|fallas|fallo|te frenas|frenas|te detienes|te detuviste)\b",
    r"\bcual fue la causa\b",
    r"\b(?:que|cual) fue el problema\b",
    r"\b(?:entendiste|me entendiste|entiendes)\b",
    r"\bdime si (?:esta lectura|esta interpretacion|este resumen|esta respuesta|este analisis) (?:es correcta|es correcto|esta bien)\b",
    r"\banaliza (?:esta conversacion|este chat|esta respuesta|este comportamiento|esta interaccion)\b",
    r"\brevisa (?:el chat|esta conversacion|esta respuesta|este comportamiento|el historial)\b",
    r"\bque opinas (?:de|sobre) (?:esta|este|esa|ese|tu|la|el)\b",
    r"\bque (?:queremos|quieres|quiero) (?:comunicar|decir|expresar|transmitir)\b",
)
_META_INTROSPECTION_PATTERNS_EN: tuple[str, ...] = (
    r"\bwhy did you (?:fail|stop|skip|miss|not finish|not complete|freeze)\b",
    r"\bwhat went wrong\b",
    r"\b(?:do|did) you understand\b",
    r"\banalyze (?:this conversation|this chat|this behavior|this response|this interaction)\b",
    r"\breview (?:this conversation|this chat|this response|this behavior|the history)\b",
    r"\bis this (?:reading|interpretation|summary|response|analysis) correct\b",
    r"\bwhat are we (?:trying to|going to|here to) (?:communicate|say|express|convey)\b",
)
_META_AUDIT_PATTERNS: tuple[str, ...] = (
    r"\binvestiga (?:los logs|el log|la traza|el bug|esta falla|este error|esta tarea fallida|la falla)\b",
    r"\bauditar?(?:la|lo|el|los|esta|este) (?:falla|error|tarea|log|traza)\b",
    r"\brevisa los logs\b",
    r"\bdebug (?:this|the) (?:log|logs|trace|failure|error)\b",
    r"\binspect (?:the|this) (?:log|logs|trace|failure|error)\b",
)
_EXPLICIT_IMPLEMENTATION_PATTERNS: tuple[str, ...] = (
    # Spanish imperative implementation verbs.
    r"\bimplementa\b",
    r"\bparchea\b",
    r"\bagrega (?:un |unos |los )?tests?\b",
    r"\bmodifica (?:el|la|los|las|este|esta)\b",
    r"\bcorrige (?:el|la|los|las|este|esta)\b",
    r"\baplica (?:el|este|ese|un) (?:cambio|fix|patch|parche)\b",
    r"\bescribe (?:el|un) (?:patch|parche|fix|codigo)\b",
    r"\bcrea (?:el|un) (?:pr|pull request|commit|patch|parche)\b",
    # English imperative implementation verbs.
    r"\bimplement (?:the|this|a)\b",
    r"\bpatch \w",
    r"\badd (?:a |the |some |more )?tests?\b",
    r"\bmodify (?:the|this)\b",
    r"\bfix (?:the|this|that) (?:bug|issue|test|build|error)\b",
    r"\bapply (?:the|this|that) (?:change|fix|patch)\b",
    r"\bwrite (?:the|a) (?:patch|fix|code)\b",
    r"\bcreate (?:the|a) (?:pr|pull request|commit|patch)\b",
)


def has_explicit_implementation_request(text: str) -> bool:
    """True iff the message contains an unambiguous request to write/modify code.

    Used to prevent the meta-introspection guard from blocking real
    implementation work. Past-tense conjugations ("implementaste",
    "parcheaste") do not match — only imperative / present forms do, so a
    question about prior implementation is still treated as meta.
    """
    if not text:
        return False
    normalized = _normalize_command_text(text)
    return any(re.search(pattern, normalized) for pattern in _EXPLICIT_IMPLEMENTATION_PATTERNS)


def _is_secret_shaped_token(text: str) -> bool:
    """True iff the input is a single high-entropy token rather than a sentence.

    Matches things like `8eyt8R1Hp008liTCA98a` (mixed-case alphanumeric, no
    spaces, length >= 16). Avoids false positives on common task IDs
    (`tg-574707975:1778533984299303000`) because those contain `:` and have
    no mixed case.
    """
    stripped = (text or "").strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        return False
    if len(stripped) < 16:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", stripped):
        return False
    has_upper = any(c.isupper() for c in stripped)
    has_lower = any(c.islower() for c in stripped)
    has_digit = any(c.isdigit() for c in stripped)
    return has_upper and has_lower and has_digit


@dataclass(frozen=True)
class OwnerDelegationIntent:
    """Result of detecting an owner-delegation phrase.

    `kind` is one of:
        - "execution":    user wants the bot to RUN the pending/proposed action
                          ("córrelo tú", "hazlo tu", "ejecútalo tú", "run it
                          yourself"). Usually requires resolving WHAT to run.
        - "decision":     user wants the bot to CHOOSE among options ("decide
                          tú", "you decide", "tú decides").
        - "no_manual_work": user is refusing further manual back-and-forth
                          ("te toca a ti", "encárgate tú", "ya no tengo que
                          teclear nada", "stop asking me to run commands").

    `requires_resolution` is True when the verb is pronoun-bound and needs
    context to know WHAT to do.

    `explicit_action_hint` carries any object/verb that appeared inline with
    the delegation (e.g. "encárgate tú DE actualizar el deck" → hint =
    "actualizar el deck"). When set, the resolver can use it directly
    without searching session state.
    """

    kind: str
    confidence: float
    normalized_text: str
    requires_resolution: bool
    is_execution_delegation: bool = False
    is_decision_delegation: bool = False
    is_no_manual_work_delegation: bool = False
    explicit_action_hint: str | None = None
    lexical_score: float = 0.0
    direction_score: float = 0.0


@dataclass(frozen=True)
class TelegramImperativeIntent:
    """Normalized operator command for Telegram imperative routing.

    ``intent`` is deliberately narrower than "do task": UI focus, clipboard,
    paste, submit, inspect, and continuation have different safety/capability
    requirements. ``needs_context`` means the current message names a pronoun
    or generic object ("la app", "el prompt", "eso") and must be resolved from
    active mission/session state before execution.
    """

    intent: str
    normalized_text: str
    confidence: float
    target_hint: str | None = None
    artifact_hint: str | None = None
    needs_context: bool = False
    requires_ui_read: bool = False
    requires_ui_write: bool = False
    requires_submit: bool = False
    requires_clipboard: bool = False
    matched_pattern: str = ""


# Patterns run against `_normalize_command_text(text)` — ASCII, lowercase,
# diacritics stripped. Use plain ASCII letters only. Word boundaries (\b)
# prevent false positives inside longer compound words.

# Execution delegation: "córrelo tú mismo", "hazlo tu", "ejecútalo tú", etc.
_OWNER_DELEGATION_EXEC_PATTERNS_ES: tuple[str, ...] = (
    r"\b(?:correlo|corrolo)s?\s+tu(?:\s+mismo)?\b",
    r"\bcorre\s+los?\s+tu(?:\s+mismo)?\b",
    r"\bpuedes\s+correr(?:los?)?\s+tu\b",
    r"\bhaz\s*los?\s+tu(?:\s+mismo)?\b",
    r"\bhazlo\s+tu(?:\s+mismo)?\b",
    r"\bejecuta(?:lo|los)?\s+tu(?:\s+mismo)?\b",
    r"\blo\s+haces\s+tu(?:\s+mismo)?\b",
    r"\blo\s+corres\s+tu(?:\s+mismo)?\b",
)
_OWNER_DELEGATION_EXEC_PATTERNS_EN: tuple[str, ...] = (
    r"\brun\s+(?:it|this|that|them|those)\s+yourself\b",
    r"\byou\s+run\s+(?:it|this|that|them|those)\b",
    r"\bdo\s+(?:it|this|that|them)\s+yourself\b",
    r"\bdo\s+(?:it|this|that|them)\s+for\s+me\b",
    r"\byou\s+handle\s+(?:it|this|that|them)\b",
)

# Decision delegation: pure "decide tú" / "you decide" variants.
_OWNER_DELEGATION_DECISION_PATTERNS_ES: tuple[str, ...] = (
    r"\bdecide\s+tu\b",
    r"\btu\s+decides\b",
    r"\bescoge\s+tu\b",
    r"\belige\s+tu\b",
    r"\btu\s+eliges\b",
)
_OWNER_DELEGATION_DECISION_PATTERNS_EN: tuple[str, ...] = (
    r"\byou\s+decide\b",
    r"\byou\s+choose\b",
    r"\byou\s+pick\b",
)

# No-manual-work delegation: user is refusing manual handoff entirely.
_OWNER_DELEGATION_NO_MANUAL_PATTERNS_ES: tuple[str, ...] = (
    r"\bte\s+toca\s+a\s+ti\b",
    r"\b(?:ya\s+)?no\s+tengo\s+que\s+teclear(?:\s+nada)?\b",
    r"\bno\s+me\s+pidas\s+(?:que\s+lo\s+haga|que\s+teclee|que\s+corra)\b",
    r"\bno\s+me\s+preguntes(?:\s+m[aá]s)?\b",
    r"\bno\s+me\s+devuelvas\s+el\s+trabajo\b",
    r"\bno\s+me\s+hagas\s+teclear\b",
    r"\bencargate\s+tu(?:\s+de\s+(?P<hint_es_encargate>.+))?\b",
    r"\bgestiona(?:lo|los)?\s+tu(?:\s+de\s+(?P<hint_es_gestiona>.+))?\b",
)
_OWNER_DELEGATION_NO_MANUAL_PATTERNS_EN: tuple[str, ...] = (
    r"\btake\s+ownership(?:\s+of\s+(?P<hint_en_ownership>.+))?\b",
    r"\bhandle\s+it\b",
    r"\bdon'?t\s+ask\s+me\s+(?:to\s+do\s+it|to\s+run|anymore)\b",
    r"\bstop\s+asking\s+me\s+to\s+(?:run|do|type)\b",
)


# Verbs/objects that almost always need context to resolve.
_PRONOUN_ONLY_TOKENS = (
    "lo", "los", "la", "las", "eso", "esos", "ello", "ellos", "it", "this",
    "that", "them", "those",
)

# Destructive / external / irreversible / credential markers.
_RISKY_OBJECTIVE_PATTERNS: tuple[str, ...] = (
    r"\b(?:deploy|despliega|desplegar|deployment|deploys?)\b",
    r"\b(?:merge|mergea|mergear)\b",
    r"\b(?:publish|publica|publicar|publishing|publi[ck]a)\b",
    r"\b(?:send|env[ií]a|enviar|mandar)\s+(?:el|un|una|the|a|an)?\s*(?:email|correo|mensaje|telegram|sms|notification|tweet|post)\b",
    r"\b(?:submit|env[ií]a)\s+(?:the|la|el|una)?\s*(?:application|aplicacion|solicitud|form|formulario)\b",
    r"\b(?:pay|paga|pagar|payment|charge|cobra|cobrar|spend|gasta|gastar)\b",
    r"\b(?:borra|borrar|delete|elimina|eliminar|drop|remove)\s+(?:la|el|los|las|all|the|table|database|data|files|user|account|customer|usuario|cuenta)\b",
    r"\brm\s+-rf\b",
    r"\b(?:secret|secrets|password|passwords|token|tokens|credentials|credenciales|api[_\s]?key|rotar|rotate)\b",
    r"\b(?:production|prod|en\s+produccion|on\s+prod)\b",
    r"\bsudo\b",
    r"\bdrop\s+table\b",
    r"\b(?:force\s*push|--force\b|git\s+push\s+--force)\b",
)


def is_destructive_or_external_objective(text: str) -> bool:
    """True iff the resolved objective looks destructive, external,
    irreversible, or credential-gated."""
    if not text:
        return False
    normalized = _normalize_command_text(text)
    return any(re.search(pattern, normalized) for pattern in _RISKY_OBJECTIVE_PATTERNS)


def _extract_inline_hint(match: re.Match[str]) -> str | None:
    """Pull a named capture group out of an owner-delegation match, if any.

    Patterns that opt into capturing inline objects use named groups whose
    names start with ``hint_``; this helper returns the first non-empty
    one. Returns ``None`` when the delegation phrase carried no inline
    object (the common case for "córrelo tú").
    """
    try:
        groups = match.groupdict()
    except IndexError:
        return None
    for name, value in groups.items():
        if not name.startswith("hint_"):
            continue
        if value:
            cleaned = value.strip().strip(".!?")
            if cleaned and cleaned not in _PRONOUN_ONLY_TOKENS:
                return cleaned[:200]
    return None


_OWNER_DELEGATION_MIN_SIGNAL = 0.70


def _owner_delegation_lexical_score(normalized: str, matched_text: str) -> float:
    matched_tokens = set(_normalize_command_text(matched_text).split())
    if not matched_tokens:
        return 0.0
    tokens = set(normalized.split())
    if not tokens:
        return 0.0
    return len(matched_tokens & tokens) / len(matched_tokens)


def _owner_delegation_direction_score(normalized: str, *, kind: str) -> float:
    """Return how strongly the utterance directs action at the agent.

    Regex match alone is not enough. Phrases like "what would happen if you
    decide?" contain the lexical trigger but are hypothetical, not a command.
    """
    text = f" {normalized} "
    hypothetical_markers = (
        " what would happen ",
        " que pasaria ",
        " que pasa si ",
        " if you decide ",
        " if you choose ",
        " si decide tu ",
        " si tu decides ",
        " dime si decide tu ",
        " antes de que ",
        " deberia hacerlo tu o yo ",
        " deberia hacerlo yo o tu ",
    )
    if any(marker in text for marker in hypothetical_markers):
        return 0.20
    if re.search(r"\b(?:deberia|should)\b.+\b(?:tu|you)\b.+\b(?:yo|me|i)\b", normalized):
        return 0.25
    if kind == "decision" and re.search(
        r"\b(?:decide tu|tu decides|tu eliges|escoge tu|elige tu|you decide|you choose|you pick)\b",
        normalized,
    ):
        return 0.95
    if kind == "execution" and re.search(r"\b(?:tu|you|yourself|for me)\b", normalized):
        return 0.95
    if kind == "no_manual_work" and re.search(
        r"\b(?:no me|no tengo|don't ask|don t ask|stop asking|take ownership|handle it|encargate|gestiona|gestionalo|te toca)\b",
        normalized,
    ):
        return 0.95
    return 0.65


def _build_owner_delegation_intent(
    *,
    kind: str,
    normalized: str,
    match: re.Match[str],
) -> OwnerDelegationIntent | None:
    hint = _extract_inline_hint(match)
    lexical_score = _owner_delegation_lexical_score(normalized, match.group(0))
    direction_score = _owner_delegation_direction_score(normalized, kind=kind)
    if lexical_score < _OWNER_DELEGATION_MIN_SIGNAL or direction_score < _OWNER_DELEGATION_MIN_SIGNAL:
        return None
    confidence = min(0.99, round((lexical_score + direction_score) / 2, 3))
    return OwnerDelegationIntent(
        kind=kind,
        confidence=confidence,
        normalized_text=normalized[:200],
        requires_resolution=hint is None,
        is_execution_delegation=(kind == "execution"),
        is_decision_delegation=(kind == "decision"),
        is_no_manual_work_delegation=(kind == "no_manual_work"),
        explicit_action_hint=hint,
        lexical_score=lexical_score,
        direction_score=direction_score,
    )


def detect_owner_delegation(text: str) -> OwnerDelegationIntent | None:
    """Classify owner-delegation phrases.

    Returns ``None`` when the input is empty or doesn't match any
    delegation pattern. Casual chat ("hola", "ok", "gracias", "perfecto")
    does not match because the patterns require specific verbs/pronouns.
    """
    if not text:
        return None
    normalized = _normalize_command_text(text)

    for pattern in _OWNER_DELEGATION_EXEC_PATTERNS_ES + _OWNER_DELEGATION_EXEC_PATTERNS_EN:
        match = re.search(pattern, normalized)
        if match:
            intent = _build_owner_delegation_intent(
                kind="execution", normalized=normalized, match=match
            )
            if intent is not None:
                return intent

    for pattern in _OWNER_DELEGATION_DECISION_PATTERNS_ES + _OWNER_DELEGATION_DECISION_PATTERNS_EN:
        match = re.search(pattern, normalized)
        if match:
            intent = _build_owner_delegation_intent(
                kind="decision", normalized=normalized, match=match
            )
            if intent is not None:
                return intent

    for pattern in _OWNER_DELEGATION_NO_MANUAL_PATTERNS_ES + _OWNER_DELEGATION_NO_MANUAL_PATTERNS_EN:
        match = re.search(pattern, normalized)
        if match:
            intent = _build_owner_delegation_intent(
                kind="no_manual_work", normalized=normalized, match=match
            )
            if intent is not None:
                return intent

    return None


_TELEGRAM_IMPERATIVE_RULES: tuple[dict[str, Any], ...] = (
    {
        "intent": "approvals.cleanup_stale_duplicates",
        "patterns": (
            r"^\s*(?:limpia|limpiar|depura|depurar|clean\s+up|cleanup)\s*$",
            r"\b(?:limpia|depura|clean\s+up)\s+(?:las\s+)?(?:aprobaciones|approvals)(?:\s+(?:duplicadas|stale|viejas|pendientes))?\b",
        ),
        "needs_context": True,
    },
    {
        "intent": "ui.submit_prompt",
        "patterns": (
            r"\b(?:mandalo|mandalo ya|envialo|dale enter|presiona enter|ejecutalo|correlo|run it|submit|send it)\b",
        ),
        "requires_ui_write": True,
        "requires_submit": True,
        "needs_context": True,
        "artifact_hint": "prompt",
    },
    {
        "intent": "ui.paste_text",
        "patterns": (
            r"\b(?:pegale|pega|pégale)\s+(?:el\s+)?prompt\b",
            r"\b(?:pegalo|pégalo|paste\s+it)\b",
            r"\bpaste\s+(?:the\s+)?prompt\b",
        ),
        "requires_ui_write": True,
        "requires_clipboard": True,
        "needs_context": True,
        "artifact_hint": "prompt",
    },
    {
        "intent": "ui.set_instructions",
        "patterns": (
            r"\bdale\s+las\s+(?:instructions|instrucciones)\b",
            r"\bgive\s+it\s+the\s+instructions\b",
        ),
        "requires_ui_write": True,
        "requires_clipboard": True,
        "needs_context": True,
        "artifact_hint": "instructions",
    },
    {
        "intent": "ui.open_app",
        "patterns": (
            r"\babre\s+(?:la\s+)?app(?:\s+de\s+(?P<target_es_app>[a-z0-9 ._-]+))?\b",
            r"\babre\s+(?P<target_es_bare>codex|chatgpt|chrome|claude)\b",
            r"\bopen\s+(?:the\s+)?app(?:\s+(?P<target_en_app>[a-z0-9 ._-]+))?\b",
            r"\bopen\s+(?P<target_en_bare>codex|chatgpt|chrome|claude)\b",
        ),
        "requires_ui_write": True,
        "requires_ui_read": False,
        "needs_context": False,
    },
    {
        "intent": "ui.inspect_app",
        "patterns": (
            r"\brevisa\s+(?:la\s+)?app(?:\s+(?:de\s+)?(?P<target_es_inspect>[a-z0-9 ._-]+))?\b",
            r"\brevisa\s+(?P<target_es_inspect_bare>codex|chatgpt|chrome|claude)\b",
            r"\brevisa\s+en\s+(?P<target_es_inspect_in>[a-z0-9 ._-]+)\b",
            r"\breview\s+(?:the\s+)?app(?:\s+(?P<target_en_inspect>[a-z0-9 ._-]+))?\b",
            r"\breview\s+(?P<target_en_inspect_bare>codex|chatgpt|chrome|claude)\b",
            r"\breview\s+in\s+(?P<target_en_inspect_in>[a-z0-9 ._-]+)\b",
        ),
        "requires_ui_read": True,
        "needs_context": True,
    },
    {
        "intent": "task.continue_active_mission",
        "patterns": (
            r"\b(?:continua|sigue|procede)\b",
            r"\b(?:continue|proceed)\b",
        ),
        "needs_context": True,
    },
)

_ACTIONABLE_TELEGRAM_MARKERS: tuple[str, ...] = (
    "abre",
    "open",
    "dale",
    "pega",
    "pegale",
    "paste",
    "revisa",
    "review",
    "corre",
    "correlo",
    "ejecuta",
    "ejecutalo",
    "encargate",
    "continua",
    "sigue",
    "procede",
    "orquesta",
    "orchestrate",
    "limpia",
    "limpiar",
    "depura",
    "depurar",
    "clean up",
    "cleanup",
    "run",
    "submit",
    "send it",
    "take ownership",
)


def _target_from_match(match: re.Match[str]) -> str | None:
    for name, value in match.groupdict().items():
        if not name.startswith("target_"):
            continue
        if value:
            target = value.strip(" .,:;!?")
            if target:
                return target[:80]
    return None


def _canonical_target(target: str | None) -> str | None:
    if not target:
        return None
    normalized = _normalize_command_text(target).strip()
    if "codex" in normalized:
        return "Codex app"
    if "chatgpt" in normalized or "chat gpt" in normalized:
        return "ChatGPT"
    if "chrome" in normalized:
        return "Chrome"
    if "claude" in normalized:
        return "Claude"
    if "google cloud" in normalized or "gcp" in normalized:
        return "Google Cloud"
    if normalized in {"app", "la app", "the app"}:
        return None
    return target.strip()[:80]


def detect_telegram_imperative(text: str) -> TelegramImperativeIntent | None:
    """Detect explicit Spanish/English Telegram operator commands.

    This is intentionally separate from the fuzzy task_intent classifier.
    The global task_intent kill switch may disable weak inference, but it
    must not disable explicit operator commands.
    """
    if not text:
        return None
    normalized = _normalize_command_text(text).strip()
    if not normalized or normalized.startswith("/"):
        return None
    for rule in _TELEGRAM_IMPERATIVE_RULES:
        for pattern in rule["patterns"]:
            match = re.search(pattern, normalized)
            if not match:
                continue
            intent_name = str(rule["intent"])
            target_hint = _canonical_target(_target_from_match(match))
            if intent_name in {"ui.open_app", "ui.inspect_app"} and _is_web_target_ambiguous(
                normalized, target_hint
            ):
                return None
            return TelegramImperativeIntent(
                intent=intent_name,
                normalized_text=normalized[:200],
                confidence=0.94,
                target_hint=target_hint,
                artifact_hint=rule.get("artifact_hint"),
                needs_context=bool(rule.get("needs_context", False)),
                requires_ui_read=bool(rule.get("requires_ui_read", False)),
                requires_ui_write=bool(rule.get("requires_ui_write", False)),
                requires_submit=bool(rule.get("requires_submit", False)),
                requires_clipboard=bool(rule.get("requires_clipboard", False)),
                matched_pattern=pattern,
            )
    return None


def _is_web_target_ambiguous(normalized: str, target_hint: str | None) -> bool:
    """Return True when the imperative names a web/URL context that cannot
    be served by a local `open -a <App>` and should fall through to brain.

    Examples that must NOT resolve to the desktop app:
    - "abre claude/design" → claude.ai/design in browser
    - "abre claude.ai/projects" → URL
    - "abre en chrome claude/design" → explicit browser path
    """
    if not target_hint:
        return False
    if target_hint not in {"Claude", "ChatGPT", "Codex app"}:
        return False
    if "claude.ai" in normalized or "chatgpt.com" in normalized or "openai.com" in normalized:
        return True
    if re.search(r"\b(?:claude|chatgpt|codex)\s*/\s*\w+", normalized):
        return True
    if "en chrome" in normalized or "in chrome" in normalized:
        return True
    return False


def looks_like_actionable_telegram_message(text: str) -> bool:
    """Broad safety net for imperative-ish Telegram messages.

    A True result is not enough to execute anything. It only means the
    message must not silently fall into generic brain fallback; callers should
    map it to a supported intent, block it, or ask one concise clarification.
    """
    if not text:
        return False
    normalized = _normalize_command_text(text).strip()
    if not normalized or normalized.startswith("/"):
        return False
    if detect_owner_delegation(text) is not None or detect_telegram_imperative(text) is not None:
        return True
    return any(re.search(rf"\b{re.escape(marker)}\b", normalized) for marker in _ACTIONABLE_TELEGRAM_MARKERS)


def detect_meta_introspection_request(text: str) -> MetaIntrospectionIntent | None:
    """Classify reflective/meta/audit/non-actionable prompts.

    Returns `None` when the message is either:
      - empty,
      - an explicit implementation request (those win over meta),
      - or simply not meta/audit/secret-shaped.

    Otherwise returns a `MetaIntrospectionIntent` whose `kind` is one of
    `meta`, `audit`, or `non_actionable_token`. The caller is responsible
    for routing — typically to chat/brain — and for not logging
    `normalized_text` if `kind == "non_actionable_token"` (the field is
    redacted to length-and-shape for that kind).
    """
    if not text:
        return None
    if has_explicit_implementation_request(text):
        return None
    normalized = _normalize_command_text(text)
    for pattern in _META_AUDIT_PATTERNS:
        match = re.search(pattern, normalized)
        if match:
            return MetaIntrospectionIntent(
                kind="audit",
                normalized_text=normalized[:200],
                reason=f"audit_pattern:{match.group(0)[:40]}",
            )
    for pattern in _META_INTROSPECTION_PATTERNS_ES + _META_INTROSPECTION_PATTERNS_EN:
        match = re.search(pattern, normalized)
        if match:
            return MetaIntrospectionIntent(
                kind="meta",
                normalized_text=normalized[:200],
                reason=f"meta_pattern:{match.group(0)[:40]}",
            )
    if _is_secret_shaped_token(text):
        token = text.strip()
        shape = (
            f"len={len(token)}"
            f" upper={sum(1 for c in token if c.isupper())}"
            f" lower={sum(1 for c in token if c.islower())}"
            f" digit={sum(1 for c in token if c.isdigit())}"
        )
        return MetaIntrospectionIntent(
            kind="non_actionable_token",
            normalized_text=f"<redacted:{shape}>",
            reason="single_token_high_entropy",
        )
    return None


def _stable_task_id(summary: str, *, mode: str, source: str) -> str:
    normalized = _normalize_command_text(summary)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:48] or "task"
    return f"{mode}:{source}:{slug}"


def _extract_option_reference(text: str) -> int | None:
    normalized = _normalize_command_text(text).strip()
    match = re.fullmatch(r"(?:opcion|option)\s+([a-e]|\d+)", normalized)
    if match:
        value = match.group(1)
        return int(value) if value.isdigit() else _OPTION_ORDINALS.get(value)
    match = re.fullmatch(r"(?:vamos|vete|ve|dale|procede|continua|sigue)\s+con\s+(?:la\s+)?(?:opcion\s+)?(\d+)", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(?:vamos|vete|ve|dale|procede|continua|sigue)\s+con\s+la\s+(\w+)", normalized)
    if match:
        return _OPTION_ORDINALS.get(match.group(1))
    match = re.fullmatch(r"(\d+)", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"la\s+(\w+)", normalized)
    if match:
        if match.group(1).isdigit():
            return int(match.group(1))
        return _OPTION_ORDINALS.get(match.group(1))
    return _OPTION_ORDINALS.get(normalized)


def _looks_like_proceed_request(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    if not normalized:
        return False
    # Strip surrounding punctuation/whitespace so "PROCEDE.", "Dale!", "ok..."
    # all collapse to their bare token form before lookup.
    stripped = normalized.strip(" \t\n\r.,;:!?\"'`")
    if stripped in _PROCEED_TOKENS:
        return True
    if normalized in _PROCEED_TOKENS:
        return True
    return any(
        " " in token and (
            normalized.startswith(f"{token} ") or stripped.startswith(f"{token} ")
        )
        for token in _PROCEED_TOKENS
    )


def _looks_like_ratio_reference_request(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if "ratio" not in normalized:
        return False
    if not any(token in normalized for token in ("dame", "manda", "mandame", "envia", "enviame", "pasame", "tira", "tirame")):
        return False
    return bool(
        re.search(r"\b(?:los\s+)?2\b", normalized)
        or "dos ratios" in normalized
        or "ambos ratios" in normalized
        or "los dos ratios" in normalized
    )


def _extract_ratio_context_from_text(text: str) -> list[str]:
    normalized = _normalize_command_text(text)
    ratios: list[str] = []
    for pattern, label in (
        (r"\b9\s*[:x]\s*16\b|\b9x16\b|\bvertical\b", "9:16 vertical"),
        (r"\b1\s*[:x]\s*1\b|\b1x1\b|\bcuadrad[oa]\b", "1:1 cuadrado"),
        (r"\b16\s*[:x]\s*9\b|\b16x9\b|\bhorizontal\b", "16:9 horizontal"),
    ):
        if re.search(pattern, normalized) and label not in ratios:
            ratios.append(label)
    return ratios


_INTERNAL_LEAK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmessage[_ -]?id\b", re.IGNORECASE),
    re.compile(r"\bchat[_ -]?id\b", re.IGNORECASE),
    re.compile(r"\btg-[A-Za-z0-9_-]+\b"),
    re.compile(r"\bnlm-[A-Za-z0-9_-]+\b"),
    re.compile(r"(?<!\w)to=(?:functions|multi_tool_use|web|image_gen|tool_search)\.", re.IGNORECASE),
    re.compile(r'"recipient_name"\s*:\s*"(?:functions|multi_tool_use|web|image_gen|tool_search)\.', re.IGNORECASE),
    re.compile(r'"tool_uses"\s*:\s*\[', re.IGNORECASE),
    re.compile(r"\blocalhost(?::\d+)?\b", re.IGNORECASE),
    re.compile(r"\b127\.0\.0\.1(?::\d+)?\b"),
    re.compile(r"\bterminal bridge\b", re.IGNORECASE),
    re.compile(r"\bblocked model response\b", re.IGNORECASE),
    re.compile(r"\brespuesta del modelo fue bloqueada\b", re.IGNORECASE),
    re.compile(r"\bsalida del modelo\b", re.IGNORECASE),
    re.compile(r"\btrazas internas\b", re.IGNORECASE),
    re.compile(r"\bherramientas internas\b", re.IGNORECASE),
    re.compile(r"\bla ocult[eé]\b", re.IGNORECASE),
    re.compile(r"\brespuesta bloqueada\b", re.IGNORECASE),
    re.compile(r"\bsanitizer\b", re.IGNORECASE),
    re.compile(r"\btool traces\b", re.IGNORECASE),
    re.compile(r"\brepite la instrucci[oó]n\b", re.IGNORECASE),
    re.compile(r"^\s*#\s*Telegram message\b", re.IGNORECASE),
    re.compile(r"\bReply ONLY to (?:that|the) latest message\b", re.IGNORECASE),
    re.compile(r"\bNow respond to the user[’']s most recent message\b", re.IGNORECASE),
    re.compile(r"\bTelegram-friendly Markdown\b", re.IGNORECASE),
    re.compile(r"\bDo not include internal trace\b", re.IGNORECASE),
    re.compile(r"\bNo user-visible text is valid outside <response> tags\b", re.IGNORECASE),
    re.compile(r"(?m)^\s*(?:user|assistant|system)\s*:\s*\S", re.IGNORECASE),
    re.compile(r"\bcontradice las capacidades\b", re.IGNORECASE),
    re.compile(r"\bcircuit breaker\b", re.IGNORECASE),
    re.compile(r"\bPID\s*[:#]?\s*\d+\b", re.IGNORECASE),
    re.compile(r"/Users/hector/"),
    re.compile(r"</?\s*system-reminder\s*>", re.IGNORECASE),
    re.compile(r"&lt;/?\s*system-reminder\s*&gt;", re.IGNORECASE),
    re.compile(r"\bReply\s+ONLY\b", re.IGNORECASE),
    re.compile(r"\bReply\s+ONLY\b[\s\S]{0,500}\bI\s+am\s+Dr\.?\s+Strange\b", re.IGNORECASE),
    re.compile(r"\bbrain-tooluse:[^\s`]+", re.IGNORECASE),
    re.compile(r"(?:\[id interno omitido\]|tg-[A-Za-z0-9_-]+):evidence-gate:\d+", re.IGNORECASE),
    re.compile(r"\b(?:needs_verification|running_needs_verification)\b", re.IGNORECASE),
    re.compile(r"\bcompleted_unverified\b", re.IGNORECASE),
    re.compile(r"\bbrain_tooluse_with_manifest_pending_verification\b", re.IGNORECASE),
    re.compile(r"\bruntime lost authoritative backing state\b", re.IGNORECASE),
    re.compile(r"\b(?:observe_stream|agent_tasks)\b", re.IGNORECASE),
    re.compile(r"\bbinary\s+'[^']+'\s+requires higher privilege level\b", re.IGNORECASE),
    re.compile(r"\b(?:allowed whitelist|sandbox\.excludedCommands|Seatbelt OS-level|runtime host|CLI host|Bash tool|tool Bash)\b", re.IGNORECASE),
    re.compile(r"\bsandbox\s+(?:embebido|del entorno local|que est[aá] bloqueando|carga policies)\b", re.IGNORECASE),
    re.compile(r"(?:~|/Users/hector)/\.claude/settings\.json(?:\.[A-Za-z0-9_.-]+)?", re.IGNORECASE),
    re.compile(r"\bclaw_v2/sandbox\.py\b", re.IGNORECASE),
)

# Indices in _INTERNAL_LEAK_PATTERNS that bump the entire reply to the error
# template. Only true internal scaffolding belongs here: tool-call blobs and
# verbatim prompt echoes. Soft phrases ("respuesta bloqueada", "trazas internas",
# "circuit breaker", etc.) are inline-redacted further below so legitimate
# technical references do not nuke an otherwise valid reply.
#
# Index 26 (the structural ^role: line) is intentionally NOT in this set: a single
# role-prefixed line also matches a legitimate Spanish reply that quotes/summarizes
# a chat transcript or a log. It is gated by _role_echo_dominates() instead, which
# only nukes when the role lines ARE the message (real prompt echo), not when they
# are quoted prose inside a real answer.
_NUKE_PATTERN_INDICES: tuple[int, ...] = (4, 5, 6, 20, 21, 22, 23, 24, 25, 33, 34)

# A block of >=2 consecutive role-prefixed lines is a real conversation-prompt echo.
_CONSECUTIVE_ROLE_ECHO = re.compile(
    r"(?m)^\s*(?:user|assistant|system)\s*:\s*\S.*(?:\n\s*(?:user|assistant|system)\s*:\s*\S.*)+",
    re.IGNORECASE,
)
# A role header that OPENS the whole message is a verbatim prompt echo (e.g.
# "system: Eres Dr. Strange.\n<internal instructions>"). A legitimate reply that
# cites a transcript line carries it inside prose, not as the very first line.
_LEADING_ROLE_ECHO = re.compile(
    r"(?s)\A\s*(?:user|assistant|system)\s*:\s*\S",
    re.IGNORECASE,
)
# Full role lines, used to measure how much of the reply is bare role echo.
_ROLE_LINE_FULL = re.compile(r"(?im)^\s*(?:user|assistant|system)\s*:.*$")


def _role_echo_dominates(text: str) -> bool:
    """True when ^role: lines are a verbatim prompt echo, not quoted transcript prose.

    Nukes only when (a) a role header opens the message (a single leaked
    "system:/user:" prompt header), (b) 2+ consecutive role lines appear (a real
    multi-turn echo), or (c) the role line(s) make up most of the message (a bare
    "user: Estatus" echo). A reply that merely cites one transcript line inside
    real prose is left intact.
    """
    # idx 26 — keep the trigger condition co-located with the pattern it gates.
    if not _INTERNAL_LEAK_PATTERNS[26].search(text):
        return False
    if _LEADING_ROLE_ECHO.search(text):
        return True
    if _CONSECUTIVE_ROLE_ECHO.search(text):
        return True
    role_chars = sum(len(m.group(0)) for m in _ROLE_LINE_FULL.finditer(text))
    total = len(text.strip())
    return total > 0 and role_chars / total >= 0.6


def _chat_response_has_internal_leak(text: str) -> bool:
    return any(pattern.search(text) for pattern in _INTERNAL_LEAK_PATTERNS)


def _sanitize_chat_response(text: str) -> str:
    if not text:
        return text
    from claw_v2.leak_scrub import redact_system_reminders

    precleaned = redact_system_reminders(text)
    if any(_INTERNAL_LEAK_PATTERNS[i].search(precleaned) for i in _NUKE_PATTERN_INDICES) or _role_echo_dominates(precleaned):
        return (
            "Tuve un error preparando la respuesta. "
            "Retomo la acción con el contexto disponible o te diré el bloqueo verificado."
        )

    sanitized = precleaned
    sanitized = re.sub(r"\[redacted:\s*system-reminder\]", "[redacted: internal marker]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"&lt;/?\s*system-reminder\s*&gt;", "[redacted: internal marker]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"</?\s*system-reminder\s*>", "[redacted: internal marker]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(
        r"\bbrain-tooluse:[^\s`]+",
        "tarea local",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"(?:\[id interno omitido\]|tg-[A-Za-z0-9_-]+):evidence-gate:\d+",
        "tarea local",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"\bMessage ID\s+\d+\b", "Mensaje enviado", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bmessage_id\s*[:#]?\s*\d+\b", "mensaje enviado", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bchat_id\s*[:#]?\s*-?\d+\b", "chat", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\btg-[A-Za-z0-9_-]+\b", "[id interno omitido]", sanitized)
    sanitized = re.sub(
        r"\[id interno omitido\]:evidence-gate:\d+",
        "tarea local",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"\bnlm-[A-Za-z0-9_-]+\b", "[id interno omitido]", sanitized)
    sanitized = re.sub(r"\bPID\s*[:#]?\s*\d+\b", "proceso local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\blocalhost(?::\d+)?\b", "[endpoint local interno]", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\b127\.0\.0\.1(?::\d+)?\b", "[endpoint local interno]", sanitized)
    sanitized = re.sub(
        r"\bbrain_tooluse_with_manifest_pending_verification\b",
        "pendiente de verificacion con evidencia local",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\brunning_needs_verification\b",
        "en verificacion",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\bneeds_verification\b",
        "pendiente de verificacion",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\bcompleted_unverified\b",
        "completada sin verificacion final",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\bobserve_stream\b",
        "telemetria local",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\bagent_tasks\b",
        "registro de tareas",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"^\s*Ledger\s*:",
        "Historial de tareas:",
        sanitized,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    sanitized = re.sub(
        r"\bruntime lost authoritative backing state\b",
        "se perdio el estado ejecutable de la tarea",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\bbinary\s+'([^']+)'\s+requires higher privilege level\s+\(not in the allowed whitelist\)",
        r"el comando '\1' está bloqueado por la política de ejecución local",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"\bsandbox\.excludedCommands\b",
        "configuración local de permisos",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        r"`?(?:~|/Users/hector)/\.claude/settings\.json(?:\.[A-Za-z0-9_.-]+)?`?",
        "[configuración local omitida]",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"\ballowed whitelist\b", "política de ejecución", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bel allowlist real\b", "la política efectiva", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\ballowlist real\b", "política efectiva", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bSeatbelt OS-level\b", "política del sistema", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bClaude Code\s+\(CLI host\)", "el entorno local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bCLI host\b", "entorno local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bruntime host\b", "entorno local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bBash tool\b|\btool Bash\b", "herramienta local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsub-runtime\b", "entorno local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsandbox que est[aá] bloqueando\b", "política de ejecución que está bloqueando", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsandbox embebido\b", "política de ejecución embebida", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsandbox del entorno local\b", "política de ejecución local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsandbox carga policies\b", "la política de ejecución carga reglas", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bruntime de la herramienta local\b", "entorno de ejecución local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bclaw_v2/sandbox\.py\b", "política local del agente", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bconfig de el entorno local\b", "config del entorno local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bel política\b", "la política", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bpuerto\s+\d{2,5}\b", "puerto interno", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\ben\s+:\d{2,5}\b", "en un puerto interno", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bterminal bridge\b", "herramienta local", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bblocked model response\b", "respuesta interna suprimida", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\brespuesta del modelo fue bloqueada\b", "respuesta interna suprimida", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\brespuesta bloqueada\b", "respuesta interna suprimida", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsalida del modelo\b", "salida interna", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\btrazas internas\b", "detalles internos", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bherramientas internas\b", "herramientas locales", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bla ocult[eé]\b", "se omitió", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\btool traces\b", "trazas locales", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bsanitizer\b", "filtro defensivo", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\brepite la instrucci[oó]n\b", "indícame el siguiente paso", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bcontradice las capacidades\b", "contradice el contexto operacional", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"\bCircuit breaker\b", "bloqueo operacional interno", sanitized, flags=re.IGNORECASE)
    sanitized = re.sub(r"`sendVideo`|`sendDocument`|sendVideo|sendDocument", "envío de Telegram", sanitized)

    def _redact_backticked_path(match: re.Match[str]) -> str:
        raw_path = match.group(1)
        basename = raw_path.rstrip("/").split("/")[-1] or "archivo"
        return f"`[ruta local omitida]/{basename}`"

    sanitized = re.sub(r"`(/Users/hector/[^`]+)`", _redact_backticked_path, sanitized)
    sanitized = re.sub(r"(?<!`)/Users/hector/[^\n`]+", "[ruta local omitida]", sanitized)
    return sanitized


def _looks_like_autonomy_grant(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if "autonom" not in normalized and "permiso" not in normalized and "autoriz" not in normalized:
        return False
    grant_markers = (
        "tienes toda la autonomia",
        "tienes autonomia",
        "autonomia completa",
        "modo autonomo",
        "full autonomy",
        "complete autonomy",
        "no tienes que pedirme autorizacion",
        "no me pidas autorizacion",
        "no me pidas permiso",
        "no tienes que pedirme permiso",
        "sin pedir autorizacion",
        "sin pedir permiso",
    )
    return any(marker in normalized for marker in grant_markers)


def _extract_numbered_options(text: str) -> list[str]:
    options: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^\s*(\d+)\.\s+(.+?)\s*$", line)
        if not match:
            continue
        options.append(match.group(2).strip())
    return options[:9]


def _compact_summary(text: str, *, limit: int = 240) -> str | None:
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _extract_pending_action_from_reply(text: str) -> str | None:
    for line in text.splitlines():
        normalized_line = re.sub(r"^\s*(?:[-*]\s*)+", "", line.strip())
        normalized_line = normalized_line.replace("**", "").replace("__", "")
        match = re.match(
            r"^\s*(?:siguiente paso|next step|pendiente|retomo la acci[oó]n)\s*:\s*(.+?)\s*$",
            normalized_line,
            re.IGNORECASE,
        )
        if match:
            pending = match.group(1).strip()
            pending = re.split(r"\s+[—-]\s+requiere\b", pending, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            return pending
    return None


def _filter_task_queue_by_mode(task_queue: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
    normalized_mode = _normalize_command_text(mode).strip()
    if not normalized_mode:
        return task_queue
    return [item for item in task_queue if _normalize_command_text(str(item.get("mode", ""))) == normalized_mode]


def _select_next_task_queue_item(task_queue: list[dict[str, Any]], *, preferred_mode: str) -> dict[str, Any] | None:
    normalized_mode = _normalize_command_text(preferred_mode).strip()
    preferred = [
        item for item in task_queue
        if item.get("status") == "pending"
        and item.get("summary")
        and _task_queue_item_ready(task_queue, item)
        and _normalize_command_text(str(item.get("mode", ""))) == normalized_mode
    ]
    if preferred:
        return preferred[0]
    for item in task_queue:
        if item.get("status") == "pending" and item.get("summary") and _task_queue_item_ready(task_queue, item):
            return item
    return None


def _task_queue_item_ready(task_queue: list[dict[str, Any]], item: dict[str, Any]) -> bool:
    dependency_ids = [str(dep) for dep in (item.get("depends_on") or []) if dep]
    if not dependency_ids:
        return True
    statuses = {
        str(entry.get("task_id")): str(entry.get("status"))
        for entry in task_queue
        if entry.get("task_id")
    }
    return all(statuses.get(dep_id) == "done" for dep_id in dependency_ids)


def _extract_verification_status(text: str) -> str | None:
    for line in text.splitlines():
        match = _VERIFICATION_STATUS_RE.search(line)
        if match:
            normalized = match.group(1).strip().lower()
            if normalized in {"ok", "passed"}:
                return "passed"
            if normalized == "failed":
                return "failed"
            if normalized == "pending":
                return "pending"
            return "unknown"
    return None


def verify_brain_tooluse(
    coordinator: Any,
    *,
    task_id: str,
    objective: str,
    files_written: list[str],
    commands_run: list[str],
    response_excerpt: str = "",
    lane_overrides: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Verify a substantive brain-tool-use turn from its real artifacts.

    Dispatches one verifier-lane task directly, without research/synthesis
    phases. The close path does not call this helper until the B1 wiring lands.
    """
    evidence_lines: list[str] = []
    if files_written:
        evidence_lines.append("Files written: " + ", ".join(files_written[:50]))
    if commands_run:
        evidence_lines.append("Commands run: " + " | ".join(commands_run[:20]))
    if response_excerpt:
        evidence_lines.append("Assistant claim: " + response_excerpt[:1000])
    evidence = "\n".join(evidence_lines) or "No artifacts captured."

    instruction = (
        "Verify whether this brain tool-use turn actually accomplished its "
        f"objective from the evidence below. Objective: {objective}\n\n"
        f"{evidence}\n\n"
        "Inspect the written files / commands as needed. Return a concise "
        "operational review and include a line "
        "`Verification Status: passed|pending|failed`. "
        "If more work is needed, include `Siguiente paso: ...`."
    )
    task = WorkerTask(
        name="verify_brain_action",
        instruction=instruction,
        lane="verifier",
    )
    try:
        trace = new_trace_context(job_id=task_id, artifact_id=task_id)
        results = coordinator._dispatch_parallel(
            [task],
            trace,
            lane_overrides=lane_overrides,
        )
    except Exception:
        return "pending"
    text = "\n".join(str(getattr(result, "content", "") or "") for result in results)
    return _extract_verification_status(text) or "pending"


def _build_checkpoint(text: str, *, pending_action: str | None, verification_status: str) -> dict[str, str]:
    checkpoint = {
        "summary": _compact_summary(text, limit=180) or "",
        "verification_status": verification_status,
    }
    if pending_action:
        checkpoint["pending_action"] = pending_action
    return checkpoint


def _build_coordinator_tasks(
    mode: str,
    objective: str,
) -> tuple[list[WorkerTask], list[WorkerTask] | None, list[WorkerTask] | None]:
    if mode == "coding":
        research = [
            WorkerTask(
                name="scope_and_risks",
                lane="research",
                instruction=(
                    "Inspect the request, identify the relevant files, likely risks, and the smallest viable implementation. "
                    f"Objective: {objective}"
                ),
            )
        ]
        implementation = [
            WorkerTask(
                name="implement_change",
                lane="worker",
                instruction=(
                    "Implement the requested change in the workspace. Keep edits minimal and explicit. "
                    "Work in three explicit phases and emit each phase as its own labeled section in the response:\n"
                    "1) `## Edits` — for every file touched, list `path: <one-line summary of change>`. "
                    "If you ran search/inspect tools, list them under `inspections:`.\n"
                    "2) `## Build/Verify` — for every command executed (lint, typecheck, build, export, tests), "
                    "list `cmd: <command>` and `result: ok|fail (<short reason>)`. If nothing was built/verified, "
                    "say `none` and explain why.\n"
                    "3) `## Evidence` — list any artifact paths the next phase can inspect (diff hunks, screenshots, "
                    "build logs, output dirs). If none, say `none`.\n"
                    "Do NOT skip any of the three sections. If a phase fails, still emit the section and state the failure.\n"
                    f"Objective: {objective}"
                ),
            )
        ]
        verification = [
            WorkerTask(
                name="verify_change",
                lane="verifier",
                instruction=(
                    "Verify the implementation from the available evidence. "
                    "Return a concise operational review and include a line `Verification Status: passed|pending|failed`. "
                    "If more work is needed, include `Siguiente paso: ...`. "
                    f"Objective: {objective}"
                ),
            )
        ]
        return research, implementation, verification

    research = [
        WorkerTask(
            name="gather_findings",
            lane="research",
            instruction=(
                "Gather the key facts, claims, and open questions for this request. "
                f"Objective: {objective}"
            ),
        ),
        WorkerTask(
            name="challenge_findings",
            lane="research",
            instruction=(
                "Challenge the assumptions, identify weak claims, and note what still needs verification. "
                f"Objective: {objective}"
            ),
        ),
    ]
    verification = [
        WorkerTask(
            name="verify_findings",
            lane="verifier",
            instruction=(
                "Review the synthesized findings and include `Verification Status: passed|pending|failed`. "
                "If follow-up work is needed, include `Siguiente paso: ...`."
            ),
        )
    ]
    return research, None, verification


def _coordinator_checkpoint(result: CoordinatorResult, *, objective: str) -> dict[str, str]:
    verification_results = result.phase_results.get("verification", [])
    implementation_results = result.phase_results.get("implementation", [])
    verification_text = "\n".join(item.content for item in verification_results if item.content)
    implementation_text = "\n".join(item.content for item in implementation_results if item.content)
    critical_worker_error = _is_critical_worker_error(result)
    pending_action = _extract_pending_action_from_reply(verification_text) or _extract_pending_action_from_reply(implementation_text)
    implementation_error = next(
        (item.error for item in implementation_results if item.error),
        "",
    )
    verification_status = (
        ("failed" if critical_worker_error else None)
        or ("failed" if implementation_error else None)
        or _extract_verification_status(verification_text)
        or ("failed" if result.error else None)
        or ("pending" if verification_results else "unknown")
    )
    summary_source = _CRITICAL_WORKER_VISIBLE_MESSAGE if critical_worker_error else (result.synthesis or objective)
    summary = _compact_summary(summary_source, limit=180) or summary_source
    checkpoint = {
        "summary": summary,
        "verification_status": verification_status,
    }
    if critical_worker_error:
        checkpoint["critical_worker_error"] = True
        checkpoint["coordinator_audit"] = dict(getattr(result, "audit", {}) or {})

    structured = _coordinator_result_to_structured(
        result,
        objective=objective,
        verification_status=verification_status,
        pending_action=pending_action,
        implementation_error=implementation_error,
    )
    semantic_errors = _validate_structured_semantics(structured)
    if semantic_errors:
        checkpoint["coordinator_semantic_errors"] = ";".join(semantic_errors)
        if verification_status == "passed":
            verification_status = "pending"
            checkpoint["verification_status"] = "pending"
    checkpoint["coordinator_result"] = structured

    if pending_action:
        checkpoint["pending_action"] = pending_action
    elif critical_worker_error:
        checkpoint["pending_action"] = "reparar error crítico del subagente antes de reintentar la misión principal"
    elif implementation_error:
        checkpoint["pending_action"] = f"reintentar implementación: {implementation_error}"
    if result.error:
        checkpoint["error"] = result.error
    elif implementation_error:
        checkpoint["error"] = implementation_error
    return checkpoint


def _coordinator_result_to_structured(
    result: CoordinatorResult,
    *,
    objective: str,
    verification_status: str,
    pending_action: str | None,
    implementation_error: str,
) -> dict[str, Any]:
    from claw_v2.coordinator_schema import coerce_unstructured_coordinator_output

    actions: list[dict[str, str]] = []
    for phase, items in result.phase_results.items():
        for item in items:
            if item.error:
                continue
            if not getattr(item, "content", None):
                continue
            actions.append({
                "agent": "coordinator",
                "action": str(getattr(item, "task_name", phase)),
                "tool": str(phase),
                "result": str(item.content)[:500],
            })

    evidence: list[dict[str, str]] = []
    if verification_status == "passed" and not implementation_error:
        # Only implementation phase counts as concrete evidence.
        # Verification phase output is a check, not the artifact itself —
        # treat it as a verification check, not evidence.
        for item in result.phase_results.get("implementation", []):
            if item.error or not getattr(item, "content", None):
                continue
            evidence.append({
                "type": "implementation",
                "name": str(getattr(item, "task_name", "implementation")),
                "value": str(item.content)[:500],
            })

    blockers: list[str] = []
    if result.error:
        blockers.append(f"coordinator_error: {result.error}")
    if implementation_error:
        blockers.append(f"implementation_error: {implementation_error}")
    if _is_critical_worker_error(result):
        blockers.append("critical_worker_error")

    if not actions and not evidence:
        coerced = coerce_unstructured_coordinator_output(result.synthesis or objective)
        coerced["task_kind"] = "coordinator_unstructured"
        coerced["blockers"] = blockers + coerced["blockers"]
        return coerced

    status_map = {
        "passed": "executed",
        "failed": "failed",
        "blocked": "blocked",
        "pending": "pending",
        "awaiting_approval": "blocked",
    }
    status = status_map.get(verification_status, "pending")
    if blockers and status == "executed":
        status = "blocked"

    return {
        "status": status,
        "task_kind": "coordinator",
        "actions_taken": actions,
        "evidence": evidence,
        "changed_files": [],
        "verification": {
            "status": verification_status if verification_status in {"passed", "pending", "failed", "blocked"} else "pending",
            "checks": [],
        },
        "blockers": blockers,
        "next_user_action": pending_action,
        "summary_for_user": (result.synthesis or objective)[:1000],
    }


def _validate_structured_semantics(structured: dict[str, Any]) -> list[str]:
    from claw_v2.coordinator_schema import validate_coordinator_semantics

    return validate_coordinator_semantics(structured)


def _format_worker_results(results: list[Any]) -> str:
    lines: list[str] = []
    for item in results:
        if item.error:
            lines.append(f"- {item.task_name}: ERROR {item.error}")
        else:
            lines.append(f"- {item.task_name}: {_compact_summary(item.content, limit=240) or '(no content)'}")
    return "\n".join(lines) if lines else "- none"


def _format_coordinator_response(result: CoordinatorResult, *, checkpoint: dict[str, str], forced: bool) -> str:
    if _is_critical_worker_error(result):
        lines = [_CRITICAL_WORKER_VISIBLE_MESSAGE]
        if checkpoint.get("pending_action"):
            lines.append(f"Siguiente paso: {checkpoint['pending_action']}")
        return "\n".join(lines)
    status = checkpoint.get("verification_status", "unknown")
    opening = {
        "passed": f"Listo. Cerré la tarea `{result.task_id}`.",
        "failed": f"No pude cerrar bien la tarea `{result.task_id}`.",
        "pending": f"La tarea `{result.task_id}` todavía necesita un paso más.",
        "blocked": f"La tarea `{result.task_id}` quedó bloqueada.",
        "awaiting_approval": f"La tarea `{result.task_id}` necesita aprobación antes de seguir.",
    }.get(status, f"La tarea `{result.task_id}` quedó en estado {status}.")
    lines = [opening]
    summary = checkpoint.get("summary") or result.synthesis
    if summary:
        lines.append(_compact_summary(summary, limit=240) or summary[:240])
    error = result.error or checkpoint.get("error")
    if error:
        lines.append(f"Error: {error}")
    if checkpoint.get("pending_action"):
        lines.append(f"Siguiente paso: {checkpoint['pending_action']}")
    return "\n".join(lines)


def _is_critical_worker_error(result: CoordinatorResult) -> bool:
    return str(getattr(result, "error", "") or "").startswith(_CRITICAL_WORKER_ERROR_PREFIX)


def _autonomy_policy_payload(state: dict[str, Any]) -> dict[str, Any]:
    autonomy_mode = state.get("autonomy_mode", "assisted")
    policy = _policy_for_mode(autonomy_mode)
    return {
        "autonomy_mode": autonomy_mode,
        "automatic_coordinator_modes": list(policy["automatic_coordinator_modes"]),
        "allowed_task_actions": list(policy["allowed_task_actions"]),
        "approval_required_actions": list(policy["approval_required_actions"]),
        "step_budget": state.get("step_budget"),
        "verification_status": state.get("verification_status"),
        "blocked_actions": list(policy["blocked_actions"]),
        "action_patterns": {name: list(patterns) for name, patterns in _AUTONOMY_ACTION_PATTERNS.items()},
        "task_action_patterns": {name: list(patterns) for name, patterns in _AUTONOMY_TASK_ACTION_PATTERNS.items()},
        "notes": list(policy["notes"]),
    }


def _evaluate_autonomy_policy(
    text: str,
    *,
    mode: str,
    forced: bool,
    autonomy_mode: str | None = None,
    approved_actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    normalized = _normalize_command_text(text)
    effective_autonomy_mode = autonomy_mode or ("autonomous" if not forced else "assisted")
    policy = _policy_for_mode(effective_autonomy_mode)
    matched_actions = _matched_policy_actions(normalized, blocked_actions=policy["blocked_actions"])
    if matched_actions:
        labels = ", ".join(matched_actions)
        return {
            "allowed": False,
            "reason": "sensitive_action",
            "summary": f"La tarea incluye acciones sensibles bloqueadas por política: {labels}.",
        }
    approval_actions = _matched_named_actions(
        normalized,
        names=policy["approval_required_actions"],
        pattern_map=_AUTONOMY_TASK_ACTION_PATTERNS,
    )
    approval_actions = [action for action in approval_actions if action not in approved_actions]
    if approval_actions:
        labels = ", ".join(approval_actions)
        return {
            "allowed": False,
            "reason": "approval_required_action",
            "summary": f"La tarea requiere aprobación para estas acciones: {labels}.",
            "matched_approval_actions": approval_actions,
        }
    if mode not in {"coding", "research"}:
        return {
            "allowed": False,
            "reason": "unsupported_mode",
            "summary": f"Modo {mode} fuera del coordinador autónomo.",
        }
    if not forced and mode not in policy["automatic_coordinator_modes"]:
        return {
            "allowed": False,
            "reason": "mode_not_auto_enabled",
            "summary": f"El modo {mode} no está habilitado para ejecución automática en {effective_autonomy_mode}.",
        }
    requested_actions = _classify_task_actions(normalized, mode=mode)
    effective_allowed_actions = set(policy["allowed_task_actions"]) | set(approved_actions)
    disallowed_actions = sorted(set(requested_actions) - effective_allowed_actions)
    if disallowed_actions:
        labels = ", ".join(disallowed_actions)
        return {
            "allowed": False,
            "reason": "action_not_allowed",
            "summary": f"La tarea pide acciones fuera del scope permitido para {effective_autonomy_mode}: {labels}.",
        }
    return {
        "allowed": True,
        "reason": "allowed",
        "summary": "La tarea entra dentro de la política de autonomía.",
    }


def _format_autonomy_policy_block(policy: dict[str, str | bool]) -> str:
    summary = str(policy.get("summary", "autonomy policy blocked this task"))
    reason = str(policy.get("reason", "blocked"))
    return (
        "autonomy policy blocked coordinated execution.\n"
        f"Reason: {reason}\n"
        f"Summary: {summary}\n"
        "Allowed automatic scopes: coding, research.\n"
        "Blocked automatic scopes: publish, deploy, destructive actions."
    )


def _format_task_approval_response(policy: dict[str, Any], pending: Any) -> str:
    summary = str(policy.get("summary", "task approval required"))
    reason = str(policy.get("reason", "approval_required_action"))
    return (
        "autonomy policy requires approval before coordinated execution.\n"
        f"Reason: {reason}\n"
        f"Summary: {summary}\n"
        f"Approve via: `/task_approve {pending.approval_id} {pending.token}`\n"
        f"Abort via: `/task_abort {pending.approval_id}`"
    )


def _task_approval_summary(objective: str, *, approval_actions: tuple[str, ...]) -> str:
    action_text = ", ".join(approval_actions) if approval_actions else "manual approval"
    compact = objective.strip()
    if len(compact) > 120:
        compact = compact[:117] + "..."
    return f"Approve coordinated task ({action_text}): {compact}"


def _policy_for_mode(autonomy_mode: str) -> dict[str, Any]:
    return _AUTONOMY_POLICY_MATRIX.get(autonomy_mode, _AUTONOMY_POLICY_MATRIX["assisted"])


def _matched_policy_actions(normalized_text: str, *, blocked_actions: tuple[str, ...] | list[str]) -> list[str]:
    return _matched_named_actions(normalized_text, names=blocked_actions, pattern_map=_AUTONOMY_ACTION_PATTERNS)


def _matched_named_actions(
    normalized_text: str,
    *,
    names: tuple[str, ...] | list[str],
    pattern_map: dict[str, tuple[str, ...]],
) -> list[str]:
    matches: list[str] = []
    for name in names:
        patterns = pattern_map.get(name, ())
        if any(re.search(pattern, normalized_text, re.IGNORECASE) for pattern in patterns):
            matches.append(name)
    return matches


def _classify_task_actions(normalized_text: str, *, mode: str) -> list[str]:
    matched = _matched_named_actions(
        normalized_text,
        names=tuple(_AUTONOMY_TASK_ACTION_PATTERNS.keys()),
        pattern_map=_AUTONOMY_TASK_ACTION_PATTERNS,
    )
    if matched:
        return matched
    if mode == "coding":
        return ["inspect", "edit", "test"]
    if mode == "research":
        return ["research", "summarize"]
    return []


def _infer_session_mode(user_text: str, reply_text: str | None = None) -> str:
    normalized = _normalize_command_text(f"{user_text}\n{reply_text or ''}")
    if any(token in normalized for token in ("browse", "http://", "https://", "www.")):
        return "browse"
    if any(token in normalized for token in ("terminal", "chrome", "screen", "computer", "click", "scroll", "sesion")):
        return "ops"
    if any(token in normalized for token in (
        "commit",
        "push",
        "git push",
        "test",
        "pytest",
        "fix",
        "corrige",
        "arregla",
        "bug",
        "repo",
        "codigo",
        "code",
        "patch",
        "completa",
        "termina",
        "implementa",
        "coloca",
        "colócala",
        "colocalo",
        "colócalo",
        "sube",
        "ponlo vivo",
        "ponla vivo",
        "produccion",
        "producción",
        "branding",
        "pagina",
        "página",
        "ga4",
    )):
        return "coding"
    if any(token in normalized for token in ("tweet", "post", "publica", "publish", "x.com", "social")):
        return "publish"
    if any(token in normalized for token in ("investiga", "research", "analiza", "notebook", "cuaderno")):
        return "research"
    return "chat"


def _looks_like_api_key(value: str) -> bool:
    """Reject values that look like env dumps or contain newlines."""
    if "\n" in value or "\r" in value:
        return False
    if "=" in value and not value.startswith("sk-"):
        return False
    if len(value) > 300:
        return False
    return True


def _read_env_var_from_shell_files(name: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}=(?P<value>.+?)\s*$")
    for path in (
        Path.home() / ".zshrc",
        Path.home() / ".zprofile",
        Path.home() / ".zshenv",
        Path.home() / ".profile",
    ):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except FileNotFoundError:
            continue
        for line in reversed(lines):
            match = pattern.match(line)
            if match is None:
                continue
            value = match.group("value").strip().strip("\"'")
            if value:
                return value
    return None


def _computer_instruction_requires_actions(text: str) -> bool:
    # Long texts are pasted content, not computer commands.
    if len(text) > 300:
        return False
    normalized = _normalize_command_text(text)
    return any(token in normalized for token in _COMPUTER_ACTION_TOKENS)


def _looks_like_computer_read_request(normalized: str) -> bool:
    if len(normalized) > 300:
        return False
    return any(token in normalized for token in _COMPUTER_READ_TOKENS)


def _extract_url_candidate(text: str) -> str | None:
    for pattern in (_SCHEME_URL_RE, _HOST_URL_RE):
        match = pattern.search(text)
        if match is None:
            continue
        candidate = _strip_url_punctuation(match.group("url"))
        if candidate:
            return candidate
    return None


def _extract_url_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for pattern in (_SCHEME_URL_RE, _HOST_URL_RE):
        for match in pattern.finditer(text):
            candidate = _strip_url_punctuation(match.group("url"))
            if not candidate:
                continue
            try:
                normalized = _normalize_url(candidate)
            except ValueError:
                continue
            key = _url_identity(normalized)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(normalized)
    return candidates


def _strip_url_punctuation(value: str) -> str:
    candidate = value.strip().strip("<>[]{}\"'")
    while candidate and candidate[-1] in ".,;:!?":
        candidate = candidate[:-1]
    return candidate


def _looks_like_standalone_url(text: str, url: str) -> bool:
    remainder = text.replace(url, " ", 1)
    remainder = re.sub(r"[\s`\"'“”‘’<>()\[\]{}.,;:!?-]+", "", remainder)
    return remainder == ""


def _url_identity(url: str) -> str:
    try:
        parsed = urlsplit(_normalize_url(url))
    except Exception:
        return _strip_url_punctuation(url).lower()
    host = (parsed.hostname or parsed.netloc).lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return f"{host}{path}".lower()


def _nested_url_candidates(text: str, *, skip_urls: list[str] | tuple[str, ...] = (), max_urls: int = 3) -> list[str]:
    skip = {_url_identity(url) for url in skip_urls}
    candidates: list[str] = []
    for url in _extract_url_candidates(text):
        key = _url_identity(url)
        if key in skip:
            continue
        skip.add(key)
        candidates.append(url)
        if len(candidates) >= max_urls:
            break
    return candidates


def _textual_nested_url_review_blocks(
    text: str,
    *,
    skip_urls: list[str] | tuple[str, ...] = (),
    max_urls: int = 3,
) -> list[str]:
    blocks: list[str] = []
    for url in _nested_url_candidates(text, skip_urls=skip_urls, max_urls=max_urls):
        content = _tweet_fxtwitter_read(url) if _is_tweet_url(url) else _jina_read(url)
        if content:
            blocks.append(f"[URL anidada analizada]: {url}\n{content[:3000]}")
        else:
            blocks.append(f"[URL anidada intentada]: {url}\nNo se pudo extraer contenido útil con lectura textual.")
    return blocks


def _looks_like_url_permission_deferral(text: str, *, context: str = "") -> bool:
    combined = f"{text}\n{context}" if context else text
    normalized = _normalize_command_text(combined)
    if not (
        _extract_url_candidate(combined)
        or any(token in normalized for token in ("url", "enlace", "tweet", "tuit", "flow.google"))
    ):
        return False
    has_defer_marker = any(
        marker in normalized
        for marker in (
            "si quieres",
            "cuando me digas",
            "cuando decidas",
            "hasta que decidas",
            "si me das la luz",
            "tu pick",
        )
    )
    if not has_defer_marker:
        return False
    return any(
        action in normalized
        for action in (
            " abro",
            " abra",
            " abrir",
            " abre ",
            " reviso",
            " revisar",
            " verifico",
            " verificar",
            " navego",
            " navegar",
            " traigo",
            " traer",
            " compruebo",
            " confirmar acceso",
        )
    )


def _strip_url_permission_deferrals(text: str, *, context: str = "") -> str:
    if not text:
        return text
    kept: list[str] = []
    changed = False
    for line in text.splitlines():
        if _looks_like_url_permission_deferral(line, context=context):
            changed = True
            continue
        kept.append(line)
    if not changed:
        return text
    stripped = "\n".join(kept)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped).strip()
    return stripped or "Detecté una URL Tier 1 que no debe convertirse en pregunta; la revisión queda como acción autónoma."


# Domains that require real browser cookies (auth) or heavy JS rendering.
# Headless browsers always hit login walls on these.
_AUTH_DOMAINS = frozenset({
    "x.com", "twitter.com",
    "instagram.com",
    "facebook.com", "fb.com",
    "linkedin.com",
    "reddit.com",
    "tiktok.com",
    "threads.net",
    "mail.google.com",
    "web.whatsapp.com",
    "flow.google",
})

_JS_RENDERED_DOMAINS = frozenset({
    "notion.so",
    "airtable.com",
    "figma.com",
    "linear.app",
    "github.com",
    "vercel.app",
})


def _needs_real_browser(url: str) -> bool:
    """Check if URL needs Chrome CDP (auth cookies + JS) instead of headless."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    return any(host == d or host.endswith(f".{d}") for d in _AUTH_DOMAINS)


def _select_navigation_strategy(url: str) -> str:
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return "static"
    if any(host == d or host.endswith(f".{d}") for d in _AUTH_DOMAINS):
        return "authenticated"
    if any(host == d or host.endswith(f".{d}") for d in _JS_RENDERED_DOMAINS):
        return "js_rendered"
    return "static"


_LOGIN_WALL_SIGNALS = [
    "log in\nsign up",
    "sign in\nsign up",
    "create an account",
    "don't miss what's happening",
    "join today",
    "this page is not available",
]


def _is_login_wall(content: str) -> bool:
    """Detect pages that returned a login/signup wall instead of real content."""
    if not content or not content.strip():
        return True
    lower = content.lower()
    return any(signal in lower for signal in _LOGIN_WALL_SIGNALS)


_RAW_MARKUP_SIGNALS = [
    "<meta",
    "<link rel",
    "dns-prefetch",
    "preconnect",
    "viewport-fit=cover",
    "user-scalable=0",
]


def _enrich_tweet_urls(text: str) -> str:
    """If text contains tweet URLs, pre-fetch content and append to message."""
    tweet_urls = [u for u in _extract_url_candidates(text) if _is_tweet_url(u)]
    if not tweet_urls:
        return text
    enriched = text
    seen_urls: list[str] = []
    for url in tweet_urls:
        seen_urls.append(url)
        content = _tweet_fxtwitter_read(url)
        if content:
            enriched += f"\n\n---\n[Contenido del tweet pre-cargado]:\n{content}"
            nested_blocks = _textual_nested_url_review_blocks(content, skip_urls=seen_urls)
            if nested_blocks:
                enriched += "\n\n---\n[URLs anidadas revisadas autónomamente]\n" + "\n\n".join(nested_blocks)
    return enriched


def _format_tweet_analysis_prompt(original_text: str, enriched_text: str) -> str:
    return (
        f"{original_text}\n\n"
        "[Instrucción de formato]\n"
        "Si respondes sobre este tweet o sobre enlaces relacionados que el tweet incluye, separa SIEMPRE la respuesta en dos secciones exactas:\n"
        "## Fuente\n"
        "- Resume únicamente lo que está en el tweet y, si aplica, en el contenido enlazado que sí fue leído.\n"
        "- Si hay bloques [URL anidada analizada] o [URL anidada intentada], incorpóralos como revisión ya ejecutada; no preguntes si debe abrirse una URL.\n"
        "- Distingue con claridad qué viene del tweet y qué viene del enlace.\n"
        "- No presentes inferencias o recomendaciones como si fueran parte de la fuente.\n\n"
        "## Aplicación sugerida\n"
        "- Incluye solo inferencias, recomendaciones o ideas prácticas tuyas.\n"
        "- Si detectas otra URL Tier 1 no revisada, no la conviertas en opción para Hector; reporta la limitación como intento pendiente o bloqueador real.\n"
        "- Si no hay una recomendación útil, escribe: Ninguna por ahora.\n\n"
        f"{enriched_text}"
    )


def _format_link_analysis_prompt(original_text: str, url: str, fetched_content: str) -> str:
    return (
        f"{original_text}\n\n"
        "[Instrucción de formato]\n"
        "Si respondes sobre este enlace, separa SIEMPRE la respuesta en dos secciones exactas:\n"
        "## Fuente\n"
        "- Resume únicamente lo que sí fue leído del enlace.\n"
        "- Si hay bloques [URL anidada analizada] o [URL anidada intentada], trátalos como revisión autónoma ya ejecutada; no preguntes si debe abrirse una URL.\n"
        "- Si el contenido vino incompleto, estaba detrás de login o hubo limitaciones de lectura, dilo explícitamente.\n"
        "- No mezcles inferencias, recomendaciones o juicio propio con la fuente.\n\n"
        "## Aplicación sugerida\n"
        "- Incluye solo inferencias, recomendaciones, riesgos o siguientes pasos tuyos.\n"
        "- No termines con 'si quieres lo abro/reviso/verifico' para URLs; esas revisiones son Tier 1 salvo acción Tier 3.\n"
        "- Si no hay una sugerencia útil, escribe: Ninguna por ahora.\n\n"
        f"[URL analizada]: {url}\n\n"
        f"[Contenido del enlace pre-cargado]:\n{fetched_content}"
    )


def _extract_link_analysis_context(text: str) -> dict[str, str] | None:
    url_match = re.search(r"(?m)^\[URL analizada\]:\s*(\S+)\s*$", text)
    content_match = re.search(r"(?ms)^\[Contenido del enlace pre-cargado\]:\n(.*)$", text)
    if url_match is None or content_match is None:
        return None
    return {
        "url": url_match.group(1).strip(),
        "fetched_content": content_match.group(1).strip(),
    }


def _looks_like_runtime_capability_question(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not any(token in normalized for token in ("bot", "claw", "sistema", "runtime")):
        return False
    return any(
        token in normalized
        for token in (
            "que aporta",
            "que aportara",
            "que tiene",
            "que le falta",
            "que puede",
            "que ya tiene",
            "que ganaria",
            "como esta",
            "como funciona hoy",
        )
    )


def _format_runtime_capability_prompt(text: str) -> str:
    return (
        "[Instrucción de rigor sobre el sistema]\n"
        "Si hablas del estado actual del bot/sistema, no sobreafirmes.\n"
        "Estructura SIEMPRE la respuesta con estas tres secciones exactas y en este orden:\n"
        "## Implementado hoy\n"
        "## Parcial\n"
        "## Sugerencia\n"
        "En 'Implementado hoy' incluye solo capacidades verificables hoy.\n"
        "En 'Parcial' incluye capacidades incompletas, limitadas o no plenamente confiables.\n"
        "En 'Sugerencia' incluye inferencias, recomendaciones o próximos pasos.\n"
        "Usa lenguaje conservador cuando no tengas evidencia directa del código o del comportamiento observado.\n"
        "No atribuyas capacidades internas no verificadas.\n\n"
        f"{text}"
    )


def _has_runtime_capability_sections(text: str) -> bool:
    return all(re.search(rf"(?m)^##\s+{re.escape(title)}\s*$", text) for title in _RUNTIME_CAPABILITY_SECTION_TITLES)


def _has_link_analysis_sections(text: str) -> bool:
    return all(re.search(rf"(?m)^##\s+{re.escape(title)}\s*$", text) for title in _LINK_ANALYSIS_SECTION_TITLES)


def _bulletize_runtime_capability_body(text: str) -> str:
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = re.sub(r"^#+\s*", "", line).strip()
            if not line:
                continue
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        bullets.append(line)
    if not bullets:
        bullets.append("- La respuesta original no separó hechos verificados de inferencias.")
    return "\n".join(bullets[:8])


def _bulletize_link_analysis_body(text: str) -> str:
    bullets: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = re.sub(r"^#+\s*", "", line).strip()
            if not line:
                continue
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        bullets.append(line)
    return "\n".join(bullets[:6]) if bullets else ""


def _summarize_prefetched_link_content(fetched_content: str) -> str:
    bullets: list[str] = []
    for raw_line in fetched_content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            continue
        if line.startswith("**") and line.endswith("**"):
            line = line.strip("*").strip()
        line = re.sub(r"^#+\s*", "", line).strip()
        if not line:
            continue
        if not line.startswith(("-", "*")):
            line = f"- {line}"
        bullets.append(line)
        if len(bullets) >= 4:
            break
    if not bullets:
        bullets.append("- El enlace fue leído, pero no se pudo extraer un resumen claro del contenido pre-cargado.")
    return "\n".join(bullets)


def _enforce_runtime_capability_sections(text: str) -> str:
    if _has_runtime_capability_sections(text):
        return text
    partial_body = _bulletize_runtime_capability_body(text)
    return (
        "## Implementado hoy\n"
        "- La respuesta original no identificó con suficiente rigor qué capacidades están verificadas hoy.\n\n"
        "## Parcial\n"
        f"{partial_body}\n\n"
        "## Sugerencia\n"
        "- Reescribe la respuesta separando hechos verificados, límites actuales e inferencias.\n"
        "- Antes de afirmar capacidades internas, apóyate en código, tests o telemetría reciente."
    )


def _is_url_echo(text: str, url: str) -> bool:
    normalized = text.strip().strip("<>[]{}\"'")
    return normalized == url or normalized == f"- {url}"


def _enforce_link_analysis_sections(text: str, *, url: str, fetched_content: str) -> str:
    if _has_link_analysis_sections(text):
        return text
    source_body = _bulletize_link_analysis_body(text)
    if not source_body or _is_url_echo(text, url):
        source_body = _summarize_prefetched_link_content(fetched_content)
    return (
        "## Fuente\n"
        f"{source_body}\n\n"
        "## Aplicación sugerida\n"
        "- Propón una acción, riesgo o siguiente paso usando lo anterior.\n"
        "- Si no aplica nada más, Ninguna por ahora."
    )


def _is_tweet_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except Exception:
        return False
    host = parsed.netloc.lower()
    if not any(host == domain or host.endswith(f".{domain}") for domain in ("x.com", "twitter.com")):
        return False
    return "/status/" in parsed.path


def _looks_like_tweet_followup_request(normalized_text: str) -> bool:
    if not any(token in normalized_text for token in _BROWSE_SHORTCUT_TOKENS):
        return False
    if not any(token in normalized_text for token in ("tweet", "tweets", "tuit", "tuits", "post", "hilo", "hilos", "thread")):
        return False
    # Only reuse the prior tweet when the user clearly points back to it.
    return any(
        token in normalized_text
        for token in (
            "ultimo tweet",
            "ultimo tuit",
            "ultimos tweets",
            "ultimos tuits",
            "tweet anterior",
            "tuit anterior",
            "ese tweet",
            "ese tuit",
            "este tweet",
            "este tuit",
            "tweet de arriba",
            "tuit de arriba",
            "tweet pasado",
            "tuit pasado",
            "tweet que te acabo",
            "tuit que te acabo",
            "post que te acabo",
            "hilo que te acabo",
            "thread que te acabo",
            "tweet que te di",
            "tuit que te di",
            "post que te di",
            "hilo que te di",
            "tweet que te mande",
            "tuit que te mande",
            "post que te mande",
            "hilo que te mande",
            "tweet que te envie",
            "tuit que te envie",
            "post que te envie",
            "hilo que te envie",
            "tweet que te pase",
            "tuit que te pase",
            "post que te pase",
            "hilo que te pase",
            "que te acabo de dar",
            "que acabo de darte",
            "que te acabo de pasar",
            "que acabo de pasarte",
        )
    )


def _looks_like_raw_markup(content: str) -> bool:
    lower = content.lower()
    return any(signal in lower for signal in _RAW_MARKUP_SIGNALS)


def _is_usable_browse_content(url: str, content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if _is_login_wall(stripped):
        return False
    if _is_tweet_url(url) and _looks_like_raw_markup(stripped):
        return False
    return True


def _normalize_url(value: str) -> str:
    candidate = _strip_url_punctuation(value)
    if not candidate:
        raise ValueError("usage: /browse <url>")
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid url")
    return candidate


def _has_url_query(url: str) -> bool:
    try:
        return bool(urlsplit(url).query)
    except Exception:
        return False


def _is_local_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except Exception:
        return False
    return host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".local")


def _extract_title_from_url(url: str) -> str:
    """Derive a short title from a URL for wiki ingest."""
    parsed = urlsplit(url)
    path = parsed.path.strip("/").split("/")
    if _is_tweet_url(url):
        return f"Tweet {path[-1][:12]}" if path else "Tweet"
    slug = path[-1] if path and path[-1] else parsed.netloc
    return slug.replace("-", " ").replace("_", " ")[:60] or parsed.netloc


def _format_computer_pending_summary(task: str, pending_action: dict[str, Any]) -> str:
    action = pending_action.get("action") or pending_action.get("type") or "unknown"
    if "coordinate" in pending_action:
        return f"{task} — {action} at {pending_action['coordinate']}"
    if "text" in pending_action:
        return f"{task} — {action} {pending_action['text']!r}"
    return f"{task} — {action}"


def _jina_read(url: str, *, timeout: float = 10) -> str:
    """Fetch URL content as markdown via Jina Reader."""
    import httpx
    from urllib.parse import quote
    try:
        response = httpx.get(
            f"https://r.jina.ai/{quote(url, safe=':/')}",
            headers={"Accept": "text/markdown"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        content = response.text.strip()
        if len(content) < 20:
            return ""
        if _is_login_wall(content):
            return ""
        return content
    except Exception:
        return ""


def _tweet_fxtwitter_read(url: str, *, timeout: float = 15) -> str:
    """Fetch full tweet text via fxtwitter public API."""
    if not _is_tweet_url(url):
        return ""
    import httpx

    match = re.search(r"/status/(\d+)", url)
    if not match:
        return ""
    # Extract username from URL path
    parsed = urlsplit(url)
    path_parts = [p for p in parsed.path.split("/") if p]
    if len(path_parts) < 2:
        return ""
    user, tweet_id = path_parts[0], match.group(1)

    try:
        response = httpx.get(
            f"https://api.fxtwitter.com/{user}/status/{tweet_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return _tweet_oembed_fallback(url, timeout=timeout)

    if data.get("code") != 200 or "tweet" not in data:
        return _tweet_oembed_fallback(url, timeout=timeout)

    tweet = data["tweet"]
    text = tweet.get("text", "").strip()
    if not text:
        return ""
    author = tweet.get("author", {}).get("name", "")
    handle = tweet.get("author", {}).get("screen_name", "")
    likes = tweet.get("likes", 0)
    retweets = tweet.get("retweets", 0)
    views = tweet.get("views", 0)
    title = f"{author} (@{handle}) on X" if author else "Tweet"
    stats = f"Likes: {likes:,} | RT: {retweets:,} | Views: {views:,}"
    return f"**{title}** ({url})\n\n{text}\n\n---\n{stats}"


def _tweet_oembed_fallback(url: str, *, timeout: float = 10) -> str:
    """Fallback: oEmbed (may truncate long tweets)."""
    import html as html_lib
    import httpx

    try:
        response = httpx.get(
            "https://publish.twitter.com/oembed",
            params={"url": url, "omit_script": "true", "dnt": "true"},
            timeout=timeout,
            follow_redirects=True,
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        return ""

    raw_html = str(data.get("html", "")).strip()
    if not raw_html:
        return ""
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    text_parts: list[str] = []
    for paragraph in paragraphs:
        text = re.sub(r"<br\s*/?>", "\n", paragraph, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = html_lib.unescape(text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if text:
            text_parts.append(text)
    if not text_parts:
        return ""
    author_name = str(data.get("author_name", "")).strip()
    title = f"{author_name} on X" if author_name else "Tweet"
    content = "\n\n".join(text_parts)
    return f"**{title}** ({url})\n\n{content}"


def _format_chrome_cdp_error(exc: Exception, *, prefix: str) -> str:
    message = str(exc)
    lowered = message.lower()
    if any(token in lowered for token in ("econnrefused", "connection refused", "connect_over_cdp", "browser_type.connect_over_cdp")):
        return "Chrome del bot no responde. Reinicia el bot o verifica que Chrome esté instalado."
    return f"{prefix}: {message}"


def _parse_non_negative_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be greater than or equal to 0")
    return parsed


def _parse_positive_int(value: str, *, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return parsed


def _parse_float(value: str, *, field_name: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number") from exc


def _agent_summary(agent_name: str, state: dict, *, include_instruction: bool = False) -> dict:
    summary = {
        "name": agent_name,
        "agent_class": state.get("agent_class"),
        "allowed_tools": state.get("allowed_tools", []),
        "paused": state.get("paused", False),
        "pause_reason": state.get("pause_reason", ""),
        "last_action": state.get("last_action", ""),
        "last_error": state.get("last_error", ""),
        "consecutive_failures": state.get("consecutive_failures", 0),
        "trust_level": state.get("trust_level", 1),
        "experiments_today": state.get("experiments_today", 0),
        "last_metric": state.get("last_verified_state", {}).get("metric"),
        "promote_on_improvement": state.get("promote_on_improvement", False),
        "commit_on_promotion": state.get("commit_on_promotion", False),
        "branch_on_promotion": state.get("branch_on_promotion", False),
        "promotion_commit_message": state.get("promotion_commit_message"),
        "promotion_branch_name": state.get("promotion_branch_name"),
    }
    if include_instruction:
        summary["instruction"] = state.get("instruction")
    return summary


def _run_summary(agent_name: str, state: dict, result: object) -> dict:
    summary = _agent_summary(agent_name, state, include_instruction=True)
    summary.update(
        {
            "experiments_run": getattr(result, "experiments_run"),
            "run_reason": getattr(result, "reason"),
            "run_paused": getattr(result, "paused"),
            "run_last_metric": getattr(result, "last_metric"),
        }
    )
    return summary


def _record_summary(record: object) -> dict:
    return {
        "experiment_number": getattr(record, "experiment_number"),
        "metric_value": getattr(record, "metric_value"),
        "baseline_value": getattr(record, "baseline_value"),
        "status": getattr(record, "status"),
        "cost_usd": getattr(record, "cost_usd"),
        "promotion_commit_sha": getattr(record, "promotion_commit_sha"),
        "promotion_branch_name": getattr(record, "promotion_branch_name"),
    }


def _default_pull_request_title(agent_name: str, payload: dict) -> str:
    explicit = payload.get("promotion_commit_message")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return f"chore(claw): publish {agent_name}"


def _default_pull_request_body(agent_name: str, payload: dict) -> str:
    lines = [
        "Automated draft pull request created by Claw.",
        "",
        f"- Agent: {agent_name}",
        f"- Experiments run: {payload.get('experiments_run')}",
        f"- Run reason: {payload.get('run_reason')}",
        f"- Last metric: {payload.get('run_last_metric')}",
        f"- Commit: {payload.get('published_commit_sha')}",
        f"- Branch: {payload.get('published_branch_name')}",
    ]
    return "\n".join(lines)


def _help_response(topic: str | None = None) -> str:
    if topic is None:
        return (
            "Comandos principales:\n"
            "/status - salud general del bot\n"
            "/approvals - aprobaciones pendientes\n"
            "/traces [limit] - traces recientes\n"
            "/trace <trace_id> [limit] - replay de una traza\n"
            "/spending - gasto LLM del día por lane/proveedor/modelo\n"
            "/freeze - pausar autoexec durante observación\n"
            "/unfreeze - reactivar autoexec\n"
            "/budget_status - costo, presupuesto y breakers activos\n"
            "/models - listar modelos registrados y su origen/billing\n"
            "/model status - ver modelos efectivos por lane\n"
            "/model set <lane> <provider:model> [effort=...] - override de modelo por sesión\n"
            "/pipeline_status - pipelines activos\n"
            "/agents - estado de agentes\n"
            "/browse <url> - revisar una URL\n"
            "/screen - screenshot actual\n"
            "/computer <instruccion> - operar el escritorio\n"
            "/terminal_list - sesiones PTY abiertas\n"
            "/nlm_create <tema> - crear cuaderno de NotebookLM\n"
            "/nlm_list - listar cuadernos de NotebookLM\n"
            "/autonomy [mode] - ver o ajustar autonomía de la sesión\n"
            "/autonomy_policy - ver política efectiva de autonomía\n"
            "/jobs - listar trabajos autónomos persistidos de la sesión\n"
            "/job_status <task_id> - ver detalle de un trabajo\n"
            "/job_trace <task_id> [limit] - replay de eventos de un trabajo\n"
            "/job_cancel <job_id> - cancelar un job genérico/background\n"
            "/task_resume <task_id> - reanudar un trabajo interrumpido\n"
            "/task_cancel <task_id> - cancelar un trabajo autónomo\n"
            "/task_run <objetivo> - ejecutar ciclo coordinado\n"
            "/task_loop - inspeccionar presupuesto y checkpoint actual\n"
            "/task_queue [mode] - listar siguientes pasos pendientes de la sesión\n"
            "/task_done <task_id> - marcar paso de la cola como completado\n"
            "/task_defer <task_id> - posponer paso de la cola\n"
            "/task_pending - listar aprobaciones/tareas pendientes de la sesión\n"
            "/session_state - inspeccionar estado persistente de la sesión\n"
            "/playbooks - listar playbooks disponibles\n"
            "/playbook <nombre> - ver detalle de un playbook\n"
            "/backtest <instrucción> - correr backtesting con contexto QTS\n"
            "/grill <plan> - entrevista rigurosa sobre un plan o diseño\n"
            "/tdd <feature> - desarrollo TDD red-green-refactor\n"
            "/improve_arch [foco] - review arquitectural del codebase\n"
            "/effort <level> [lane] - ajustar esfuerzo (low/medium/high/xhigh/max)\n"
            "/verify [foco] - verificar trabajo actual (tests + calidad)\n"
            "/focus - toggle focus mode (solo resultados finales)\n"
            "/voice [voz] - responder por audio (alloy/echo/fable/onyx/nova/shimmer)\n"
            "\n"
            "Ayuda por tema:\n"
            "/help approvals\n"
            "/help pipeline\n"
            "/help agents\n"
            "/help traces\n"
            "/help terminal\n"
            "/help browser\n"
            "/help social\n"
            "/help notebooklm\n"
            "/help autonomy\n"
            "/help observability\n"
            "/help spending"
        )

    normalized = topic.strip().lower().replace("-", "").replace("_", "")
    if normalized in {"approval", "approvals"}:
        return (
            "Aprobaciones:\n"
            "/approvals - listar pendientes\n"
            "/approval_status <approval_id> - ver estado\n"
            "/approve <approval_id> <token> - aprobar manualmente\n"
            "/task_approve <approval_id> <token> - aprobar tarea coordinada pendiente\n"
            "/task_abort <approval_id> - abortar tarea coordinada pendiente\n"
            "/action_approve <approval_id> <token> - aprobar acción pendiente\n"
            "/action_abort <approval_id> - abortar acción pendiente"
        )
    if normalized in {"pipeline", "pipelines"}:
        return (
            "Pipeline:\n"
            "/pipeline <issue_id> [repo_root] - ejecutar pipeline\n"
            "/pipeline_status - ver runs activos\n"
            "/pipeline_approve <approval_id> <token> - aprobar y completar pipeline\n"
            "/pipeline_merge <issue_id> - merge y cierre"
        )
    if normalized in {"trace", "traces", "replay"}:
        return (
            "Trazas:\n"
            "/traces [limit] - listar traces recientes\n"
            "/trace <trace_id> [limit] - ver replay de eventos para una traza"
        )
    if normalized in {"spending", "cost", "costs", "gasto", "costos"}:
        return (
            "Costos:\n"
            "/spending - gasto LLM de hoy desglosado por lane, provider y modelo\n"
            "/budget_status - costo, presupuesto restante, freeze y circuit breakers\n"
            "/tokens - estimación de uso de contexto de la sesión"
        )
    if normalized in {"observability", "observation", "observacion", "observación"}:
        return (
            "Observabilidad:\n"
            "/freeze - pausar autoexec y bloquear tool dispatch\n"
            "/unfreeze - reactivar autoexec\n"
            "/budget_status - ver costo, presupuesto restante y breakers\n"
            "Dashboard local: http://127.0.0.1:8765/observability"
        )
    if normalized in {"agent", "agents"}:
        return (
            "Agentes:\n"
            "/agents - listar agentes\n"
            "/agent_status <agent_name> - ver detalle\n"
            "/agent_create <name> <researcher|operator|deployer> <instruction> - crear agente\n"
            "/agent_run <agent_name> <max_experiments> - correr loop\n"
            "/agent_publish <agent_name> <max_experiments> - publicar cambios\n"
            "/agent_pr <agent_name> <max_experiments> - abrir draft PR\n"
            "/agent_pause <agent_name> - pausar\n"
            "/agent_resume <agent_name> - reanudar\n"
            "/agent_history <agent_name> [limit] - historial reciente"
        )
    if normalized in {"terminal", "terminals", "pty"}:
        return (
            "Terminal:\n"
            "/terminal_list - listar sesiones\n"
            "/terminal_open <claude|codex> [cwd] - abrir PTY\n"
            "/terminal_status <session_id> - ver estado\n"
            "/terminal_read <session_id> [offset] - leer salida\n"
            "/terminal_send <session_id> <text> - enviar texto\n"
            "/terminal_close <session_id> - cerrar sesión"
        )
    if normalized in {"browser", "browse", "chrome", "screen", "computer"}:
        return (
            "Navegación y escritorio:\n"
            "/browse <url> - revisar una URL\n"
            "/chrome_pages - listar tabs de Chrome\n"
            "/chrome_browse <url> - abrir URL en Chrome\n"
            "/chrome_shot - screenshot del tab actual\n"
            "/chrome_download [ext] - esperar descarga CDP\n"
            "/chrome_login - abrir Chrome visible para login\n"
            "/chrome_headless - volver a headless\n"
            "/screen - screenshot del escritorio\n"
            "/computer <instruccion> - controlar escritorio\n"
            "/computer_abort - cancelar sesión activa"
        )
    if normalized in {"social", "socialmedia"}:
        return (
            "Social:\n"
            "/social_status - listar cuentas\n"
            "/social_preview <cuenta> - previsualizar posts\n"
            "/social_publish <cuenta> - publicar batch"
        )
    if normalized in {"notebooklm", "nlm", "notebook"}:
        return (
            "NotebookLM:\n"
            "/nlm_list - listar cuadernos\n"
            "/nlm_create <tema> - crear cuaderno\n"
            "/nlm_status <notebook_id> - ver estado\n"
            "/nlm_sources <notebook_id> <url1,url2,...> - agregar fuentes\n"
            "/nlm_text <notebook_id> <titulo> | <contenido> - agregar nota\n"
            "/nlm_research <notebook_id> <consulta> - correr research\n"
            "/nlm_podcast [notebook_id] - generar podcast\n"
            "/nlm_chat <notebook_id> <pregunta> - chatear con el cuaderno"
        )
    if normalized in {"autonomy", "sessionstate", "state"}:
        return (
            "Autonomía de sesión:\n"
            "/autonomy - ver estado actual\n"
            "/autonomy <manual|assisted|autonomous> - ajustar modo\n"
            "/autonomy_policy - ver límites efectivos del modo actual\n"
            "/jobs - ver trabajos persistidos de la sesión\n"
            "/job_status <task_id> - inspeccionar un trabajo\n"
            "/job_trace <task_id> [limit] - ver eventos asociados al trabajo\n"
            "/job_cancel <job_id> - cancelar un job genérico/background\n"
            "/task_resume <task_id> - reanudar un trabajo interrumpido\n"
            "/task_cancel <task_id> - cancelar un trabajo autónomo\n"
            "/task_run <objetivo> - disparar research/synthesis/implementation/verification\n"
            "/task_loop - ver presupuesto, pasos y checkpoint\n"
            "/task_queue [mode] - ver siguientes pasos persistidos, opcionalmente filtrados por mode\n"
            "/task_done <task_id> - marcar un paso como done\n"
            "/task_defer <task_id> - marcar un paso como deferred\n"
            "/task_pending - ver aprobaciones pendientes en la sesión\n"
            "/session_state - inspeccionar estado persistente\n"
            "El estado guarda objetivo actual, acción pendiente, objeto activo y opciones recientes."
        )
    return (
        f"Tema de ayuda no reconocido: {topic}\n"
        "Prueba con: approvals, pipeline, agents, terminal, browser, social, notebooklm, spending."
    )
