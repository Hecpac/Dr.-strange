from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

logger = logging.getLogger(__name__)

from claw_v2.agents import AutoResearchAgentService
from claw_v2.agent_handler import AgentHandler
from claw_v2.browse_handler import BrowseHandler
from claw_v2.task_handler import TaskHandler
from claw_v2.approval import ApprovalManager
from claw_v2.approval_gate import ApprovalPending, approved_tool_invocation
from claw_v2.brain import BrainService
from claw_v2.bot_commands import BotCommand, CommandContext, dispatch_commands
from claw_v2.dispatch import Route, RouteContext, RouteOutcome, dispatch_routes
from claw_v2.capability_router import (
    CapabilityRoute,
    RuntimeAliveProbe,
    classify_autonomy_intent,
    route_request,
)
from claw_v2.capability_preflight import CapabilityPreflightResult, preflight_objective
from claw_v2.execution_environment import (
    ExecutionEnvironment,
    detect_execution_environment,
)
from claw_v2.runtime_handoff import (
    create_runtime_handoff,
    format_handoff_message,
)
from claw_v2.checkpoint_handler import CheckpointHandler
from claw_v2.chrome_handler import ChromeHandler
from claw_v2.computer_handler import ComputerHandler
from claw_v2.design_handler import DesignHandler
from claw_v2.nlm_handler import NlmHandler
from claw_v2.natural_language_renderer import NaturalLanguageRenderer
from claw_v2.state_handler import StateHandler, _BrainShortcut, reply_context_fresh
from claw_v2.semantic_turn import SemanticTurn, classify_semantic_turn
from claw_v2.terminal_handler import TerminalHandler
from claw_v2.wiki_handler import WikiHandler
from claw_v2.coordinator import CoordinatorService
from claw_v2.content import ContentEngine
from claw_v2.redaction import redact_sensitive
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService, HeartbeatSnapshot, _compute_health
from claw_v2.idle_executor import IdleOwnershipExecutor
from claw_v2.stop_notifier import StopNotifier
from claw_v2.model_registry import (
    ModelRegistry,
    ModelOverride,
    model_overrides_from_state,
    normalize_model_lane,
    serialize_model_overrides,
)
from claw_v2.pipeline import PipelineService
from claw_v2.social import SocialPublisher
from claw_v2.bot_helpers import *  # noqa: F403
from claw_v2.bot_helpers import _is_secret_shaped_token, verify_brain_tooluse  # explicit: private helper + B1 verifier seam
from claw_v2.turn_context import current_turn_id, turn_id_context
from claw_v2.turn_receipt import emit_turn_receipt

if TYPE_CHECKING:
    from claw_v2.jobs import JobService


_DEFAULT_COMPUTER_MODEL = "gpt-5.4"
_COMPUTER_SYSTEM_PROMPT = (
    "You control the user's Mac via the computer-use tool. "
    "Be careful, explicit, and incremental. "
    "Prefer reading the current screen before acting. "
    "When searching for a visible UI element, move/scroll as needed, then click only when confident. "
    "Stop and explain what you see when the task is complete."
)
_CAPABILITY_MESSAGES = {
    "chrome_cdp": "Lo siento, mi módulo de navegación está actualmente degradado y no puedo acceder a Chrome/CDP.",
    "computer_use": "Lo siento, mi módulo de control de escritorio está actualmente degradado y no puedo operar la computadora.",
    "computer_control": "Lo siento, mi módulo de control de escritorio está actualmente degradado y no puedo ejecutar acciones interactivas.",
    "browser_use": "Lo siento, mi backend de automatización web está actualmente degradado y no puedo completar ese flujo web.",
}
_CHATGPT_TARGET_TOKENS = ("chatgpt", "chat gpt", "chat.openai.com")
_CHATGPT_OPEN_TOKENS = (
    "abre",
    "abrir",
    "open",
    "inicia",
    "iniciar",
    "nuevo chat",
    "nueva conversacion",
    "new chat",
)
_BACKGROUND_MONITOR_PROMISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bwatcher\b", re.IGNORECASE),
    re.compile(r"\b(?:en|in)\s+background\b|\bbackground\s+(?:task|job|watcher|process|worker)\b", re.IGNORECASE),
    re.compile(r"\bdispatch\s+durable\b", re.IGNORECASE),
    re.compile(r"\bsobreviv[ae]\s+(?:a\s+)?interrupciones?\b", re.IGNORECASE),
    re.compile(r"\bmonitore(?:ar|o|ando|aré|are|e|é)\b", re.IGNORECASE),
    re.compile(r"\bte\s+aviso\s+cuando\b", re.IGNORECASE),
    re.compile(r"\blo\s+dejo\s+(?:corriendo|trabajando|en\s+background)\b", re.IGNORECASE),
    re.compile(r"\bsin\s+intervenci[oó]n\s+tuya\b", re.IGNORECASE),
    re.compile(r"\bno\s+necesit[aá]s\s+hacer\s+nada\b", re.IGNORECASE),
    re.compile(r"\bpolling\b|\bpoll(?:ea|ear|ando|ando)\b", re.IGNORECASE),
    re.compile(r"\bcuando\s+.+?\btermine\b.+?\b(?:descarg|extra|notific|avis|renombr|entreg|mand|envi|devuelv|report)", re.IGNORECASE | re.DOTALL),
)
_DURABLE_DISPATCH_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdispatch\s+durable\b", re.IGNORECASE),
    re.compile(r"\bsobreviv[ae]\s+(?:a\s+)?interrupciones?\b", re.IGNORECASE),
    re.compile(r"\bno\s+es\s+una\s+promesa\s+de\s+background\b", re.IGNORECASE),
)
_BRAIN_TOOLUSE_VERIFIED_ACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:publica(?:lo|la|los|las)?|publicaste|publish|posted|postear?)\b", re.IGNORECASE),
    re.compile(r"\bpudiste\b", re.IGNORECASE),
    re.compile(r"\b(?:lee|leer|read)\s+(?:los?\s+)?(?:docs?|documentos?)\b", re.IGNORECASE),
    re.compile(r"\babre\s+(?:instagram|x|twitter|linkedin|chrome)\b", re.IGNORECASE),
    re.compile(r"\blisto\s+(?:logueado|loggeado|logged\s+in)\b", re.IGNORECASE),
    re.compile(r"\b(?:arranca|arrancar|empieza|inicia)\s+con\s+(?:el\s+)?plan\b", re.IGNORECASE),
)
_BACKGROUND_MONITOR_ACTIVE_JOB_STATUSES = ("queued", "running", "waiting_approval", "retrying")
_BACKGROUND_MONITOR_ACTIVE_TASK_STATUSES = ("queued", "running")
_NOTEBOOKLM_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_CHATGPT_INTERACTIVE_TOKENS = (
    "pidele",
    "pidelo",
    "crea",
    "crear",
    "genera",
    "generar",
    "imagen",
    "foto",
    "image",
    "prompt",
    "escribe",
    "manda",
    "envia",
)
_CHATGPT_DIRECT_ACTION_TOKENS = _CHATGPT_OPEN_TOKENS + (
    "pidele",
    "pidelo",
    "preguntale",
    "dile a chatgpt",
    "en chatgpt",
    "usa chatgpt",
    "use chatgpt",
)
_CHATGPT_COMPOUND_FALLTHROUGH_TOKENS = (
    "verifica",
    "valid",
    "fuente primaria",
    "fuentes primarias",
    "cifras",
    "websearch",
    "ajusta los drafts",
    "publica",
)
_CHATGPT_CONTEXTUAL_REFERENCE_RE = re.compile(
    r"\b(?:haz|hace|crea|genera|regenera|arma)(?:lo|la|los|las)\b.*\bchat\s*gpt\b"
    r"|\b(?:haz|hace|crea|genera|regenera|arma)(?:lo|la|los|las)\b.*\bchatgpt\b"
    r"|\b(?:esto|eso|lo|la|esta|esa)\b.*\b(?:chat\s*gpt|chatgpt)\b",
    re.IGNORECASE,
)
_CAPABILITY_DENIAL_TERMS = (
    "no puedo",
    "no tengo acceso",
    "no tengo una herramienta",
    "no dispongo",
    "no hay acceso",
    "no hay herramienta",
    "habilita",
    "habilitas",
    "browser bridge",
)
_CAPABILITY_SURFACE_TERMS = (
    "navegador",
    "browser",
    "chrome",
    "cdp",
    "desktop",
    "escritorio",
    "terminal",
    "herramienta",
    "tool",
)


_PRE_HOOK_BLOCK_PREFIX = "Request blocked by pre-hook"
_PRE_HOOK_BLOCK_RE = re.compile(
    r"^Request blocked by pre-hook \(([^)]+)\)\. Reason: (.+)$"
)


def _looks_like_chatgpt_browser_request(normalized: str) -> bool:
    if not any(token in normalized for token in _CHATGPT_TARGET_TOKENS):
        return False
    if _chatgpt_reference_should_fallthrough_to_brain(normalized):
        return False
    return any(token in normalized for token in _CHATGPT_OPEN_TOKENS + _CHATGPT_INTERACTIVE_TOKENS)


def _looks_like_chatgpt_interactive_request(normalized: str) -> bool:
    if not any(token in normalized for token in _CHATGPT_TARGET_TOKENS):
        return False
    if _chatgpt_reference_should_fallthrough_to_brain(normalized):
        return False
    return any(token in normalized for token in _CHATGPT_INTERACTIVE_TOKENS)


def _chatgpt_reference_should_fallthrough_to_brain(normalized: str) -> bool:
    if _CHATGPT_CONTEXTUAL_REFERENCE_RE.search(normalized):
        return True
    if re.search(r"\bchat\s*gpt(?:\s+image)?\s+o\s+nano\s+banana\b", normalized):
        return True
    if re.search(r"\bchatgpt(?:\s+image)?\s+o\s+nano\s+banana\b", normalized):
        return True
    if not any(token in normalized for token in _CHATGPT_COMPOUND_FALLTHROUGH_TOKENS):
        return False
    return not any(token in normalized for token in _CHATGPT_DIRECT_ACTION_TOKENS)


def _chatgpt_browser_task_instruction(text: str) -> str:
    return (
        "Usa Chrome/CDP con la sesión autenticada del usuario. "
        "Si ya hay un tab de ChatGPT abierto, úsalo. Si no, abre https://chatgpt.com/ en un chat nuevo. "
        "Realiza exactamente esta tarea, verifica el resultado visible y termina con un resumen breve:\n"
        f"{text.strip()}"
    )


_CAPABILITY_DENIAL_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")
_CAPABILITY_DENIAL_MAX_LEN = 600
_IDENTITY_DRIFT_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")
_IDENTITY_DRIFT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsoy\s+(?:claude|claude code|codex|chatgpt|un modelo|una ia|un asistente de ia)\b"),
    re.compile(r"\bmi identidad\s+es\s+(?:claude|claude code|codex|chatgpt|un modelo|una ia)\b"),
    re.compile(r"\bcomo\s+(?:claude|claude code|codex|chatgpt|modelo|ia)\b"),
    re.compile(r"\bestoy\s+(?:corriendo|ejecutandome|en)\s+(?:claude code|codex cli|el cli|la cli)\b"),
    re.compile(r"\bthis\s+(?:claude code|codex|chatgpt)\s+(?:session|instance)\b"),
    re.compile(r"\bi\s*(?:am|'m)\s+(?:claude|claude code|codex|chatgpt|an ai language model)\b"),
    re.compile(r"\bas\s+(?:claude|claude code|codex|chatgpt|an ai language model)\b"),
)
_IDENTITY_DRIFT_SAFE_CONTEXT = (
    "no soy claude",
    "no soy claude code",
    "no soy codex",
    "no debo decir que soy",
    "nunca decir que soy",
    "no deberia decir que soy",
    "do not identify as",
    "never identify as",
)
_MANUAL_HANDOFF_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bpasos finales\b"),
    re.compile(r"\bcorre\s+este\s+comando\b"),
    re.compile(r"\bejecuta(?:lo)?\s+tu\b"),
    re.compile(r"\bpruebalo\s+tu\b"),
    re.compile(r"\bcopia\s+y\s+pega\b"),
    re.compile(r"\bte\s+toca\s+(?:a\s+ti)?\b"),
    re.compile(r"\b(?:vos|tu|t[uú])\s+en\s+la\s+mac\b"),
    re.compile(r"\b(?:click|haz click|hace click)\s+en\s+la\s+ventana\b"),
    re.compile(r"\bcmd\s*\+?\s*v\b"),
    re.compile(r"\b(?:cmd|command)\s*\+?\s*(?:enter|return)\b"),
    re.compile(r"\b(?:queda|quedo)\s+contigo\b"),
    re.compile(r"\b(?:te toca|hazlo tu|hazlo t[uú]|lo haces tu|lo haces t[uú])\b"),
    re.compile(r"\b(?:desde aqui|desde aqu[ií])\s+no\s+puedo\b"),
    re.compile(r"\bno\s+(?:se\s+lo\s+)?(?:pegue|pegu[eé]|pude pegar|puedo pegar)\s+yo\b"),
    re.compile(r"\b(?:run this command|try it yourself|copy and paste this|paste it yourself|you need to click|you do the final|press enter yourself)\b"),
)
_OPERATOR_ACTION_REQUEST_TERMS = (
    "abre",
    "abrir",
    "actualiza",
    "aplica",
    "arregla",
    "cierra",
    "completa",
    "continua",
    "continúa",
    "corrige",
    "corre",
    "correlo",
    "córrelo",
    "crea",
    "dale",
    "ejecuta",
    "encargate",
    "encárgate",
    "envia",
    "envía",
    "enviame",
    "envíame",
    "genera",
    "hazlo",
    "instala",
    "levanta",
    "limpia",
    "limpiar",
    "manda",
    "mandalo",
    "mándalo",
    "pega",
    "pegale",
    "pégale",
    "revisa",
    "retoma",
    "run",
    "open",
    "paste",
    "review",
    "send",
    "create",
    "generate",
    "install",
    "continue",
    "resume",
    "execute",
    "fix",
    "clean",
    "cleanup",
    "clean up",
    "take ownership",
)
_COMPLETION_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:listo|hecho|done|cerrado|terminado|verificado)\b"),
    re.compile(r"\b(?:lo\s+correg[ií]|lo\s+limpi[eé]|lo\s+arregl[eé]|cambi[eé]\s+el|actualic[eé]\s+el)\b"),
    re.compile(r"\b(?:i\s+fixed|i\s+changed|i\s+updated|completed|verified)\b"),
)
_SIDE_EFFECT_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:archivo|file|comando|command|test|tests|deploy|mensaje|email|prompt|app|codex|approval|approvals|aprobaciones|ledger|cola)\b"),
    re.compile(r"\b(?:corr[ií]|ejecut[eé]|corr[eí]\s+tests?|ran|changed|updated|sent|submitted|pasted)\b"),
)
# A turn that attributes a CONCRETE returned record to a search/lookup
# ("la busqueda me devolvio ...", "el lookup arrojo el dueno") AND frames the
# data source as confirmed/real ("fuente ... confirmada", "datos confirmados",
# "duenos reales"). Matched against `_normalize_command_text` (lowercase,
# diacritics stripped) so all literals are ASCII-only. Used to block such a
# claim when the only tool evidence for the turn is a FAILED tool run.
_RETURNED_RECORD_ATTR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:la\s+)?(?:busqueda|consulta|lookup|query)\s+(?:me\s+)?(?:devolvio|arrojo|retorno|trajo|dio)\b"),
    re.compile(r"\b(?:devolvio|arrojo|retorno|trajo)\s+(?:el\s+|los\s+|un\s+|unos\s+)?(?:dueno|duenos|owner|owners|deed|record|registro|propietario|propietarios)\b"),
)
_CONFIRMED_SOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfuente\s+(?:de\s+datos\s+)?(?:esta\s+)?confirmad[ao]\b"),
    re.compile(r"\bdatos\s+confirmad[oa]s\b"),
    re.compile(r"\b(?:datos|fuente|owners?|duenos?)\s+reales\b"),
    re.compile(r"\bduenos?\s+reales\b"),
)
_STARTING_ACTION_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:voy\s+a|procedo\s+a|empiezo|arranco|arrancando|iniciando)\b"),
    re.compile(r"\b(?:i\s+will|i'll|i\s+am\s+going\s+to|i'm\s+going\s+to|starting|started)\b"),
)
_STARTING_ACTION_OBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:depur|limpi|archiv|ejecut|corr|aplic|actualiz|arregl)\w*\b"),
    re.compile(r"\b(?:clean|cleanup|archive|execute|run|apply|update|fix)\w*\b"),
    re.compile(r"\b(?:approval|approvals|aprobaciones|ledger|cola|archivo|file|comando|command|task|tarea)\b"),
)
_PLAN_STATUS_SOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bprimero\s+(?:entrega|prepara|dame|devuelve)\s+(?:el\s+)?plan\b"),
    re.compile(r"\b(?:entrega|prepara|dame|devuelve)\s+(?:el\s+)?plan\s+[a-z0-9_.-]+\b"),
    re.compile(r"\b(?:preflight|requerido|required)\b"),
    re.compile(r"\bexternal_check\b"),
    re.compile(r"\bevidence_uri\b"),
    re.compile(r"\bendpoints?\s+(?:o\s+fuentes\s+)?read-only\b"),
    re.compile(r"\bcondiciones exactas\b"),
)
_STATUS_ACK_SOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bok\s+final\s*:\s*marca\b"),
    re.compile(r"\bmarca\s+[a-z0-9_.+-]+\s+como\s+(?:done|succeeded|completed|list[oa]|cerrad[oa]|terminad[oa])\b"),
    re.compile(r"\bdeja\s+[a-z0-9_.+-]+\s+como\s+(?:done|succeeded|completed|list[oa]|cerrad[oa]|terminad[oa])\b"),
)
_PLAN_STATUS_RESPONSE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*#+\s*plan\b", re.MULTILINE),
    re.compile(r"\bplan\s+[a-z0-9_.-]+\b"),
    re.compile(r"\bendpoints?\s+(?:o\s+fuentes\s+)?read-only\b"),
    re.compile(r"\bpreflight\b"),
    re.compile(r"\bexternal_check\b"),
    re.compile(r"\bevidence_uri\b"),
    re.compile(r"\bcondiciones exactas\b"),
    re.compile(r"\btests?\s+mock\b"),
    re.compile(r"\bmarcad[ao]s?\s+(?:como\s+)?(?:done|succeeded|completed|list[oa]s?)\b"),
)
_EXPLICIT_EXECUTION_REPORT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:cambie|actualice|modifique|edite|cree|agregue|borre|elimine|publique|envie|mande|desplegue|pushee|mergee|commitee|instale|arregle|corregi|limpie)\b"
    ),
    re.compile(
        r"\b(?:corri|ejecute|lance)\s+(?:el\s+|los\s+|las\s+)?(?:test|tests|pytest|comando|script|smoke|smokes)\b"
    ),
    re.compile(
        r"\b(?:i\s+fixed|i\s+changed|i\s+updated|i\s+created|i\s+deleted|i\s+sent|i\s+published|i\s+deployed|ran\s+tests?|i\s+ran)\b"
    ),
)


def _looks_like_unverified_capability_denial(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if len(normalized) > _CAPABILITY_DENIAL_MAX_LEN:
        return False
    for sentence in _CAPABILITY_DENIAL_SENTENCE_SPLIT.split(normalized):
        if any(term in sentence for term in _CAPABILITY_DENIAL_TERMS) and any(
            term in sentence for term in _CAPABILITY_SURFACE_TERMS
        ):
            return True
    return False


def _looks_like_identity_drift(text: str) -> bool:
    normalized = _normalize_command_text(text)
    for sentence in _IDENTITY_DRIFT_SENTENCE_SPLIT.split(normalized):
        compact = re.sub(r"\s+", " ", sentence).strip()
        if not compact:
            continue
        if any(safe in compact for safe in _IDENTITY_DRIFT_SAFE_CONTEXT):
            continue
        if any(pattern.search(compact) for pattern in _IDENTITY_DRIFT_PATTERNS):
            return True
    return False


def _contains_substantive_operational_content(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if len(normalized) < 180:
        return False
    markers = (
        "accion",
        "acciones",
        "audit",
        "auditoria",
        "evidencia",
        "hallazgo",
        "inventario",
        "mcp",
        "resultado",
        "riesgo",
        "server",
        "tabla",
        "verificado",
    )
    return any(marker in normalized for marker in markers) or bool(
        re.search(r"^\s*(?:[-*#]|\|)", text, flags=re.MULTILINE)
    )


def _looks_like_manual_handoff(text: str) -> bool:
    normalized = _normalize_command_text(text)
    normalized = re.sub(r"\s+", " ", normalized)
    return any(pattern.search(normalized) for pattern in _MANUAL_HANDOFF_PATTERNS)


def _looks_like_operator_action_request(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not normalized.strip():
        return False
    try:
        if detect_telegram_imperative(text) is not None or detect_owner_delegation(text) is not None:
            return True
    except Exception:
        logger.exception(
            "dispatch detector failed in _looks_like_operator_action_request"
        )
    if looks_like_actionable_telegram_message(text):
        return True
    return _contains_operator_action_term(normalized)


def _contains_operator_action_term(normalized_text: str) -> bool:
    for term in _OPERATOR_ACTION_REQUEST_TERMS:
        pattern = rf"(?<!\w){re.escape(term)}(?!\w)"
        if re.search(pattern, normalized_text):
            return True
    return False


def _looks_like_completion_side_effect_claim(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not normalized.strip():
        return False
    has_completion = any(pattern.search(normalized) for pattern in _COMPLETION_CLAIM_PATTERNS)
    if not has_completion:
        return False
    return any(pattern.search(normalized) for pattern in _SIDE_EFFECT_CLAIM_PATTERNS)


def _looks_like_starting_side_effect_claim(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not normalized.strip():
        return False
    if not any(pattern.search(normalized) for pattern in _STARTING_ACTION_CLAIM_PATTERNS):
        return False
    return any(pattern.search(normalized) for pattern in _STARTING_ACTION_OBJECT_PATTERNS)


def _looks_like_confirmed_returned_record_claim(text: str) -> bool:
    """True when the outgoing message both (a) attributes a concrete returned
    record to a search/lookup and (b) frames the data source as confirmed/real.

    This is the shape that justified a paid skip-trace spend off a record that
    only existed in a FAILED tool run (msg 1095). It must not be surfaced as a
    confirmed lookup unless an exit-0 tool result backs it.
    """
    normalized = _normalize_command_text(text)
    if not normalized.strip():
        return False
    has_attr = any(pattern.search(normalized) for pattern in _RETURNED_RECORD_ATTR_PATTERNS)
    if not has_attr:
        return False
    return any(pattern.search(normalized) for pattern in _CONFIRMED_SOURCE_PATTERNS)


def _looks_like_plan_or_status_only_source(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not normalized.strip():
        return False
    has_plan_shape = any(pattern.search(normalized) for pattern in _PLAN_STATUS_SOURCE_PATTERNS)
    has_status_ack = any(pattern.search(normalized) for pattern in _STATUS_ACK_SOURCE_PATTERNS)
    has_execution_guard = any(
        marker in normalized
        for marker in (
            "no ejecutes",
            "no ejecutes tools",
            "no hagas llamadas reales",
            "no llamadas reales",
            "cero tools",
            "cero llamadas reales",
            "read-only/status-only",
            "solo plan",
            "solamente plan",
        )
    )
    return has_status_ack or (has_plan_shape and has_execution_guard)


def _looks_like_plan_or_status_response(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not normalized.strip():
        return False
    if any(pattern.search(normalized) for pattern in _EXPLICIT_EXECUTION_REPORT_PATTERNS):
        return False
    return any(pattern.search(normalized) for pattern in _PLAN_STATUS_RESPONSE_PATTERNS)


def _evidence_gate_plan_status_skip_reason(source_text: str, content: str) -> str | None:
    if not _looks_like_plan_or_status_only_source(source_text):
        return None
    if not _looks_like_plan_or_status_response(content):
        return None
    return "plan_or_status_ack_without_execution"


_USER_AUTHORITATIVE_DONE_PATTERNS = (
    re.compile(r"\bok\s+final\b[^\n]*\bmarca\b", re.IGNORECASE),
    re.compile(r"\bmarca\s+(?:la\s+|el\s+|las\s+|los\s+)?\S[^\n]{0,80}?\s+como\s+(?:done|succeeded|listo|cerrad[oa]|terminad[oa]|completad[oa]|hech[oa])\b", re.IGNORECASE),
    re.compile(r"\bdeja\s+(?:la\s+|el\s+|las\s+|los\s+)?\S[^\n]{0,80}?\s+como\s+done\b", re.IGNORECASE),
    re.compile(r"\bya\s+quedó\s+(?:done|listo|cerrad[oa]|terminad[oa]|hech[oa])\b", re.IGNORECASE),
    re.compile(r"\bmark\s+\S[^\n]{0,80}?\s+as\s+(?:done|succeeded|complete[d]?|closed)\b", re.IGNORECASE),
)


def _user_authoritatively_marked_done(source_text: str) -> bool:
    if not source_text:
        return False
    return any(pattern.search(source_text) for pattern in _USER_AUTHORITATIVE_DONE_PATTERNS)


_EVIDENCE_REFERENCE_PATTERNS = (
    re.compile(r"\bartifacts/(?:verification|heygen|content|x_sweep|notebooklm|behavior_audit|email)/\S+", re.IGNORECASE),
    re.compile(r"\bevidence_uri\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bevidence\s*[:=]\s*[\"\']?artifacts/\S+", re.IGNORECASE),
    re.compile(r"\bf3b\d+_\w+_\d+\.(?:json|log)\b", re.IGNORECASE),
    re.compile(r"\bcorrelation_id\s*[:=]\s*[a-f0-9-]{8,}", re.IGNORECASE),
    re.compile(r"\*\*Checkpoint", re.IGNORECASE),
    re.compile(r"\bdata/claw\.db\b", re.IGNORECASE),
    re.compile(r"\bmsg_id[:= ]\s*\d{3,}", re.IGNORECASE),
)


def _brain_content_references_evidence(content: str) -> bool:
    if not content:
        return False
    return any(pattern.search(content) for pattern in _EVIDENCE_REFERENCE_PATTERNS)


PRE_HOOK_BLOCK_REPEATED_THRESHOLD = 5
PRE_HOOK_BLOCK_REPEATED_WINDOW_MINUTES = 10


def _parse_observe_timestamp(value: object) -> float | None:
    """observe_stream timestamps are 'YYYY-MM-DD HH:MM:SS' in UTC."""
    from datetime import datetime, timezone

    try:
        return (
            datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (TypeError, ValueError):
        return None


def _looks_like_pre_hook_block(content: str) -> bool:
    return content.strip().startswith(_PRE_HOOK_BLOCK_PREFIX)


def _parse_pre_hook_block(content: str) -> tuple[str, str] | None:
    match = _PRE_HOOK_BLOCK_RE.match(content.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _format_approval_pending(exc: ApprovalPending) -> str:
    """Convert a Tier 3 soft-block into Telegram-ready instructions for Hector."""
    lines = [
        "⚠️ Acción de Tier 3 detectada. Requiere aprobación de Hector.\n\n"
        f"Tool: `{exc.tool}`",
        f"Resumen: {exc.summary}",
    ]
    if exc.required_confirmation:
        lines.append(f"Risk code: `{exc.risk_code}`")
        if exc.sensitive_paths:
            lines.append("Rutas sensibles: " + ", ".join(f"`{path}`" for path in exc.sensitive_paths[:8]))
        if exc.diff_summary:
            lines.append(f"Diff resumido:\n```\n{redact_sensitive(exc.diff_summary, limit=1200)}\n```")
        lines.append(f"Confirmación exacta: `/approve {exc.approval_id} {exc.required_confirmation}`")
    else:
        lines.append(f"Comando: `/approve {exc.approval_id} {exc.token}`")
    return "\n\n".join(lines)


def _format_approval_pending_for_memory(exc: ApprovalPending) -> str:
    lines = [
        "Acción de Tier 3 pendiente de aprobación.",
        f"Tool: {exc.tool}",
        f"Resumen: {exc.summary}",
        f"Approval ID: {exc.approval_id}",
    ]
    if exc.required_confirmation:
        lines.append(f"Risk code: {exc.risk_code}")
        lines.append(f"Confirmación exacta requerida: {exc.required_confirmation}")
    else:
        lines.append("Token omitido en memoria.")
    return "\n".join(lines)


def _looks_like_pending_tool_approval_grant(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in {
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
        "dale",
        "ok",
        "si",
        "yes",
        "approved",
    }


def _looks_like_computer_approval_reject(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return any(
        phrase in normalized
        for phrase in (
            "aborta",
            "abortalo",
            "abortala",
            "cancela",
            "cancelalo",
            "cancelala",
            "no autorices",
            "no autorizo",
            "no lo hagas",
            "no la hagas",
        )
    )


# Explicit authorization verbs may appear inside a longer message
# ("Abre ChatGPT y crea la imagen. Te autorizo"), but only on word
# boundaries and never alongside a negation.
_COMPUTER_APPROVAL_VERB_RE = re.compile(r"\b(?:autorizo|apruebo)\b")
_COMPUTER_APPROVAL_NEGATION_RE = re.compile(r"\b(?:no|ni|nunca|jamas|tampoco)\b")


def _looks_like_computer_approval_grant(text: str) -> bool:
    if _looks_like_computer_approval_reject(text):
        return False
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if _looks_like_pending_tool_approval_grant(normalized):
        return True
    # Ambiguous short grants must be the whole message: substring matching let
    # unrelated messages ("consigue...", "continuamos", "dale una vuelta...")
    # approve pending Tier-3 desktop actions. Non-matches fall to the brain.
    if normalized in {
        "te autorizo",
        "lo autorizo",
        "la autorizo",
        "autorizo",
        "te apruebo",
        "lo apruebo",
        "la apruebo",
        "apruebo",
        "puedes continuar",
        "puedes hacerlo",
        "continua",
        "sigue",
        "hazlo",
        "dale",
    }:
        return True
    return bool(
        _COMPUTER_APPROVAL_VERB_RE.search(normalized)
        and not _COMPUTER_APPROVAL_NEGATION_RE.search(normalized)
    )


def _looks_like_task_completion_question(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not any(token in normalized for token in ("tarea", "task", "trabajo")):
        return False
    return any(
        token in normalized
        for token in (
            "completaste",
            "terminaste",
            "esta completa",
            "esta completada",
            "esta terminada",
            "quedo lista",
            "quedo listo",
            "completed",
            "done",
            "status",
            "estado",
        )
    )


_STATUS_CHANGE_PHRASE_RE = re.compile(
    r"(?:estatus|status|estado)\s+de\s+(?:los\s+)?(?:fixes|cambios)"
)


def _looks_like_change_status_question(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return _STATUS_CHANGE_PHRASE_RE.fullmatch(normalized) is not None


def _is_autonomous_task_start_ack(text: str) -> bool:
    return "Tarea autónoma iniciada:" in text


_TASK_TERMS = (
    "tarea", "task", "trabajo", "job", "cuaderno", "notebook",
    "pipeline", "run", "proceso",
)

_DIAGNOSTIC_TERMS = (
    "por que", "por qué", "porque", "que paso", "qué pasó",
    "fallo", "falló", "no pudiste", "no pudo", "no completaste",
    "no terminaste", "no se completo", "no quedo", "no quedó",
    "quedo pendiente", "bloqueada", "bloqueado", "error",
    "failed", "why failed",
)

_FOLLOWUP_TERMS = (
    "continua", "continúa", "retoma", "reanuda", "sigue",
    "hazlo", "crealo", "créalo", "lo que te pedi", "lo que te pedí",
    "que te pedi", "que te pedí", "la anterior", "el anterior",
    "eso mismo", "termina eso",
)


def _contains_command_term(normalized: str, terms: tuple[str, ...]) -> bool:
    for term in terms:
        normalized_term = _normalize_command_text(term)
        if " " in normalized_term:
            if normalized_term in normalized:
                return True
            continue
        if re.search(rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])", normalized):
            return True
    return False


def _looks_like_task_diagnostic_question(text: str) -> bool:
    normalized = _normalize_command_text(text)
    if not _contains_command_term(normalized, _TASK_TERMS):
        return False
    if not _contains_command_term(normalized, _DIAGNOSTIC_TERMS):
        return False
    broad_causal_only = _contains_command_term(normalized, ("por que", "porque"))
    if broad_causal_only and not _contains_command_term(
        normalized,
        (
            "fallo",
            "falló",
            "fallaste",
            "no pudiste",
            "no pudo",
            "no completaste",
            "no terminaste",
            "no se completo",
            "no quedo",
            "no quedó",
            "bloqueada",
            "bloqueado",
            "error",
            "failed",
        ),
    ):
        return False
    return True


def _looks_like_previous_task_followup(text: str) -> bool:
    normalized = _normalize_command_text(text)
    return _contains_command_term(normalized, _FOLLOWUP_TERMS)


def _looks_like_short_meta_question(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    if len(normalized) >= 120:
        return False
    is_question = "?" in text or normalized.startswith(("porque", "por que", "por qué", "que paso", "qué pasó"))
    if not is_question:
        return False
    if not _contains_command_term(normalized, ("tarea", "task", "job", "cuaderno", "notebook")):
        return False
    return _contains_command_term(normalized, ("completaste", "fallo", "falló", "no pudiste", "que paso"))


def _looks_like_operational_alert(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    return normalized.startswith("alerta operacional:") or normalized.startswith("operational alert:")


# Brain-bypass refactor: a literal task_id is the only natural-language
# token that may legitimately route to the task handler before the brain.
# Patterns observed in production: nlm-<16hex>, tg-<digits>:<kind>:<digits>,
# generic agent-<uuid> shapes, and bare 16+ hex slugs used for notebooks.
_LITERAL_TASK_ID_RE = re.compile(
    r"\b("
    r"nlm-[a-f0-9]{8,}"
    r"|tg-\d+:[a-z][\w-]*:\d+"
    r"|agent-[a-f0-9]{8,}"
    r"|task-[a-f0-9]{8,}"
    r")\b",
    re.IGNORECASE,
)


def _has_literal_task_id(text: str) -> bool:
    return _LITERAL_TASK_ID_RE.search(text) is not None


def _is_explicit_command(text: str) -> bool:
    """An explicit command is the only kind of message a heuristic pre-brain
    handler is allowed to capture by default. Two shapes qualify:
    a leading slash command (`/foo`), or a message that contains a literal
    task identifier the user is referring to. Everything else falls through
    to the brain so the model can reason about the request."""
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("/"):
        return True
    return _has_literal_task_id(stripped)


def _parse_operational_alert_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        normalized_key = _normalize_command_text(key).strip().replace(" ", "_")
        if normalized_key:
            fields[normalized_key] = value.strip()
    return fields


class BotService:
    def __init__(
        self,
        *,
        brain: BrainService,
        auto_research: AutoResearchAgentService,
        heartbeat: HeartbeatService,
        approvals: ApprovalManager,
        pull_requests: GitHubPullRequestService | None = None,
        allowed_user_id: str | None = None,
        pipeline: PipelineService | None = None,
        content_engine: ContentEngine | None = None,
        social_publisher: SocialPublisher | None = None,
        config: object | None = None,
        coordinator: CoordinatorService | None = None,
        browser: object | None = None,
        terminal_bridge: object | None = None,
        computer: object | None = None,
        browser_use: object | None = None,
        computer_gate: object | None = None,
        computer_client_factory: Callable[[], Any] | None = None,
        computer_model: str = _DEFAULT_COMPUTER_MODEL,
        computer_system_prompt: str | None = None,
        observe: object | None = None,
        task_ledger: object | None = None,
        job_service: JobService | None = None,
        model_registry: ModelRegistry | None = None,
        observation_window: object | None = None,
        stop_notifier: StopNotifier | None = None,
    ) -> None:
        self.brain = brain
        self.auto_research = auto_research
        self.heartbeat = heartbeat
        self.approvals = approvals
        self._stop_notifier = stop_notifier
        self._task_started_at: dict[str, float] = {}
        self.allowed_user_id = allowed_user_id
        self.pipeline = pipeline
        self.content_engine = content_engine
        self.social_publisher = social_publisher
        self.config = config
        self._terminal_handler = TerminalHandler(terminal_bridge=terminal_bridge)
        self.observe = observe
        self.task_ledger = task_ledger
        self.job_service: JobService | None = job_service
        self.model_registry = model_registry or ModelRegistry.default()
        self.observation_window = observation_window
        self.learning: Any | None = None
        self._wiki_handler = WikiHandler(memory=brain.memory)
        self._nlm_handler = NlmHandler(
            update_session_state=brain.memory.update_session_state,
            task_ledger=task_ledger,
            get_session_state=brain.memory.get_session_state,
            get_recent_messages=brain.memory.get_recent_messages,
        )
        self._capability_status: dict[str, dict[str, Any]] = {}
        self._runtime_probe: RuntimeAliveProbe | None = None
        self._execution_environment: ExecutionEnvironment | None = None
        self._browse_handler = BrowseHandler(
            config=config,
            observe=observe,
            get_learning=lambda: self.learning,
            get_browser=lambda: self.browser,
            get_managed_chrome=lambda: self.managed_chrome,
            wiki_ingest=lambda title, content, source_type: self._wiki_handler.maybe_ingest(title, content, source_type=source_type),
            capability_unavailable_message=self._capability_unavailable_message,
            update_session_state=brain.memory.update_session_state,
            get_session_state=brain.memory.get_session_state,
        )
        self._task_handler = TaskHandler(
            approvals=approvals,
            coordinator=coordinator,
            observe=observe,
            task_ledger=task_ledger,
            job_service=job_service,
            router=getattr(brain, "router", None),
            get_session_state=brain.memory.get_session_state,
            update_session_state=brain.memory.update_session_state,
            merge_active_object=brain.memory.merge_active_object,
            store_message=self._store_message_from_handler,
            workspace_root=getattr(config, "workspace_root", None),
            telemetry_root=getattr(config, "telemetry_root", None),
            max_autonomous_workers=getattr(config, "max_autonomous_workers", 4),
        )
        brain.delegation_handler_factory = self._delegation_handler_for_session
        self._state_handler = StateHandler(
            brain_memory=brain.memory,
            task_handler=self._task_handler,
            observe=observe,
        )
        self._idle_executor = IdleOwnershipExecutor(
            memory=brain.memory,
            task_ledger=task_ledger,
            job_service=job_service,
            task_handler=self._task_handler,
            observe=observe,
            telemetry_root=getattr(config, "telemetry_root", None),
        )
        self._chrome_handler = ChromeHandler(
            capability_check=self._capability_unavailable_message,
            remember_url=self._browse_handler.remember_recent_browse_url,
        )
        self._chrome_handler.browser = browser
        self._design_handler = DesignHandler(
            browser=browser,
            capability_check=self._capability_unavailable_message,
            get_managed_chrome=lambda: self.managed_chrome,
        )
        self._checkpoint_handler = CheckpointHandler(checkpoint=brain.checkpoint) if brain.checkpoint is not None else None
        self._computer_handler = ComputerHandler(
            computer=computer,
            browser_use=browser_use,
            computer_gate=computer_gate,
            computer_client_factory=computer_client_factory,
            computer_model=computer_model,
            computer_system_prompt=computer_system_prompt or _COMPUTER_SYSTEM_PROMPT,
            approvals=approvals,
            config=config,
            observe=observe,
            capability_check=self._capability_unavailable_message,
            brain_handle_message=lambda *args, **kwargs: self.brain.handle_message(*args, **kwargs),
            current_url_resolver=self._browse_handler.recent_browse_url,
            capability_status_updater=self.set_capability_status,
        )
        # Option (b), 2026-06-13: route delegated CDP/browser jobs to the
        # in-process browser executor (ComputerHandler -> BrowserUseService /
        # Playwright in the daemon venv) instead of the network-denied Codex
        # coordinator (whose --sandbox workspace-write blocks localhost:9250).
        self._task_handler.browser_executor = (
            self._computer_handler.run_delegated_browser_task
        )
        self._agent_handler = AgentHandler(
            auto_research=auto_research,
            pull_requests=pull_requests,
        )
        self._skill_threads: dict[str, threading.Thread] = {}
        self._skill_lock = threading.Lock()
        self._pre_state_commands = self._build_pre_state_commands()
        self._post_shortcut_commands = self._build_post_shortcut_commands()
        self._pre_brain_routes: list[Route] = self._build_pre_brain_routes()

    @property
    def terminal_bridge(self) -> object | None:
        return self._terminal_handler.terminal_bridge

    @terminal_bridge.setter
    def terminal_bridge(self, value: object | None) -> None:
        self._terminal_handler.terminal_bridge = value

    @property
    def computer(self) -> object | None:
        return self._computer_handler.computer

    @computer.setter
    def computer(self, value: object | None) -> None:
        self._computer_handler.computer = value

    @property
    def browser_use(self) -> object | None:
        return self._computer_handler.browser_use

    @browser_use.setter
    def browser_use(self, value: object | None) -> None:
        self._computer_handler.browser_use = value

    @property
    def computer_gate(self) -> object | None:
        return self._computer_handler.computer_gate

    @computer_gate.setter
    def computer_gate(self, value: object | None) -> None:
        self._computer_handler.computer_gate = value

    @property
    def computer_client_factory(self) -> Callable[[], Any] | None:
        return self._computer_handler.computer_client_factory

    @computer_client_factory.setter
    def computer_client_factory(self, value: Callable[[], Any] | None) -> None:
        self._computer_handler.computer_client_factory = value

    @property
    def wiki(self) -> object | None:
        return self._wiki_handler.wiki

    @wiki.setter
    def wiki(self, value: object | None) -> None:
        self._wiki_handler.wiki = value

    @property
    def managed_chrome(self) -> object | None:
        return self._chrome_handler.managed_chrome

    @managed_chrome.setter
    def managed_chrome(self, value: object | None) -> None:
        self._chrome_handler.managed_chrome = value

    @property
    def browser(self) -> object | None:
        return self._chrome_handler.browser

    @browser.setter
    def browser(self, value: object | None) -> None:
        self._chrome_handler.browser = value

    @property
    def notebooklm(self) -> object | None:
        return self._nlm_handler.notebooklm

    @property
    def pull_requests(self) -> object | None:
        return self._agent_handler.pull_requests

    @pull_requests.setter
    def pull_requests(self, value: object | None) -> None:
        self._agent_handler.pull_requests = value

    @notebooklm.setter
    def notebooklm(self, value: object | None) -> None:
        self._nlm_handler.notebooklm = value

    @property
    def coordinator(self) -> object | None:
        return self._task_handler.coordinator

    @coordinator.setter
    def coordinator(self, value: object | None) -> None:
        self._task_handler.coordinator = value

    def resume_interrupted_tasks(self) -> int:
        return self._task_handler.resume_interrupted_autonomous_tasks()

    def set_capability_status(self, name: str, *, available: bool, reason: str | None = None) -> None:
        self._capability_status[name] = {"available": available, "reason": reason or ""}

    def _capability_unavailable_message(self, name: str, fallback: str) -> str | None:
        status = self._capability_status.get(name)
        if status is None or status.get("available", True):
            return None
        base = _CAPABILITY_MESSAGES.get(name, fallback)
        reason = str(status.get("reason", "")).strip()
        return f"{base} {reason}".strip()

    def _capability_available(self, name: str) -> bool:
        status = self._capability_status.get(name)
        if status is None:
            return True
        return bool(status.get("available", True))

    def _scheduled_skill_available(self, skill: str) -> bool:
        scheduled = getattr(self.config, "scheduled_sub_agents", None) or []
        for entry in scheduled:
            if getattr(entry, "skill", None) == skill:
                return True
        return False

    def _scheduled_skill_lane(self, skill: str) -> str:
        scheduled = getattr(self.config, "scheduled_sub_agents", None) or []
        for entry in scheduled:
            if getattr(entry, "skill", None) == skill:
                lane = str(getattr(entry, "lane", "") or "").strip()
                return lane or "worker"
        return "worker"

    def _runtime_alive(self) -> bool:
        if self._runtime_probe is None:
            port = int(getattr(self.config, "web_chat_port", 8765))
            self._runtime_probe = RuntimeAliveProbe(port=port)
        return self._runtime_probe.is_alive()

    def _current_environment(self) -> ExecutionEnvironment:
        if self._execution_environment is None:
            workspace_root = getattr(self.config, "workspace_root", None)
            self._execution_environment = detect_execution_environment(
                workspace_root=str(workspace_root) if workspace_root else None
            )
        return self._execution_environment

    def _maybe_handle_capability_route(
        self, text: str, *, session_id: str
    ) -> str | None:
        # Guard: slash commands NO se interceptan; van a sus handlers existentes.
        if not text or text.lstrip().startswith("/"):
            return None
        intent = classify_autonomy_intent(text)
        if intent.task_kind == "unknown":
            return None
        # Skip notebooklm here — el NlmHandler downstream tiene su propio resolver.
        if intent.task_kind.startswith("notebooklm"):
            return None
        # Acciones críticas (publish/merge/deploy) las maneja la autonomy policy
        # existente en task_handler con su propio mensaje. No interceptamos.
        from claw_v2.capability_router import CRITICAL_TASK_KINDS

        if intent.task_kind in CRITICAL_TASK_KINDS:
            return None
        env = self._current_environment()
        route = route_request(
            intent,
            skill_available=self._scheduled_skill_available,
            runtime_alive=self._runtime_alive(),
            chrome_cdp=self._capability_available("chrome_cdp"),
            web_available=self._capability_available("browser_use"),
            current_environment=env.kind,
        )
        self._emit_capability_route_event(route, session_id=session_id)
        if route.route == "runtime_handoff":
            return self._dispatch_runtime_handoff(route, session_id=session_id)
        if route.route == "approval_required":
            return route.next_action
        if route.route == "blocked":
            return route.next_action
        if route.route == "skill":
            return self._start_skill_task(route, text, session_id=session_id)
        if route.route == "runtime":
            return f"Ejecutando {route.task_kind} vía runtime. {route.next_action}"
        if route.route == "cdp":
            return None  # Existing chrome handler / browse handler downstream picks up.
        if route.route == "local":
            return None  # Existing handlers pick up.
        return None

    def _start_skill_task(
        self,
        route: CapabilityRoute,
        user_text: str,
        *,
        session_id: str,
    ) -> str:
        agent_name = route.agent or "alma"
        skill_name = route.skill or ""
        sub_agents = getattr(self, "sub_agents", None)
        if sub_agents is None or not skill_name:
            return f"Skill no disponible para `{route.task_kind}`."
        try:
            agent_def = sub_agents.get_agent(agent_name)
        except Exception:
            agent_def = None
        if agent_def is None:
            return f"Sub-agente `{agent_name}` no disponible para `{skill_name}`."
        if skill_name not in getattr(agent_def, "skills", {}):
            return f"Skill `{skill_name}` no disponible en `{agent_name}`."

        task_id = f"{session_id}:skill:{time.time_ns()}"
        self._task_started_at[task_id] = time.time()
        lane = self._scheduled_skill_lane(skill_name)
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["active_task"] = {
            "task_id": task_id,
            "objective": user_text,
            "task_kind": route.task_kind,
            "route": "skill",
            "agent": agent_name,
            "skill": skill_name,
            "status": "running",
            "started_at": time.time(),
        }
        self.brain.memory.update_session_state(
            session_id,
            mode="research",
            verification_status="running",
            pending_action="",
            last_checkpoint={
                "summary": f"Skill task started: {skill_name}",
                "verification_status": "running",
                "task_id": task_id,
            },
            active_object=active_object,
        )
        if self.task_ledger is not None:
            self.task_ledger.create(
                task_id=task_id,
                session_id=session_id,
                objective=user_text,
                mode="research",
                runtime="sub_agent_skill",
                provider=getattr(agent_def, "provider", None),
                model=getattr(agent_def, "model", None),
                status="running",
                route=active_object.get("last_channel_route") if isinstance(active_object.get("last_channel_route"), dict) else {},
                metadata={
                    "agent": agent_name,
                    "skill": skill_name,
                    "task_kind": route.task_kind,
                    "autonomous": True,
                },
                artifacts={
                    "skill": skill_name,
                    "agent": agent_name,
                    "dispatch_reason": route.reason,
                },
            )
        self._emit_skill_task_event(
            "skill_task_started",
            task_id=task_id,
            session_id=session_id,
            user_text=user_text,
            agent=agent_name,
            skill=skill_name,
            task_kind=route.task_kind,
        )
        thread = threading.Thread(
            target=self._run_skill_task,
            args=(task_id, session_id, user_text, agent_name, skill_name, lane, route.task_kind),
            daemon=True,
            name=f"skill-task-{task_id[-8:]}",
        )
        with self._skill_lock:
            self._skill_threads[task_id] = thread
        thread.start()
        return (
            f"Voy con eso vía skill `{skill_name}` ({agent_name}).\n\n"
            f"Tarea de skill iniciada: `{task_id}`\n"
            "Te aviso por Telegram cuando cierre con resultado."
        )

    def _run_skill_task(
        self,
        task_id: str,
        session_id: str,
        user_text: str,
        agent_name: str,
        skill_name: str,
        lane: str,
        task_kind: str,
    ) -> None:
        try:
            sub_agents = getattr(self, "sub_agents", None)
            if sub_agents is None:
                raise RuntimeError("sub-agent service unavailable")
            context = f"User request from Telegram:\n{user_text}"
            result = sub_agents.run_skill(agent_name, skill_name, context=context, lane=lane)
            if _looks_like_pre_hook_block(result):
                result = self._maybe_augment_pre_hook_block(result)
                self._mark_skill_task_failed(
                    task_id=task_id,
                    session_id=session_id,
                    user_text=user_text,
                    agent=agent_name,
                    skill=skill_name,
                    task_kind=task_kind,
                    error=result,
                    verification_status="blocked",
                )
                return
            summary = result.strip().splitlines()[0][:500] if result.strip() else f"Skill {skill_name} completed"
            self._mark_skill_task_succeeded(
                task_id=task_id,
                session_id=session_id,
                user_text=user_text,
                agent=agent_name,
                skill=skill_name,
                task_kind=task_kind,
                result=result,
                summary=summary,
            )
        except Exception as exc:
            self._mark_skill_task_failed(
                task_id=task_id,
                session_id=session_id,
                user_text=user_text,
                agent=agent_name,
                skill=skill_name,
                task_kind=task_kind,
                error=f"{type(exc).__name__}: {exc}",
                verification_status="failed",
            )
        finally:
            with self._skill_lock:
                self._skill_threads.pop(task_id, None)

    def wait_for_skill_task(self, task_id: str, timeout: float = 5.0) -> bool:
        with self._skill_lock:
            thread = self._skill_threads.get(task_id)
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _mark_skill_task_succeeded(
        self,
        *,
        task_id: str,
        session_id: str,
        user_text: str,
        agent: str,
        skill: str,
        task_kind: str,
        result: str,
        summary: str,
    ) -> None:
        self._update_skill_active_task(
            session_id=session_id,
            task_id=task_id,
            status="completed",
            checkpoint_summary=summary,
            verification_status="passed",
        )
        artifacts = {
            "skill": skill,
            "agent": agent,
            "task_kind": task_kind,
            "skill_result": result[:20000],
        }
        if self.task_ledger is not None:
            self.task_ledger.mark_terminal(
                task_id,
                status="succeeded",
                summary=summary,
                verification_status="passed",
                artifacts=artifacts,
            )
        self._emit_skill_task_event(
            "sub_agent_skill",
            task_id=task_id,
            session_id=session_id,
            user_text=user_text,
            agent=agent,
            skill=skill,
            task_kind=task_kind,
            result=result,
        )
        self._emit_skill_task_event(
            "autonomous_task_completed",
            task_id=task_id,
            session_id=session_id,
            user_text=user_text,
            agent=agent,
            skill=skill,
            task_kind=task_kind,
            response=result,
            verification_status="passed",
            terminal_status="succeeded",
        )
        self._notify_stop(
            task_id=task_id,
            kind=task_kind or skill or "autonomous_task",
            status="succeeded",
            summary=summary,
        )

    def _mark_skill_task_failed(
        self,
        *,
        task_id: str,
        session_id: str,
        user_text: str,
        agent: str,
        skill: str,
        task_kind: str,
        error: str,
        verification_status: str,
    ) -> None:
        self._update_skill_active_task(
            session_id=session_id,
            task_id=task_id,
            status="failed",
            checkpoint_summary=f"Skill task failed: {error[:300]}",
            verification_status=verification_status,
            error=error,
        )
        if self.task_ledger is not None:
            self.task_ledger.mark_terminal(
                task_id,
                status="failed",
                summary=f"Skill task failed: {skill}",
                error=error,
                verification_status=verification_status,
                artifacts={
                    "skill": skill,
                    "agent": agent,
                    "task_kind": task_kind,
                    "error": error[:2000],
                },
            )
        self._emit_skill_task_event(
            "autonomous_task_failed",
            task_id=task_id,
            session_id=session_id,
            user_text=user_text,
            agent=agent,
            skill=skill,
            task_kind=task_kind,
            response=error,
            error=error,
            verification_status=verification_status,
        )
        self._notify_stop(
            task_id=task_id,
            kind=task_kind or skill or "autonomous_task",
            status="failed",
            summary=error,
        )

    def _notify_stop(
        self,
        *,
        task_id: str,
        kind: str,
        status: str,
        summary: str,
    ) -> None:
        """Push a one-line stop notification to Telegram for autonomous tasks.

        No-op when stop_notifier is unconfigured. Computes duration from
        _task_started_at; falls back to force=True when start time is unknown
        so autonomous tasks always notify even if we did not capture start.
        Errors are swallowed by the notifier itself.
        """
        notifier = self._stop_notifier
        if notifier is None:
            return
        started = self._task_started_at.pop(task_id, None)
        duration = (time.time() - started) if started else None
        try:
            notifier.notify_completion(
                task_id=task_id,
                kind=kind,
                status=status,
                summary=summary,
                duration_sec=duration,
                force=duration is None,
            )
        except Exception:
            logger.exception("stop_notifier dispatch failed task_id=%s", task_id)

    def _update_skill_active_task(
        self,
        *,
        session_id: str,
        task_id: str,
        status: str,
        checkpoint_summary: str,
        verification_status: str,
        error: str = "",
    ) -> None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_task = dict(active_object.get("active_task") or {})
        if active_task.get("task_id") == task_id:
            active_task["status"] = status
            active_task["updated_at"] = time.time()
            if status in {"completed", "failed"}:
                active_task["completed_at"] = time.time()
            if error:
                active_task["error"] = error
            active_object["active_task"] = active_task
        self.brain.memory.update_session_state(
            session_id,
            verification_status=verification_status,
            pending_action="",
            last_checkpoint={
                "summary": checkpoint_summary,
                "verification_status": verification_status,
                "task_id": task_id,
                **({"error": error} if error else {}),
            },
            active_object=active_object,
        )

    def _semantic_prebrain_routes_enabled(self) -> bool:
        """Brain-bypass refactor (commit #5): the heuristic semantic routers
        (task_intent, capability_route, nlm natural-language interception)
        are off by default. Operators can re-enable them by setting
        CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES=1, e.g. for canary debugging.

        Deterministic routers (operational_alert, operational_status, slash
        commands, literal-task-id shortcuts) are NOT gated by this flag —
        they have non-overlapping signal and were not implicated in the
        brain-bypass false positives.
        """
        return os.getenv("CLAW_ENABLE_SEMANTIC_PREBRAIN_ROUTES", "0") == "1"

    def _capability_route_allowed(self, text: str) -> bool:
        if self._semantic_prebrain_routes_enabled() or _is_explicit_command(text):
            return True
        intent = classify_autonomy_intent(text)
        return intent.task_kind in {"ai_news_brief", "x_trends"}

    def _emit_dispatch_decision(
        self,
        *,
        route: str,
        session_id: str,
        text: str,
        captured: bool,
        handler: str | None = None,
        reason: str | None = None,
        matched_pattern: str | None = None,
    ) -> None:
        # Telemetry for the brain-bypass refactor: emit one event per
        # pre-brain handler decision so we can audit which route fires
        # for which message and detect false positives without guessing.
        #
        # Schema (commit #4 of the brain-bypass refactor):
        #   handler:     name of the pre-brain handler that ran
        #   route:       "intercepted" | "fall_through" | "explicit_command"
        #                (legacy callers pre-#4 still pass the handler name as
        #                `route` plus a `captured` bool; we preserve those
        #                fields for back-compat and synthesize the new ones.)
        #   reason:      short tag explaining the decision
        #   text_len:    full length of the message (uncapped)
        #   text_preview: first 80 chars only — never the full message
        if self.observe is None:
            return
        # Back-compat: when callers still use the legacy positional shape
        # (route=<handler_name>, captured=bool), promote it to the new schema
        # so the audit stream is uniform.
        legacy_route_values = {"intercepted", "fall_through", "explicit_command"}
        if handler is None and route not in legacy_route_values:
            handler = route
            route = "intercepted" if captured else "fall_through"
        if reason is None:
            reason = f"{handler or 'unknown'}_{'matched' if captured else 'fall_through'}"
        # matched_pattern: canonical label of the sub-pattern that fired
        # (e.g. "task_intent.resume_previous_es", "shortcut.url_extract",
        # "proceed_class.pending_action"). Default: handler name when matched,
        # None on fall_through. Lets `claw think tail --type dispatch_decision`
        # show *why* it matched, not just *which* handler ran.
        if matched_pattern is None and captured and handler:
            matched_pattern = handler
        try:
            self.observe.emit(
                "dispatch_decision",
                payload={
                    "session_id": session_id,
                    "handler": handler,
                    "route": route,
                    "reason": reason,
                    "captured": captured,
                    "matched_pattern": matched_pattern,
                    "text_preview": text[:80],
                    "text_len": len(text),
                    # legacy alias kept so existing dashboards keep parsing
                    "text_length": len(text),
                },
            )
        except Exception:
            logger.exception(
                "failed to emit dispatch_decision handler=%s route=%s",
                handler,
                route,
            )

    def _emit_semantic_turn_trace(
        self,
        *,
        session_id: str,
        text: str,
        semantic_turn: SemanticTurn,
        state_sources_checked: list[str],
        approval_scope_match: str,
        decision: str,
        output_kind: str,
        response_text: str | None = None,
    ) -> None:
        if self.observe is None:
            return
        renderer = NaturalLanguageRenderer(mode="normal")
        leaked = renderer.leaked_internal_labels(response_text or "")
        try:
            self.observe.emit(
                "semantic_turn_trace",
                payload={
                    "session_id": session_id,
                    "semantic_intent": semantic_turn.intent,
                    "semantic_confidence": semantic_turn.confidence,
                    "clear_goal": semantic_turn.clear_goal,
                    "state_sources_checked": list(state_sources_checked),
                    "approval_scope_match": approval_scope_match,
                    "decision": decision,
                    "output_kind": output_kind,
                    "leaked_internal_labels": leaked,
                    "text_preview": text[:80],
                    "text_len": len(text),
                    "reasons": list(semantic_turn.reasons),
                },
            )
        except Exception:
            logger.debug("failed to emit semantic_turn_trace", exc_info=True)

    @staticmethod
    def _semantic_approval_scope_default(semantic_turn: SemanticTurn) -> str:
        if semantic_turn.intent == "new_task":
            return "skipped_new_task"
        if semantic_turn.intent in {"approval_response", "continue_active_mission"}:
            return "deferred_until_state_scope"
        return "not_checked"

    def _emit_skill_task_event(
        self,
        event_type: str,
        *,
        task_id: str,
        session_id: str,
        user_text: str,
        agent: str,
        skill: str,
        task_kind: str,
        **extra: Any,
    ) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(
                event_type,
                payload={
                    "task_id": task_id,
                    "session_id": session_id,
                    "objective": user_text,
                    "agent": agent,
                    "skill": skill,
                    "task_kind": task_kind,
                    **extra,
                },
            )
        except Exception:
            logger.exception("failed to emit %s", event_type)

    def _dispatch_runtime_handoff(
        self, route: CapabilityRoute, *, session_id: str
    ) -> str:
        workspace_root = getattr(self.config, "workspace_root", None)
        queue_root = (
            Path(workspace_root) / "runtime_handoffs"
            if workspace_root is not None
            else Path("data") / "runtime_handoffs"
        )
        port = int(getattr(self.config, "web_chat_port", 8765))
        try:
            handoff = create_runtime_handoff(
                goal=route.task_kind,
                session_id=session_id,
                required_capabilities=list(route.required_capabilities),
                queue_root=queue_root,
                gateway_port=port,
            )
        except Exception:
            logger.exception("failed to create runtime handoff")
            return (
                "Esta sesión no puede ejecutar la misión y no pude crear un "
                "handoff. Reinicia Claw producción con: "
                "`cd ~/Projects/Dr.-strange && ./scripts/restart.sh`"
            )
        if self.observe is not None:
            try:
                self.observe.emit(
                    "runtime_handoff_created",
                    payload={
                        "handoff_id": handoff.handoff_id,
                        "session_id": session_id,
                        "task_kind": route.task_kind,
                        "dispatch_method": handoff.dispatch_method,
                        "queue_path": handoff.queue_path,
                        "status": handoff.status,
                    },
                )
            except Exception:
                logger.exception("failed to emit runtime_handoff_created")
        return format_handoff_message(handoff)

    def _emit_capability_route_event(
        self, route: CapabilityRoute, *, session_id: str
    ) -> None:
        if self.observe is None:
            return
        event_type = (
            "capability_route_blocked"
            if route.route == "blocked"
            else "capability_route_selected"
        )
        try:
            self.observe.emit(
                event_type,
                payload={
                    "session_id": session_id,
                    "route": route.route,
                    "reason": route.reason,
                    "task_kind": route.task_kind,
                    "required_capabilities": list(route.required_capabilities),
                    "available_capabilities": list(route.available_capabilities),
                    "missing_capabilities": list(route.missing_capabilities),
                    "skill": route.skill,
                    "ask_user": route.ask_user,
                },
            )
        except Exception:
            logger.exception("failed to emit capability route event")

    def _memory_compaction_enabled(self) -> bool:
        return bool(getattr(self.config, "use_compaction", False))

    def _store_memory_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        *,
        assistant_limit: int,
    ) -> None:
        self.brain.memory.store_message(session_id, "user", user_text)
        self.brain.memory.store_message(
            session_id,
            "assistant",
            assistant_text[:assistant_limit],
            compact=self._memory_compaction_enabled(),
        )

    def _store_message_from_handler(self, session_id: str, role: str, content: str) -> None:
        self.brain.memory.store_message(
            session_id,
            role,
            content,
            compact=role == "assistant" and self._memory_compaction_enabled(),
        )

    def _delegation_handler_for_session(
        self, session_id: str
    ) -> Callable[[dict[str, Any]], dict[str, Any]]:
        """Factory for the brain's `delegate_task` tool handler.

        Returns a plain closure (NOT a bound method): the router fallback path
        rebuilds LLMRequest via asdict()/deepcopy, and deep-copying a bound
        method would drag BotService (locks, sockets) with it.
        """
        task_handler = self._task_handler
        observe = self.observe

        def _handle(args: dict[str, Any]) -> dict[str, Any]:
            objective = str(args.get("objective") or "").strip()
            mode = args.get("mode")
            if mode not in {"coding", "research", "ops", "publish", "browse"}:
                mode = _infer_session_mode(objective)
            if mode == "chat":
                mode = "ops"
            reason = str(args.get("reason") or "")[:300]
            if observe is not None:
                try:
                    observe.emit(
                        "brain_delegation_requested",
                        payload={
                            "session_id": session_id,
                            "mode": mode,
                            "reason": reason,
                            "objective_preview": objective[:200],
                        },
                    )
                except Exception:
                    logger.debug("brain_delegation_requested emit failed", exc_info=True)
            ack = task_handler.start_autonomous_task(
                session_id,
                objective,
                mode=mode,
                source_text=objective,
                delegation_metadata={"origin": "brain_delegate_tool", "reason": reason},
            )
            return {"ok": True, "ack": ack, "mode": mode}

        return _handle

    def _emit_internal_chat_suppressed(self, session_id: str, *, reason: str, original: str, sanitized: str) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(
                "internal_message_suppressed_from_chat",
                payload={
                    "session_id": session_id,
                    "reason": reason,
                    "original_length": len(original),
                    "sanitized_length": len(sanitized),
                },
            )
        except Exception:
            logger.debug("failed to emit internal_message_suppressed_from_chat", exc_info=True)

    def _sanitize_visible_chat_response(self, session_id: str, content: str) -> str:
        sanitized = _sanitize_chat_response(content)
        if sanitized != content:
            self._emit_internal_chat_suppressed(
                session_id,
                reason="internal_runtime_detail",
                original=content,
                sanitized=sanitized,
            )
        return sanitized

    def _final_render(self, session_id: str, content: str) -> str:
        """Single funnel for user-visible text on the Telegram brain path.

        Contract — keep narrow:
        1. Apply ``NaturalLanguageRenderer(mode="normal").render`` to drop
           internal labels (approval_id, /task_*, explicit_blocker, …) and
           replace internal tokens with natural Spanish copy.
        2. Apply ``_sanitize_visible_chat_response`` to redact runtime IDs
           and local paths.

        Forbidden inside this helper (enforced by tests):
        - Touching ``_record_evidence_gate_explicit_blocker`` or any
          evidence-gate / task ledger logic.
        - Reading ``current_meta_introspection_kind`` to alter behaviour.
        - Mutating session_state, observe, or approvals state.

        Both inner ops are idempotent regex transforms: ``_final_render(_final_render(x)) == _final_render(x)``
        is exercised by ``tests/test_final_render_idempotency.py``.

        Placement invariant when applied to the brain path: must run INSIDE
        ``_brain_text_response`` so it stays inside the
        ``with meta_introspection_context(...)`` set by the
        ``meta_introspection_guard``. See INTERNAL_WIRING.md §1
        ``final_render_brain_path_inside_meta_context``.
        """
        if not content:
            return content
        rendered = NaturalLanguageRenderer(mode="normal").render(content)
        return self._sanitize_visible_chat_response(session_id, rendered)

    def _enforce_background_monitor_contract(
        self,
        *,
        session_id: str,
        user_text: str,
        content: str,
        raw_content: str,
    ) -> str:
        """Prevent false claims that long-running work is being monitored.

        A visible promise to keep working after the turn must be backed by a
        durable job/task or a verified launchd agent. Brain tool-use rows are
        terminal audit records, not execution handles, so they do not satisfy
        this contract.
        """
        if not self._claims_background_monitor(content):
            return content
        response_text = f"{content}\n{raw_content}"
        evidence = self._background_monitor_evidence(session_id, response_text)
        if not evidence.get("durable"):
            registered = self._maybe_register_notebooklm_background_monitor(
                session_id=session_id,
                response_text=response_text,
            )
            if registered:
                evidence = self._background_monitor_evidence(session_id, response_text)
        if not evidence.get("durable") and self._claims_durable_dispatch(content):
            # Causa 2: the brain narrated a durable dispatch but never called a
            # primitive that creates the job/task, so there is nothing for the
            # evidence probe to find and the reply would be nuked while the work
            # never runs. Make the promise true instead of only blocking it:
            # start a real autonomous coordinator task for this turn, then
            # re-check evidence so the narration is backed by running work.
            promoted = self._maybe_promote_durable_dispatch_to_task(
                session_id=session_id,
                user_text=user_text,
                response_text=response_text,
            )
            if promoted:
                evidence = self._background_monitor_evidence(session_id, response_text)
        if evidence.get("durable"):
            return content
        durable_dispatch_claim = self._claims_durable_dispatch(content)
        if durable_dispatch_claim:
            return self._background_monitor_replacement(
                session_id=session_id,
                user_text=user_text,
                reason=evidence.get("reason") or "no_durable_monitor",
            )
        stripped = self._strip_unsupported_background_monitor_claims(content)
        if stripped != content and stripped and not self._claims_background_monitor(stripped):
            self._emit_safe(
                "background_monitor_claim_stripped",
                {
                    "session_id": session_id,
                    "user_text_preview": user_text[:120],
                    "reason": evidence.get("reason") or "no_durable_monitor",
                },
            )
            return stripped
        if stripped == content and not durable_dispatch_claim:
            # No single line carried the monitor claim: the only trigger was the
            # cross-line DOTALL promise pattern matching over otherwise-unrelated
            # lines. Replacing the whole reply with the defensive template here is
            # the false-positive class Hector flagged ("Listo ya quedo" nuked a
            # useful confirmation). When we cannot isolate the offending claim and
            # there is no explicit durable-dispatch claim, keep the original reply.
            self._emit_safe(
                "background_monitor_claim_kept_unisolated",
                {
                    "session_id": session_id,
                    "user_text_preview": user_text[:120],
                    "reason": evidence.get("reason") or "no_durable_monitor",
                },
            )
            return content
        return self._background_monitor_replacement(
            session_id=session_id,
            user_text=user_text,
            reason=evidence.get("reason") or "no_durable_monitor",
        )

    def _background_monitor_replacement(
        self,
        *,
        session_id: str,
        user_text: str,
        reason: str,
    ) -> str:
        replacement = (
            "Preparé o disparé parte de la acción, pero no quedó un monitor "
            "durable registrado. No puedo prometer aviso automático ni trabajo "
            "en background. Para dejarlo corriendo hace falta crear un job "
            "durable o reanudarlo manualmente."
        )
        self._emit_safe(
            "background_monitor_claim_rejected",
            {
                "session_id": session_id,
                "user_text_preview": user_text[:120],
                "reason": reason,
            },
        )
        return replacement

    def _maybe_promote_durable_dispatch_to_task(
        self,
        *,
        session_id: str,
        user_text: str,
        response_text: str,
    ) -> bool:
        """Turn an unbacked durable-dispatch claim into a real coordinator task.

        Causa 2 of the background-monitor bug: the brain narrates that it will
        dispatch durable background work but never calls a primitive that
        creates the job/task, so ``_background_monitor_evidence`` finds nothing
        and the reply gets replaced with the defensive template while the work
        never runs. Rather than only blocking, resolve the actionable objective
        for this turn and start a real autonomous coordinator task so the
        promise becomes true and the gate finds evidence on the re-check.

        Returns True only when a coordinator task was actually started. The
        coordinator still enforces its own per-tier approval gates, so this
        cannot escalate privilege; it only makes a narrated promise durable.
        """
        handler = getattr(self, "_task_handler", None)
        if handler is None or getattr(handler, "coordinator", None) is None:
            return False
        starter = getattr(handler, "start_autonomous_task", None)
        if not callable(starter):
            return False
        try:
            state = self.brain.memory.get_session_state(session_id)
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}
        objective, source = self._resolve_actionable_task_objective(user_text, state=state)
        if not objective:
            # Causa 2 option 1: the standard resolver cannot pin an objective for
            # terse option picks ("A+B", "Ambos"), which are exactly the replies
            # that triggered the loop. Reuse the reply_context the user replied
            # to — the concrete plan the brain just narrated — as the objective.
            objective = self._durable_dispatch_fallback_objective(state)
            source = "reply_context_fallback"
        if not objective:
            self._emit_safe(
                "background_monitor_dispatch_promotion_skipped",
                {
                    "session_id": session_id,
                    "user_text_preview": user_text[:120],
                    "reason": "no_actionable_objective",
                },
            )
            return False
        try:
            result = starter(session_id, objective, source_text=user_text or objective)
        except Exception as exc:
            self._emit_safe(
                "background_monitor_dispatch_promotion_failed",
                {
                    "session_id": session_id,
                    "user_text_preview": user_text[:120],
                    "error": str(exc)[:300],
                },
            )
            return False
        started = isinstance(result, str) and "Tarea autónoma iniciada" in result
        if not started:
            self._emit_safe(
                "background_monitor_dispatch_promotion_skipped",
                {
                    "session_id": session_id,
                    "user_text_preview": user_text[:120],
                    "reason": "coordinator_declined",
                    "detail": (result or "")[:200] if isinstance(result, str) else "",
                },
            )
            return False
        self._emit_safe(
            "background_monitor_dispatch_promoted",
            {
                "session_id": session_id,
                "user_text_preview": user_text[:120],
                "objective_source": source,
            },
        )
        return True

    @staticmethod
    def _durable_dispatch_fallback_objective(state: dict[str, Any]) -> str:
        """Best-effort objective for a durable-dispatch promise the actionable
        resolver could not pin down.

        Causa 2 option 1: terse picks like "A+B"/"Ambos" do not classify as a
        direct task or a followup, so the standard resolver returns nothing.
        Reuse the reply_context the user was replying to — that text is the
        concrete plan the brain narrated this turn — so the promise can become a
        real coordinator task instead of falling to the defensive template.
        """
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return ""
        reply_context = active_object.get("reply_context") or {}
        if not isinstance(reply_context, dict) or not reply_context_fresh(reply_context):
            return ""
        text = str(reply_context.get("text") or "").strip()
        if len(text) < 12:
            return ""
        return text[:500]

    def _strip_unsupported_background_monitor_claims(self, content: str) -> str:
        lines = str(content or "").splitlines()
        kept: list[str] = []
        stripped_any = False
        for line in lines:
            if self._claims_background_monitor(line):
                stripped_any = True
                continue
            kept.append(line)
        if not stripped_any:
            return content
        collapsed: list[str] = []
        previous_blank = False
        for line in kept:
            blank = not line.strip()
            if blank and previous_blank:
                continue
            collapsed.append(line)
            previous_blank = blank
        return "\n".join(collapsed).strip()

    def _maybe_register_notebooklm_background_monitor(
        self,
        *,
        session_id: str,
        response_text: str,
    ) -> bool:
        notebook_id = self._extract_notebooklm_id(response_text)
        outputs = self._notebooklm_monitor_outputs(response_text)
        if not notebook_id or not outputs:
            return False
        nlm_handler = getattr(self, "_nlm_handler", None)
        notebooklm = getattr(nlm_handler, "notebooklm", None)
        starter = getattr(notebooklm, "start_orchestration", None)
        if not callable(starter):
            return False
        try:
            starter(notebook_id, session_id=session_id, outputs=outputs)
        except Exception as exc:
            self._emit_safe(
                "background_monitor_auto_register_failed",
                {
                    "session_id": session_id,
                    "notebook_id": notebook_id[:8],
                    "outputs": list(outputs),
                    "error": str(exc)[:300],
                },
            )
            return False
        self._emit_safe(
            "background_monitor_auto_registered",
            {
                "session_id": session_id,
                "notebook_id": notebook_id[:8],
                "outputs": list(outputs),
                "kind": "notebooklm.orchestrate",
            },
        )
        return True

    @staticmethod
    def _extract_notebooklm_id(text: str) -> str | None:
        match = _NOTEBOOKLM_UUID_RE.search(text or "")
        return match.group(0) if match else None

    @staticmethod
    def _notebooklm_monitor_outputs(text: str) -> tuple[str, ...]:
        normalized = str(text or "").lower()
        if not any(
            marker in normalized
            for marker in (
                "notebooklm",
                "cuaderno",
                "notebook",
                "resumen de video",
                "resumen en video",
                "resumen de audio",
                "resumen en audio",
            )
        ):
            return ()
        outputs: list[str] = []
        if any(
            marker in normalized
            for marker in (
                "video overview",
                "resumen de video",
                "resumen en video",
                "generando video",
                "generating video",
            )
        ):
            outputs.append("video")
        if any(
            marker in normalized
            for marker in (
                "resumen de audio",
                "resumen en audio",
                "podcast",
            )
        ):
            outputs.append("podcast")
        if any(
            marker in normalized
            for marker in (
                "generando informe",
                "informe blog",
                "blog post",
                "entrada de blog",
                "publicación de blog",
                "publicacion de blog",
            )
        ):
            outputs.append("blog")
        return tuple(dict.fromkeys(outputs))

    @staticmethod
    def _claims_background_monitor(text: str) -> bool:
        if not text:
            return False
        return any(pattern.search(text) for pattern in _BACKGROUND_MONITOR_PROMISE_PATTERNS)

    @staticmethod
    def _claims_durable_dispatch(text: str) -> bool:
        if not text:
            return False
        return any(pattern.search(text) for pattern in _DURABLE_DISPATCH_CLAIM_PATTERNS)

    def _background_monitor_evidence(self, session_id: str, response_text: str) -> dict[str, Any]:
        active_task = self._active_notifying_task(session_id)
        if active_task is not None:
            return {
                "durable": True,
                "kind": "agent_task",
                "task_id": getattr(active_task, "task_id", ""),
            }
        active_job = self._active_related_job(session_id, response_text)
        if active_job is not None:
            return {
                "durable": True,
                "kind": "agent_job",
                "job_id": getattr(active_job, "job_id", ""),
            }
        launchd_label = self._verified_launchd_label(response_text)
        if launchd_label:
            return {"durable": True, "kind": "launchd", "label": launchd_label}
        return {"durable": False, "reason": "no_active_job_task_or_launchd"}

    def _active_notifying_task(self, session_id: str) -> Any | None:
        if self.task_ledger is None:
            return None
        try:
            tasks = self.task_ledger.list(
                session_id=session_id,
                statuses=_BACKGROUND_MONITOR_ACTIVE_TASK_STATUSES,
                limit=20,
            )
        except Exception:
            return None
        for task in tasks:
            notify_policy = str(getattr(task, "notify_policy", "") or "").lower()
            mode = str(getattr(task, "mode", "") or "").lower()
            if notify_policy == "none":
                continue
            if mode == "brain_fallback":
                continue
            return task
        return None

    def _active_related_job(self, session_id: str, response_text: str) -> Any | None:
        if self.job_service is None:
            return None
        try:
            jobs = self.job_service.list(
                statuses=_BACKGROUND_MONITOR_ACTIVE_JOB_STATUSES,
                limit=100,
            )
        except Exception:
            return None
        session_tokens = {session_id}
        if session_id.startswith("tg-"):
            session_tokens.add(session_id.removeprefix("tg-"))
        response = response_text or ""
        for job in jobs:
            job_id = str(getattr(job, "job_id", "") or "")
            resume_key = str(getattr(job, "resume_key", "") or "")
            payload = getattr(job, "payload", {}) or {}
            metadata = getattr(job, "metadata", {}) or {}
            haystack = json.dumps(
                {
                    "job_id": job_id,
                    "resume_key": resume_key,
                    "payload": payload,
                    "metadata": metadata,
                },
                sort_keys=True,
                default=str,
            )
            if job_id and job_id in response:
                return job
            if any(token and token in haystack for token in session_tokens):
                return job
            notebook_id = str(payload.get("notebook_id") or "")
            if notebook_id and notebook_id in response:
                return job
        return None

    def _verified_launchd_label(self, response_text: str) -> str | None:
        if not response_text or "launch" not in response_text.lower():
            return None
        labels = sorted(set(re.findall(r"\bcom\.[A-Za-z0-9_.-]+\b", response_text)))
        if not labels:
            return None
        for label in labels[:5]:
            try:
                proc = subprocess.run(
                    ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
            except Exception:
                continue
            if proc.returncode == 0:
                return label
        return None

    def _emit_sanitizer_recovery_event(self, event_type: str, session_id: str, **payload: Any) -> None:
        if self.observe is None:
            return
        safe_payload = {"session_id": session_id, **payload}
        try:
            self.observe.emit(event_type, payload=safe_payload)
        except Exception:
            logger.debug("failed to emit %s", event_type, exc_info=True)

    def _response_has_internal_trace_suppressed(self, response: Any) -> bool:
        artifacts = getattr(response, "artifacts", {}) or {}
        contract_violation = artifacts.get("contract_violation")
        return bool(
            artifacts.get("internal_response_suppressed")
            or artifacts.get("internal_tool_trace_suppressed")
            or artifacts.get("internal_prompt_echo_suppressed")
            or contract_violation in {"internal_tool_trace", "internal_prompt_echo"}
        )

    def _suppressed_response_reason(self, response: Any) -> str:
        artifacts = getattr(response, "artifacts", {}) or {}
        reason = str(artifacts.get("contract_violation") or "").strip()
        if reason in {"internal_tool_trace", "internal_prompt_echo"}:
            return reason
        if artifacts.get("internal_prompt_echo_suppressed"):
            return "internal_prompt_echo"
        return "internal_tool_trace"

    def _pending_action_for_sanitizer_recovery(self, session_id: str) -> str | None:
        try:
            state = self.brain.memory.get_session_state(session_id)
        except Exception:
            return None
        pending_action = state.get("pending_action")
        if isinstance(pending_action, str) and pending_action.strip():
            return pending_action.strip()
        task_queue = state.get("task_queue") or []
        if isinstance(task_queue, list):
            for status in ("in_progress", "pending"):
                for item in task_queue:
                    if not isinstance(item, dict) or item.get("status") != status:
                        continue
                    summary = item.get("summary")
                    if isinstance(summary, str) and summary.strip():
                        return summary.strip()
        return None

    def _internal_trace_recovery_prompt(self, *, source_text: str, pending_action: str | None) -> str:
        lines = [
            "Reintenta la respuesta para Telegram usando el mismo pedido del usuario.",
            "No hagas metacomentarios ni pidas que Hector repita la instrucción.",
            "Continúa con el siguiente paso concreto. Si falta un dato real, pregunta solo ese dato.",
            f"Mensaje actual de Hector: {source_text}",
        ]
        if pending_action:
            lines.append(f"Acción pendiente a retomar: {pending_action}")
        if "datos en vivo" in _normalize_command_text(source_text):
            lines.append(
                "Preferencia actual: usar datos en vivo. Si necesitas elegir fuente, pide solo la fuente o preferencia faltante."
            )
        return "\n".join(lines)

    def _internal_trace_recovery_fallback(self, *, source_text: str, pending_action: str | None) -> str:
        next_step = pending_action or source_text
        next_step = _compact_summary(next_step, limit=160) or "continuar con el siguiente paso disponible"
        if "datos en vivo" in _normalize_command_text(source_text) and "datos en vivo" not in _normalize_command_text(next_step):
            next_step = f"{next_step} con datos en vivo"
        return f"Tuve un error preparando la respuesta. Retomo la acción: {next_step}."

    def _looks_like_recoverable_action_text(self, text: str) -> bool:
        normalized = _normalize_command_text(text).strip()
        if len(normalized) < 8:
            return False
        if normalized in {"estatus", "status", "modo brujula", "procede", "ok", "si", "sí"}:
            return False
        action_markers = (
            "arregla",
            "corrige",
            "completa",
            "haz ",
            "hacer ",
            "manda ",
            "manda al worker",
            "sube",
            "subelo",
            "reinicia",
            "corre",
            "ejecuta",
            "revisa",
            "lee ",
            "crea",
            "fix",
            "fixes",
        )
        return any(marker in normalized for marker in action_markers)

    def _persist_sanitizer_recovery_action(
        self,
        session_id: str,
        *,
        source_text: str,
        pending_action: str | None,
    ) -> None:
        action = (pending_action or "").strip()
        if not action and self._looks_like_recoverable_action_text(source_text):
            action = _compact_summary(source_text, limit=220) or ""
        if not action:
            return
        try:
            state = self.brain.memory.get_session_state(session_id)
            task_queue = state.get("task_queue") or []
            depends_on = self._task_handler.derive_task_dependencies(task_queue, summary=action)
            task_queue = self._task_handler.upsert_task_queue_entry(
                task_queue,
                summary=action,
                mode=_infer_session_mode(action),
                status="pending",
                source="sanitizer_recovery",
                priority=1,
                depends_on=depends_on,
            )
            self.brain.memory.update_session_state(
                session_id,
                pending_action=action,
                task_queue=task_queue,
                verification_status="pending",
            )
            self._emit_sanitizer_recovery_event(
                "pending_action_persisted_after_suppression",
                session_id,
                pending_action_preview=action[:160],
            )
        except Exception:
            logger.debug("failed to persist sanitizer recovery action", exc_info=True)

    def _prepare_visible_brain_content(
        self,
        session_id: str,
        source_text: str,
        raw_content: str,
        *,
        response: Any | None = None,
        runtime_capability_question: bool,
        link_analysis_context: dict[str, Any] | None,
    ) -> str:
        content = raw_content.strip()
        meta_evidence_skip_reason = self._meta_evidence_skip_reason(content)
        if meta_evidence_skip_reason is not None:
            self._emit_safe(
                "evidence_gate_skipped_meta",
                {
                    "session_id": session_id,
                    "reason": meta_evidence_skip_reason,
                    "meta_kind": current_meta_introspection_kind(),
                },
            )
        if not content or content == "(no result)":
            content = "Recibido. ¿Qué quieres que haga con esto?"
        elif _looks_like_pre_hook_block(content):
            content = self._maybe_augment_pre_hook_block(content)
        elif _looks_like_manual_handoff(content) and _looks_like_operator_action_request(source_text):
            if self._should_allow_tool_backed_handoff_response(response, content):
                self._emit_identity_capability_binding_guard(
                    "operator_handoff_guard_allowed_tool_backed",
                    session_id,
                    reason="tool_backed_long_result",
                    original=content,
                    sanitized=content,
                )
            else:
                corrected = self._operator_handoff_binding_response()
                self._emit_identity_capability_binding_guard(
                    "operator_handoff_guard_triggered",
                    session_id,
                    reason="manual_handoff_for_action_request",
                    original=content,
                    sanitized=corrected,
                )
                self._emit_internal_chat_suppressed(
                    session_id,
                    reason="manual_handoff_for_action_request",
                    original=content,
                    sanitized=corrected,
                )
                content = corrected
        elif self._start_claim_lacks_evidence(
            session_id=session_id,
            source_text=source_text,
            content=content,
            response=response,
        ):
            meta_kind = current_meta_introspection_kind()
            if meta_kind is not None:
                if meta_evidence_skip_reason is None:
                    self._emit_safe(
                        "evidence_gate_skipped_meta",
                        {
                            "session_id": session_id,
                            "reason": "start_claim_without_evidence",
                            "meta_kind": meta_kind,
                        },
                    )
            else:
                blocker_task_id = self._record_evidence_gate_explicit_blocker(
                    session_id=session_id,
                    source_text=source_text,
                    blocked_content=content,
                    reason="start_claim_without_evidence",
                )
                corrected = self._unexecuted_start_response(blocker_task_id)
                self._emit_identity_capability_binding_guard(
                    "evidence_gate_blocked_start_claim",
                    session_id,
                    reason="start_claim_without_evidence",
                    original=content,
                    sanitized=corrected,
                )
                self._emit_internal_chat_suppressed(
                    session_id,
                    reason="start_claim_without_evidence",
                    original=content,
                    sanitized=corrected,
                )
                content = corrected
        elif self._completion_claim_lacks_evidence(
            session_id=session_id,
            source_text=source_text,
            content=content,
            response=response,
            link_analysis_context=link_analysis_context,
        ):
            meta_kind = current_meta_introspection_kind()
            if meta_kind is not None:
                if meta_evidence_skip_reason is None:
                    self._emit_safe(
                        "evidence_gate_skipped_meta",
                        {
                            "session_id": session_id,
                            "reason": "completion_claim_without_evidence",
                            "meta_kind": meta_kind,
                        },
                    )
            else:
                blocker_task_id = self._record_evidence_gate_explicit_blocker(
                    session_id=session_id,
                    source_text=source_text,
                    blocked_content=content,
                    reason="completion_claim_without_evidence",
                )
                corrected = self._pending_evidence_response(blocker_task_id)
                self._emit_identity_capability_binding_guard(
                    "evidence_gate_blocked_completion_claim",
                    session_id,
                    reason="completion_claim_without_evidence",
                    original=content,
                    sanitized=corrected,
                )
                self._emit_internal_chat_suppressed(
                    session_id,
                    reason="completion_claim_without_evidence",
                    original=content,
                    sanitized=corrected,
                )
                content = corrected
        elif _looks_like_confirmed_returned_record_claim(content) and (
            self._brain_trace_only_has_tool_failures(response)
        ):
            # msg 1095 class: the outgoing message presents a concrete owner
            # record as a confirmed TAD lookup, but the only tool evidence for
            # the turn is a FAILED (non-zero-exit) run for the wrong address.
            # Block the "confirmada" framing and force a failure report so a
            # paid decision can never rest on a failed tool's leaked snippet.
            meta_kind = current_meta_introspection_kind()
            if meta_kind is not None:
                if meta_evidence_skip_reason is None:
                    self._emit_safe(
                        "evidence_gate_skipped_meta",
                        {
                            "session_id": session_id,
                            "reason": "confirmed_record_from_failed_tool",
                            "meta_kind": meta_kind,
                        },
                    )
            else:
                blocker_task_id = self._record_evidence_gate_explicit_blocker(
                    session_id=session_id,
                    source_text=source_text,
                    blocked_content=content,
                    reason="confirmed_record_from_failed_tool",
                )
                corrected = self._unconfirmed_record_response(blocker_task_id)
                self._emit_identity_capability_binding_guard(
                    "evidence_gate_blocked_confirmed_record",
                    session_id,
                    reason="confirmed_record_from_failed_tool",
                    original=content,
                    sanitized=corrected,
                )
                self._emit_internal_chat_suppressed(
                    session_id,
                    reason="confirmed_record_from_failed_tool",
                    original=content,
                    sanitized=corrected,
                )
                content = corrected
        elif _looks_like_identity_drift(content):
            corrected = self._identity_drift_corrected_response(content)
            self._emit_identity_capability_binding_guard(
                "identity_drift_guard_triggered",
                session_id,
                reason="provider_identity_leak",
                original=content,
                sanitized=corrected,
            )
            self._emit_internal_chat_suppressed(
                session_id,
                reason="identity_drift",
                original=content,
                sanitized=corrected,
            )
            content = corrected
        elif _looks_like_unverified_capability_denial(content):
            corrected = self._correct_unverified_capability_denial(content)
            if corrected is not None:
                self._emit_identity_capability_binding_guard(
                    "capability_binding_guard_triggered",
                    session_id,
                    reason="unverified_capability_denial",
                    original=content,
                    sanitized=corrected,
                )
                self._emit_internal_chat_suppressed(
                    session_id,
                    reason="unverified_capability_denial",
                    original=content,
                    sanitized=corrected,
                )
                content = corrected
        elif runtime_capability_question:
            content = _enforce_runtime_capability_sections(content)
        elif link_analysis_context is not None:
            content = _enforce_link_analysis_sections(
                content,
                url=link_analysis_context["url"],
                fetched_content=link_analysis_context["fetched_content"],
            )
        cleaned = _strip_url_permission_deferrals(content, context=source_text)
        if cleaned != content:
            self._emit_safe(
                "url_autonomy_guard_triggered",
                {
                    "session_id": session_id,
                    "source_text_preview": source_text[:160],
                    "original_length": len(content),
                    "sanitized_length": len(cleaned),
                },
            )
            self._emit_internal_chat_suppressed(
                session_id,
                reason="url_autonomy_permission_deferral",
                original=content,
                sanitized=cleaned,
            )
            content = cleaned
        return self._sanitize_visible_chat_response(session_id, content)

    def _start_claim_lacks_evidence(
        self,
        *,
        session_id: str,
        source_text: str,
        content: str,
        response: Any | None,
    ) -> bool:
        if not _looks_like_operator_action_request(source_text):
            return False
        if not _looks_like_starting_side_effect_claim(content):
            return False
        if _user_authoritatively_marked_done(source_text):
            self._emit_safe(
                "evidence_gate_skipped_user_authority",
                {"session_id": session_id, "claim_type": "start"},
            )
            return False
        if _brain_content_references_evidence(content):
            self._emit_safe(
                "evidence_gate_skipped_content_evidence_ref",
                {"session_id": session_id, "claim_type": "start"},
            )
            return False
        skip_reason = _evidence_gate_plan_status_skip_reason(source_text, content)
        if skip_reason is not None:
            self._emit_safe(
                "evidence_gate_skipped_plan_status",
                {
                    "session_id": session_id,
                    "claim_type": "start",
                    "reason": skip_reason,
                },
            )
            return False
        if self._response_has_evidence_signal(response):
            return False
        if self._session_has_fresh_evidence(session_id):
            return False
        return True

    def _completion_claim_lacks_evidence(
        self,
        *,
        session_id: str,
        source_text: str,
        content: str,
        response: Any | None,
        link_analysis_context: dict[str, Any] | None = None,
    ) -> bool:
        if not _looks_like_operator_action_request(source_text):
            return False
        if not _looks_like_completion_side_effect_claim(content):
            return False
        if _user_authoritatively_marked_done(source_text):
            self._emit_safe(
                "evidence_gate_skipped_user_authority",
                {"session_id": session_id, "claim_type": "completion"},
            )
            return False
        if _brain_content_references_evidence(content):
            self._emit_safe(
                "evidence_gate_skipped_content_evidence_ref",
                {"session_id": session_id, "claim_type": "completion"},
            )
            return False
        skip_reason = _evidence_gate_plan_status_skip_reason(source_text, content)
        if skip_reason is not None:
            self._emit_safe(
                "evidence_gate_skipped_plan_status",
                {
                    "session_id": session_id,
                    "claim_type": "completion",
                    "reason": skip_reason,
                },
            )
            return False
        if self._link_analysis_context_has_evidence(link_analysis_context):
            return False
        if self._response_has_evidence_signal(response):
            return False
        if self._session_has_fresh_evidence(session_id):
            return False
        return True

    @staticmethod
    def _link_analysis_context_has_evidence(context: dict[str, Any] | None) -> bool:
        if not isinstance(context, dict):
            return False
        fetched_content = str(context.get("fetched_content") or "").strip()
        if not fetched_content:
            return False
        lowered = fetched_content.lower()
        if lowered.startswith("browse error"):
            return False
        if "no se pudo leer" in lowered or "all browse backends fail" in lowered:
            return False
        return _is_usable_browse_content(str(context.get("url") or ""), fetched_content)

    @staticmethod
    def _meta_evidence_skip_reason(content: str) -> str | None:
        if current_meta_introspection_kind() is None:
            return None
        if _looks_like_starting_side_effect_claim(content):
            return "start_claim_without_evidence"
        if _looks_like_completion_side_effect_claim(content):
            return "completion_claim_without_evidence"
        return None

    def _response_has_evidence_signal(self, response: Any | None) -> bool:
        if response is None:
            return False
        artifacts = getattr(response, "artifacts", {}) or {}
        if not isinstance(artifacts, dict):
            return False
        if artifacts.get("evidence_manifest"):
            return True
        if artifacts.get("tool_calls"):
            return True
        if artifacts.get("observe_event_ids"):
            return True
        trace_id = str(artifacts.get("trace_id") or "")
        if trace_id and self.observe is not None:
            try:
                events = self.observe.trace_events(trace_id)
            except Exception:
                events = []
            return any(
                str(event.get("event_type") or "")
                in {"sdk_post_tool_use", "sdk_post_tool_use_failure", "approval_required"}
                for event in events
            )
        return False

    def _brain_trace_only_has_tool_failures(self, response: Any | None) -> bool:
        """True when the turn's trace has at least one ``sdk_post_tool_use_failure``
        and NO exit-0 ``sdk_post_tool_use``.

        ``_response_has_evidence_signal`` intentionally treats a failed tool run
        as an evidence signal (a failure is still proof the runtime tried). That
        is wrong for a *confirmed-returned-record* claim: a non-zero-exit run is
        not a source of confirmed data. This narrower check exists only for that
        gate branch and never weakens the broader evidence signal.
        """
        if response is None or self.observe is None:
            return False
        artifacts = getattr(response, "artifacts", {}) or {}
        if not isinstance(artifacts, dict):
            return False
        trace_id = str(artifacts.get("trace_id") or "")
        if not trace_id:
            return False
        try:
            events = self.observe.trace_events(trace_id)
        except Exception:
            return False
        has_success = False
        has_failure = False
        for event in events:
            etype = str(event.get("event_type") or "")
            if etype == "sdk_post_tool_use":
                has_success = True
            elif etype == "sdk_post_tool_use_failure":
                has_failure = True
        return has_failure and not has_success

    def _should_allow_tool_backed_handoff_response(self, response: Any | None, content: str) -> bool:
        if len(content or "") < 1200:
            return False
        if not self._response_has_evidence_signal(response):
            return False
        return self._brain_response_has_useful_result(response)

    def _session_has_fresh_evidence(self, session_id: str) -> bool:
        if self.task_ledger is None:
            return False
        try:
            records = self.task_ledger.list(session_id=session_id, limit=3)
        except Exception:
            return False
        now = time.time()
        for record in records:
            try:
                updated_at = float(getattr(record, "updated_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                updated_at = 0.0
            if updated_at and now - updated_at > 120:
                continue
            verification = str(getattr(record, "verification_status", "") or "").lower()
            artifacts = dict(getattr(record, "artifacts", {}) or {})
            if verification in {"passed", "ok", "verified"} and artifacts:
                return True
            manifest = artifacts.get("evidence_manifest")
            if isinstance(manifest, dict) and manifest:
                return True
        return False

    def _recover_internal_trace_suppression(
        self,
        *,
        session_id: str,
        source_text: str,
        response: Any,
        runtime_capability_question: bool,
        link_analysis_context: dict[str, Any] | None,
        runtime_channel: str | None,
        pre_turn_message_id: int,
    ) -> str:
        suppression_reason = self._suppressed_response_reason(response)
        pending_action = self._pending_action_for_sanitizer_recovery(session_id)
        self._emit_sanitizer_recovery_event(
            "internal_trace_detected",
            session_id,
            reason=suppression_reason,
            response_length=len(str(getattr(response, "content", "") or "")),
        )
        self._emit_sanitizer_recovery_event(
            "internal_trace_suppressed_from_chat",
            session_id,
            reason=suppression_reason,
            has_pending_action=bool(pending_action),
        )
        self._emit_internal_chat_suppressed(
            session_id,
            reason=suppression_reason,
            original=str(getattr(response, "content", "") or ""),
            sanitized="",
        )
        try:
            self.brain.memory.delete_messages_after(session_id, after_id=pre_turn_message_id)
            provider = str(getattr(response, "provider", "") or "")
            if provider:
                self.brain.memory.clear_provider_session(session_id, provider)
        except Exception:
            logger.debug("failed to remove suppressed brain turn before retry", exc_info=True)
        if pending_action:
            self._emit_sanitizer_recovery_event(
                "pending_action_resumed_after_suppression",
                session_id,
                pending_action_preview=pending_action[:160],
            )
        retry_prompt = self._with_runtime_capability_context(
            self._internal_trace_recovery_prompt(source_text=source_text, pending_action=pending_action),
            runtime_channel=runtime_channel,
        )
        self._emit_sanitizer_recovery_event(
            "clean_retry_started",
            session_id,
            has_pending_action=bool(pending_action),
        )
        try:
            retry_response = self.brain.handle_message(
                session_id,
                retry_prompt,
                memory_text=source_text,
                task_type="telegram_message",
            )
        except ApprovalPending as exc:
            self._record_pending_tool_approval(
                session_id=session_id,
                user_text=source_text,
                exc=exc,
            )
            reply = _format_approval_pending(exc)
            self._emit_sanitizer_recovery_event(
                "clean_retry_completed",
                session_id,
                outcome="approval_pending",
            )
            return self._sanitize_visible_chat_response(session_id, reply)
        except Exception as exc:
            logger.exception("clean retry after internal trace suppression failed")
            fallback = self._sanitize_visible_chat_response(
                session_id,
                self._internal_trace_recovery_fallback(source_text=source_text, pending_action=pending_action),
            )
            self._emit_sanitizer_recovery_event(
                "clean_retry_failed",
                session_id,
                reason=type(exc).__name__,
            )
            self._persist_sanitizer_recovery_action(
                session_id,
                source_text=source_text,
                pending_action=pending_action,
            )
            self._store_memory_turn(session_id, source_text, fallback, assistant_limit=2000)
            return fallback

        if self._response_has_internal_trace_suppressed(retry_response):
            self._emit_sanitizer_recovery_event(
                "clean_retry_failed",
                session_id,
                reason="internal_trace_repeated",
            )
            try:
                self.brain.memory.delete_last_messages(session_id, count=2)
                provider = str(getattr(retry_response, "provider", "") or "")
                if provider:
                    self.brain.memory.clear_provider_session(session_id, provider)
            except Exception:
                logger.debug("failed to remove failed clean retry turn", exc_info=True)
            # Hotfix F: actionable requests land in the recovery_jobs queue
            # with failure_reason=provider_repeated_internal_trace instead of
            # being silently dropped behind the generic apology.
            recovery = self.brain.queue_internal_trace_recovery_job(
                session_id, source_text=source_text
            )
            if recovery is not None:
                _job_id, recovery_message = recovery
                fallback = self._sanitize_visible_chat_response(session_id, recovery_message)
            else:
                fallback = self._sanitize_visible_chat_response(
                    session_id,
                    self._internal_trace_recovery_fallback(
                        source_text=source_text, pending_action=pending_action
                    ),
                )
            self._persist_sanitizer_recovery_action(
                session_id,
                source_text=source_text,
                pending_action=pending_action,
            )
            self._store_memory_turn(session_id, source_text, fallback, assistant_limit=2000)
            return fallback

        raw_retry_content = getattr(retry_response, "content", "") or ""
        content = self._prepare_visible_brain_content(
            session_id,
            source_text,
            raw_retry_content,
            runtime_capability_question=runtime_capability_question,
            link_analysis_context=link_analysis_context,
        )
        if content != raw_retry_content:
            self.brain.memory.replace_latest_assistant_message(session_id, raw_retry_content, content)
        self._emit_sanitizer_recovery_event(
            "clean_retry_completed",
            session_id,
            response_length=len(content),
        )
        return content

    def _remember_inbound_context(self, session_id: str, metadata: dict[str, Any] | None) -> None:
        if not isinstance(metadata, dict):
            return
        reply_context = metadata.get("reply_context")
        if not isinstance(reply_context, dict):
            return
        text = str(reply_context.get("text") or "").strip()
        if not text:
            return
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["reply_context"] = {
            "source": "telegram_reply",
            "text": text[:2000],
            "created_at": time.time(),
        }
        self.brain.memory.update_session_state(session_id, active_object=active_object)
        if self.observe is not None:
            try:
                self.observe.emit(
                    "reply_context_loaded",
                    payload={
                        "session_id": session_id,
                        "source": "telegram_reply",
                        "text_length": len(text),
                    },
                )
            except Exception:
                logger.debug("failed to emit reply_context_loaded", exc_info=True)

    def _maybe_handle_brain_first_new_task(
        self,
        *,
        semantic_turn: SemanticTurn,
        session_id: str,
        text: str,
        runtime_channel: str | None,
    ) -> str | None:
        if (runtime_channel or "").strip().lower() != "telegram":
            return None
        if semantic_turn.intent != "new_task" or not semantic_turn.clear_goal:
            return None
        if not self._looks_like_durable_mission_request(text):
            return None

        objective = semantic_turn.objective or text.strip()
        mission_name = self._extract_requested_mission_name(text)
        task_id = f"{session_id}:brain-first:{time.time_ns()}"
        mission_id = f"mission:{session_id}:{self._stable_text_hash(objective)}"
        now = time.time()
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["active_mission"] = {
            "mission_id": mission_id,
            "channel": "telegram",
            "chat_id": session_id,
            "active_target": mission_name or "durable mission",
            "active_artifact": "continuation smoke proposal",
            "last_user_goal": objective[:240],
            "pending_action": objective,
            "proposal_task_id": task_id,
            "status": "waiting_for_continue",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + 30 * 60,
        }
        active_object["last_actionable_proposal"] = {
            "objective": objective[:500],
            "source": "brain_first_new_task",
            "task_id": task_id,
            "created_at": now,
        }
        task_queue = self._task_handler.upsert_task_queue_entry(
            state.get("task_queue") or [],
            summary=objective,
            mode=_infer_session_mode(objective),
            status="pending",
            source="brain_first_new_task",
            priority=0,
            depends_on=self._task_handler.derive_task_dependencies(
                state.get("task_queue") or [],
                summary=objective,
            ),
        )
        if self.task_ledger is not None:
            artifacts = {
                "semantic_turn": {
                    "intent": semantic_turn.intent,
                    "confidence": semantic_turn.confidence,
                    "clear_goal": semantic_turn.clear_goal,
                    "reasons": list(semantic_turn.reasons),
                },
                "proposal": {
                    "status": "waiting_for_continue",
                    "mission_name": mission_name or "",
                },
                "evidence": {"brain_first_semantic_classification": semantic_turn.intent},
            }
            self.task_ledger.create(
                task_id=task_id,
                session_id=session_id,
                objective=objective,
                runtime="brain_first",
                mode=_infer_session_mode(objective),
                status="running",
                notify_policy="none",
                route={"channel": "telegram", "external_session_id": session_id},
                metadata={
                    "origin": "brain_first_semantic",
                    "semantic_intent": semantic_turn.intent,
                    "mission_id": mission_id,
                    "mission_name": mission_name or "",
                    "source_message": text[:500],
                    "result_status": "waiting_for_continue",
                },
                artifacts=artifacts,
            )
            self.task_ledger.mark_running_checkpoint(
                task_id,
                summary="Brain-first durable mission proposal created; waiting for explicit continuation.",
                verification_status="awaiting_continue",
                artifacts=artifacts,
            )

        self.brain.memory.update_session_state(
            session_id,
            mode="ops",
            current_goal=objective[:280] if self._looks_like_persistable_current_goal(objective) else "",
            pending_action=objective,
            task_queue=task_queue,
            verification_status="awaiting_continue",
            active_object=active_object,
            last_checkpoint={
                "summary": "Brain-first durable mission proposal is waiting for continuation.",
                "verification_status": "awaiting_continue",
                "task_id": task_id,
                "mission_id": mission_id,
            },
        )
        self._emit_safe(
            "brain_first_new_task_created",
            {
                "session_id": session_id,
                "task_id": task_id,
                "mission_id": mission_id,
                "mission_name": mission_name or "",
                "objective_preview": objective[:160],
            },
        )
        name_fragment = f" `{mission_name}`" if mission_name else ""
        response = (
            f"Creé la misión durable{name_fragment} y la dejé lista como propuesta. "
            "Respóndeme \"Procede\" para ejecutarla; si quieres ajustar el alcance, dime el cambio concreto."
        )
        rendered = NaturalLanguageRenderer(mode="normal").render(response)
        self._store_memory_turn(session_id, text, rendered, assistant_limit=2000)
        self._remember_assistant_turn_state(session_id, text, rendered)
        self._emit_semantic_turn_trace(
            session_id=session_id,
            text=text,
            semantic_turn=semantic_turn,
            state_sources_checked=["message_text", "session_state", "task_ledger"],
            approval_scope_match="skipped_new_task",
            decision="new_task_proposal_created",
            output_kind="natural_reply",
            response_text=rendered,
        )
        return rendered

    @staticmethod
    def _looks_like_durable_mission_request(text: str) -> bool:
        normalized = _normalize_command_text(text)
        return (
            "mision durable" in normalized
            or "misión durable" in text.lower()
            or "durable mission" in normalized
            or "mission durable" in normalized
        )

    @staticmethod
    def _extract_requested_mission_name(text: str) -> str | None:
        match = re.search(
            r"\b(?:llamada|llamado|called|named)\s+([A-Za-z0-9][A-Za-z0-9_.:-]{1,120})",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        return match.group(1).strip(" .,:;!?`'\"") or None

    def handle_text(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        runtime_channel: str | None = None,
        context_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        # P2: open a fresh turn_id_context so every observe event, ledger
        # row, and approval created during this turn carries the same
        # correlator. The context resets on method exit. See
        # claw_v2/turn_context.py and INTERNAL_WIRING.md §1.
        turn_id = new_turn_id()
        started_at = time.time()
        with turn_id_context(turn_id):
            try:
                return self._handle_text_body(
                    user_id=user_id,
                    session_id=session_id,
                    text=text,
                    runtime_channel=runtime_channel,
                    context_metadata=context_metadata,
                )
            finally:
                # Post-merge wave: emit a behavior_turn_receipt summarising
                # the turn — intent, tools, approvals, ledger status — so
                # post-mortem queries get one row per turn instead of
                # reconstructing from N event types.
                observe = getattr(self, "observe", None)
                if observe is not None:
                    try:
                        emit_turn_receipt(
                            observe,
                            turn_id=turn_id,
                            session_id=session_id,
                            user_text=text,
                            started_at=started_at,
                        )
                    except Exception:
                        # Receipt emission must never break a turn.
                        logger.debug("turn_receipt emit failed", exc_info=True)

    def _handle_text_body(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        runtime_channel: str | None = None,
        context_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        self._ensure_default_autonomy(session_id)
        self._remember_inbound_context(session_id, context_metadata)
        stripped = text.strip()
        semantic_turn = classify_semantic_turn(stripped)
        self._emit_semantic_turn_trace(
            session_id=session_id,
            text=stripped,
            semantic_turn=semantic_turn,
            state_sources_checked=["message_text"],
            approval_scope_match=self._semantic_approval_scope_default(semantic_turn),
            decision="classified_before_state_resolution",
            output_kind="routing_trace",
        )
        context = CommandContext(user_id=user_id, session_id=session_id, text=text, stripped=stripped)
        try:
            command_response = dispatch_commands(self._pre_state_commands, context)
        except ApprovalPending as exc:
            self._record_pending_tool_approval(
                session_id=session_id,
                user_text=stripped,
                exc=exc,
            )
            return _format_approval_pending(exc)
        if isinstance(command_response, _BrainShortcut):
            return self._brain_text_response(
                session_id,
                command_response.text,
                memory_text=command_response.memory_text,
                runtime_channel=runtime_channel,
            )
        if command_response is not None:
            return command_response
        brain_first_new_task_response = self._maybe_handle_brain_first_new_task(
            semantic_turn=semantic_turn,
            session_id=session_id,
            text=stripped,
            runtime_channel=runtime_channel,
        )
        self._emit_dispatch_decision(
            handler="brain_first_new_task",
            route="brain_shortcut" if brain_first_new_task_response is not None else "fall_through",
            reason=(
                "brain_first_new_task_routed"
                if brain_first_new_task_response is not None
                else "brain_first_new_task_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=False,
        )
        if brain_first_new_task_response is not None:
            return brain_first_new_task_response
        computer_approval_response = self._handle_pending_computer_approval_response(
            session_id,
            stripped,
            semantic_turn=semantic_turn,
        )
        self._emit_dispatch_decision(
            handler="computer_approval",
            route="intercepted" if computer_approval_response is not None else "fall_through",
            reason=(
                "computer_approval_resolved"
                if computer_approval_response is not None
                else "computer_approval_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=computer_approval_response is not None,
        )
        if computer_approval_response is not None:
            self._store_memory_turn(session_id, stripped, computer_approval_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, computer_approval_response)
            return computer_approval_response
        # Marker event at the dispatch boundary: when the message qualifies
        # as an explicit command (slash prefix or literal task_id), emit a
        # single `explicit_command` route record so the audit stream can
        # distinguish "user typed a command" from "heuristic captured it".
        if _is_explicit_command(stripped):
            self._emit_dispatch_decision(
                handler="explicit_command",
                route="explicit_command",
                reason=(
                    "slash_prefix"
                    if stripped.startswith("/")
                    else "literal_task_id_match"
                ),
                session_id=session_id,
                text=stripped,
                captured=True,
            )
        route_ctx = RouteContext(
            user_id=user_id,
            session_id=session_id,
            text=text,
            stripped=stripped,
            runtime_channel=runtime_channel,
        )
        route_outcome = dispatch_routes(
            self._pre_brain_routes,
            route_ctx,
            on_decision=self._emit_route_decision,
        )
        if route_outcome.captured and route_outcome.response is not None:
            self._post_capture_intercepted(
                session_id,
                stripped,
                route_outcome.response,
                assistant_limit=route_outcome.store_memory_limit,
            )
            return route_outcome.response
        pending_tasks_matched = self._pending_tasks_query_matches(stripped)
        self._emit_dispatch_decision(
            handler="pending_tasks",
            route="intercepted" if pending_tasks_matched else "fall_through",
            reason=(
                "pending_tasks_query_matched"
                if pending_tasks_matched
                else "pending_tasks_query_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=pending_tasks_matched,
        )
        if pending_tasks_matched:
            return self._handle_pending_tasks_query(
                stripped,
                session_id=session_id,
                runtime_channel=runtime_channel,
            )
        failure_summary_response = self._maybe_handle_operational_failure_summary(
            stripped,
            session_id=session_id,
        )
        self._emit_dispatch_decision(
            handler="operational_failure_summary",
            route="intercepted" if failure_summary_response is not None else "fall_through",
            reason=(
                "operational_failure_summary_matched"
                if failure_summary_response is not None
                else "operational_failure_summary_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=failure_summary_response is not None,
        )
        if failure_summary_response is not None:
            failure_summary_response = self._quality_guard_response(
                session_id,
                stripped,
                failure_summary_response,
                source="operational_failure_summary",
            )
            self._store_memory_turn(session_id, stripped, failure_summary_response, assistant_limit=3000)
            self._remember_assistant_turn_state(session_id, stripped, failure_summary_response)
            return failure_summary_response
        operational_status_response = self._maybe_handle_operational_status(stripped, session_id=session_id)
        self._emit_dispatch_decision(
            handler="operational_status",
            route="intercepted" if operational_status_response is not None else "fall_through",
            reason=(
                "operational_status_matched"
                if operational_status_response is not None
                else "operational_status_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=operational_status_response is not None,
        )
        if operational_status_response is not None:
            operational_status_response = self._quality_guard_response(
                session_id,
                stripped,
                operational_status_response,
                source="operational_status",
            )
            self._store_memory_turn(session_id, stripped, operational_status_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, operational_status_response)
            return operational_status_response
        cleanup_status_response = self._maybe_handle_cleanup_status_query(stripped, session_id=session_id)
        self._emit_dispatch_decision(
            handler="cleanup_status",
            route="intercepted" if cleanup_status_response is not None else "fall_through",
            reason=(
                "cleanup_status_matched"
                if cleanup_status_response is not None
                else "cleanup_status_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=cleanup_status_response is not None,
        )
        if cleanup_status_response is not None:
            self._store_memory_turn(session_id, stripped, cleanup_status_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, cleanup_status_response)
            return cleanup_status_response
        owner_delegation_response = self._maybe_handle_owner_delegation_request(
            stripped,
            session_id=session_id,
            runtime_channel=runtime_channel,
        )
        self._emit_dispatch_decision(
            handler="owner_delegation",
            route="intercepted" if owner_delegation_response is not None else "fall_through",
            reason=(
                "owner_delegation_matched"
                if owner_delegation_response is not None
                else "owner_delegation_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=owner_delegation_response is not None,
        )
        if owner_delegation_response is not None:
            self._store_memory_turn(
                session_id, stripped, owner_delegation_response, assistant_limit=4000
            )
            self._remember_assistant_turn_state(session_id, stripped, owner_delegation_response)
            return owner_delegation_response
        telegram_imperative_response, telegram_imperative_reason, telegram_imperative_pattern = (
            self._maybe_handle_telegram_imperative_request(
                stripped,
                session_id=session_id,
                runtime_channel=runtime_channel,
            )
        )
        if isinstance(telegram_imperative_response, _BrainShortcut):
            telegram_imperative_route, telegram_imperative_captured = "brain_shortcut", False
        elif telegram_imperative_response is not None:
            telegram_imperative_route, telegram_imperative_captured = "intercepted", True
        else:
            telegram_imperative_route, telegram_imperative_captured = "fall_through", False
        self._emit_dispatch_decision(
            handler="telegram_imperative",
            route=telegram_imperative_route,
            reason=telegram_imperative_reason,
            session_id=session_id,
            text=stripped,
            captured=telegram_imperative_captured,
            matched_pattern=telegram_imperative_pattern,
        )
        if isinstance(telegram_imperative_response, _BrainShortcut):
            return self._handle_stateful_brain_shortcut(
                session_id,
                stripped,
                telegram_imperative_response,
                runtime_channel=runtime_channel,
            )
        if telegram_imperative_response is not None:
            telegram_imperative_response = self._quality_guard_response(
                session_id,
                stripped,
                telegram_imperative_response,
                source="telegram_imperative",
            )
            telegram_imperative_response = self._final_render(session_id, telegram_imperative_response)
            self._store_memory_turn(session_id, stripped, telegram_imperative_response, assistant_limit=3000)
            self._remember_assistant_turn_state(session_id, stripped, telegram_imperative_response)
            return telegram_imperative_response
        actionable_task_response = self._maybe_handle_actionable_task_request(
            stripped,
            session_id=session_id,
            runtime_channel=runtime_channel,
            semantic_turn=semantic_turn,
        )
        self._emit_dispatch_decision(
            handler="telegram_actionable_task",
            route="intercepted" if actionable_task_response is not None else "fall_through",
            reason=(
                "telegram_actionable_task_matched"
                if actionable_task_response is not None
                else "telegram_actionable_task_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=actionable_task_response is not None,
        )
        if actionable_task_response is not None:
            self._store_memory_turn(session_id, stripped, actionable_task_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, actionable_task_response)
            return actionable_task_response
        task_intent_response = self._maybe_handle_task_intent(stripped, session_id=session_id)
        # The task intent router is gated by CLAW_DISABLE_TASK_INTENT_ROUTER
        # (default ON); a fall_through can be either "disabled by flag" or
        # "classifier returned unknown". Distinguish them so audits show why.
        if task_intent_response is not None:
            task_intent_reason = "task_intent_classifier_matched"
        elif (
            os.getenv("CLAW_DISABLE_TASK_INTENT_ROUTER", "1") == "1"
            and not _has_literal_task_id(stripped)
        ):
            task_intent_reason = "disabled_by_flag"
        else:
            task_intent_reason = "task_intent_no_match"
        self._emit_dispatch_decision(
            handler="task_intent",
            route="intercepted" if task_intent_response is not None else "fall_through",
            reason=task_intent_reason,
            session_id=session_id,
            text=stripped,
            captured=task_intent_response is not None,
        )
        if task_intent_response is not None:
            self._store_memory_turn(session_id, stripped, task_intent_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, task_intent_response)
            return task_intent_response
        change_status_response = self._maybe_handle_change_status_question(stripped, session_id=session_id)
        self._emit_dispatch_decision(
            handler="change_status_question",
            route="intercepted" if change_status_response is not None else "fall_through",
            reason=(
                "change_status_phrase_matched"
                if change_status_response is not None
                else "change_status_phrase_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=change_status_response is not None,
        )
        if change_status_response is not None:
            self._store_memory_turn(session_id, stripped, change_status_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, change_status_response)
            return change_status_response
        # PR 0B: meta/introspection guard. Reflective questions, clarification
        # asks, audit requests, and secret-shaped tokens must NOT reach the
        # coordinator/coding pipeline; route them to brain chat instead. An
        # explicit implementation verb in the message ("implementa", "patch X")
        # disqualifies the guard so real coding work still flows downstream.
        meta_intent = detect_meta_introspection_request(stripped)
        if meta_intent is not None:
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "meta_introspection_match",
                        payload={
                            "session_id": session_id,
                            "kind": meta_intent.kind,
                            "reason": meta_intent.reason,
                            "normalized_text": meta_intent.normalized_text,
                        },
                    )
                except Exception:
                    logger.debug("meta_introspection_match emit failed", exc_info=True)
            routed_event = (
                "meta_introspection_routed_to_audit"
                if meta_intent.kind == "audit"
                else "meta_introspection_routed_to_chat"
            )
            if self.observe is not None:
                try:
                    self.observe.emit(
                        routed_event,
                        payload={
                            "session_id": session_id,
                            "kind": meta_intent.kind,
                            "reason": meta_intent.reason,
                        },
                    )
                except Exception:
                    logger.debug("%s emit failed", routed_event, exc_info=True)
            self._emit_dispatch_decision(
                handler="meta_introspection_guard",
                route="intercepted",
                reason=f"meta_introspection:{meta_intent.kind}",
                session_id=session_id,
                text=stripped,
                captured=True,
                matched_pattern=meta_intent.reason,
            )
            # Invariant: the path handle_text → _brain_text_response →
            # _prepare_visible_brain_content must stay synchronous on this thread;
            # converting any step to async or copying context across threads
            # resets the ContextVar before the evidence-gate reads it. See
            # INTERNAL_WIRING.md §1 evidence_gate_meta_skip_sync_path.
            with meta_introspection_context(meta_intent.kind):
                return self._brain_text_response(
                    session_id, stripped, runtime_channel=runtime_channel
                )
        if self.observe is not None:
            try:
                self.observe.emit(
                    "meta_introspection_no_match",
                    payload={"session_id": session_id},
                )
            except Exception:
                logger.debug("meta_introspection_no_match emit failed", exc_info=True)
        if has_explicit_implementation_request(stripped):
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "meta_introspection_allows_implementation",
                        payload={"session_id": session_id},
                    )
                except Exception:
                    logger.debug(
                        "meta_introspection_allows_implementation emit failed",
                        exc_info=True,
                    )
        # Most semantic pre-brain routes stay gated, but explicit operational
        # capability requests like AI news and X trends remain deterministic.
        capability_route_allowed = self._capability_route_allowed(stripped)
        if capability_route_allowed:
            capability_route_response = self._maybe_handle_capability_route(
                stripped, session_id=session_id
            )
        else:
            capability_route_response = None
        if capability_route_response is not None:
            capability_reason = "capability_route_matched"
        elif not capability_route_allowed:
            capability_reason = "disabled_by_flag"
        else:
            capability_reason = "capability_route_no_match"
        self._emit_dispatch_decision(
            handler="capability_route",
            route="intercepted" if capability_route_response is not None else "fall_through",
            reason=capability_reason,
            session_id=session_id,
            text=stripped,
            captured=capability_route_response is not None,
        )
        if capability_route_response is not None:
            self._store_memory_turn(session_id, stripped, capability_route_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, capability_route_response)
            return capability_route_response
        self._remember_user_turn_state(session_id, stripped)
        pending_tool_approval = self._handle_pending_tool_approval_grant_response(session_id, stripped)
        self._emit_dispatch_decision(
            handler="pending_tool_approval_grant",
            route="intercepted" if pending_tool_approval is not None else "fall_through",
            reason=(
                "pending_tool_approval_resolved"
                if pending_tool_approval is not None
                else "pending_tool_approval_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=pending_tool_approval is not None,
        )
        if pending_tool_approval is not None:
            return pending_tool_approval
        autonomy_grant_matched = _looks_like_autonomy_grant(stripped)
        self._emit_dispatch_decision(
            handler="autonomy_grant",
            route="intercepted" if autonomy_grant_matched else "fall_through",
            reason="autonomy_grant_matched" if autonomy_grant_matched else "autonomy_grant_no_match",
            session_id=session_id,
            text=stripped,
            captured=autonomy_grant_matched,
        )
        if autonomy_grant_matched:
            return self._handle_autonomy_grant_response(session_id, stripped)
        stateful_followup = self._maybe_resolve_stateful_followup(stripped, session_id=session_id)
        if isinstance(stateful_followup, _BrainShortcut):
            # route=brain_shortcut: the turn is handled by the brain — this
            # dispatcher only enriched the prompt. Labeling it "intercepted"
            # made captures indistinguishable from brain routes in the stream.
            self._emit_dispatch_decision(
                handler="stateful_followup",
                route="brain_shortcut",
                reason="stateful_followup_brain_shortcut",
                session_id=session_id,
                text=stripped,
                captured=False,
            )
            return self._handle_stateful_brain_shortcut(
                session_id,
                stripped,
                stateful_followup,
                runtime_channel=runtime_channel,
            )
        self._emit_dispatch_decision(
            handler="stateful_followup",
            route="intercepted" if stateful_followup is not None else "fall_through",
            reason=(
                "stateful_followup_resolved"
                if stateful_followup is not None
                else "stateful_followup_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=stateful_followup is not None,
        )
        if stateful_followup is not None:
            self._store_memory_turn(session_id, stripped, stateful_followup, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, stateful_followup)
            return stateful_followup
        shortcut_response = self._maybe_handle_shortcut(stripped, session_id=session_id)
        if isinstance(shortcut_response, _BrainShortcut):
            shortcut_route, shortcut_reason, shortcut_captured = (
                "brain_shortcut", "shortcut_brain_shortcut", False,
            )
        elif shortcut_response is not None:
            shortcut_route, shortcut_reason, shortcut_captured = (
                "intercepted", "shortcut_matched", True,
            )
        else:
            shortcut_route, shortcut_reason, shortcut_captured = (
                "fall_through", "shortcut_no_match", False,
            )
        self._emit_dispatch_decision(
            handler="shortcut",
            route=shortcut_route,
            reason=shortcut_reason,
            session_id=session_id,
            text=stripped,
            captured=shortcut_captured,
        )
        if isinstance(shortcut_response, _BrainShortcut):
            return self._brain_text_response(
                session_id,
                shortcut_response.text,
                memory_text=shortcut_response.memory_text,
                runtime_channel=runtime_channel,
            )
        if shortcut_response is not None:
            # Store the exchange so the brain has context on subsequent messages.
            self._store_memory_turn(session_id, stripped, shortcut_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, shortcut_response)
            return shortcut_response
        # NlmHandler owns its own narrow classifier and kill switch; keeping it
        # here prevents explicit NotebookLM commands from falling into autonomy.
        try:
            nlm_response = self._nlm_handler.natural_language_response(session_id, stripped)
        except ApprovalPending as exc:
            self._record_pending_tool_approval(
                session_id=session_id,
                user_text=stripped,
                exc=exc,
            )
            return _format_approval_pending(exc)
        if nlm_response is not None:
            nlm_reason = "nlm_intent_classifier_matched"
        elif os.getenv("CLAW_DISABLE_NLM_NATURAL_LANGUAGE", "0") == "1":
            nlm_reason = "disabled_by_flag"
        else:
            nlm_reason = "nlm_intent_no_match"
        self._emit_dispatch_decision(
            handler="nlm_natural_language",
            route="intercepted" if nlm_response is not None else "fall_through",
            reason=nlm_reason,
            session_id=session_id,
            text=stripped,
            captured=nlm_response is not None,
        )
        if nlm_response is not None:
            self._store_memory_turn(session_id, stripped, nlm_response, assistant_limit=4000)
            self._remember_assistant_turn_state(session_id, stripped, nlm_response)
            return nlm_response
        coordinated_response = self._task_handler.maybe_run_coordinated_task(session_id, stripped)
        self._emit_dispatch_decision(
            handler="coordinated_task",
            route="intercepted" if coordinated_response is not None else "fall_through",
            reason=(
                "coordinated_task_matched"
                if coordinated_response is not None
                else "coordinated_task_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=coordinated_response is not None,
        )
        if coordinated_response is not None:
            is_start_ack = _is_autonomous_task_start_ack(coordinated_response)
            if is_start_ack and (runtime_channel or "").strip().lower() == "telegram":
                self.brain.memory.store_message(session_id, "user", stripped)
                return None
            self._store_memory_turn(session_id, stripped, coordinated_response, assistant_limit=4000)
            if not is_start_ack:
                self._remember_assistant_turn_state(session_id, stripped, coordinated_response)
            return coordinated_response
        try:
            command_response = dispatch_commands(self._post_shortcut_commands, context)
        except ApprovalPending as exc:
            self._record_pending_tool_approval(
                session_id=session_id,
                user_text=stripped,
                exc=exc,
            )
            return _format_approval_pending(exc)
        if command_response is not None:
            return command_response

        # No dispatch_decision emit here — the spec is "pre-brain decision
        # boundary only". By construction, reaching this line means every
        # pre-brain handler emitted route="fall_through", and the brain's
        # own observability covers what happens next.
        return self._brain_text_response(session_id, stripped, runtime_channel=runtime_channel)

    def _build_pre_state_commands(self) -> list[BotCommand]:
        return [
            BotCommand("help", self._handle_help_command, exact=("/help",), prefixes=("/help ",)),
            BotCommand("status", self._handle_status_command, exact=("/status",)),
            BotCommand("restart", self._handle_restart_command, exact=("/restart",)),
            BotCommand("config", self._handle_config_command, exact=("/config",)),
            BotCommand("model", self._handle_model_command, exact=("/models", "/model", "/model status"), prefixes=("/model ",)),
            BotCommand("tokens", self._handle_tokens_command, exact=("/tokens",)),
            BotCommand("spending", self._handle_spending_command, exact=("/spending",)),
            BotCommand("freeze", self._handle_freeze_command, exact=("/freeze",)),
            BotCommand("unfreeze", self._handle_unfreeze_command, exact=("/unfreeze",)),
            BotCommand("budget_status", self._handle_budget_status_command, exact=("/budget_status",)),
            BotCommand("quality", self._handle_quality_command, exact=("/quality",)),
            BotCommand("diagnose_task", self._handle_diagnose_task_command, prefixes=("/diagnose_task ",)),
            BotCommand("task_run", self._handle_task_run_command, exact=("/task_run",), prefixes=("/task_run ",)),
            BotCommand("autonomy", self._handle_autonomy_command, exact=("/autonomy", "/autonomy_policy"), prefixes=("/autonomy ",)),
            BotCommand("jobs", self._handle_jobs_command, exact=("/jobs",), prefixes=("/jobs ", "/job_status ", "/job_trace ", "/job_resume ", "/job_cancel ", "/task_resume ", "/task_cancel ")),
            BotCommand("task_state", self._handle_task_state_command, exact=("/tasks", "/task_status", "/task_loop", "/task_queue", "/task_pending", "/session_state"), prefixes=("/task_queue ",)),
            BotCommand("task_transition", self._handle_task_transition_command, exact=("/task_done", "/task_defer"), prefixes=("/task_done ", "/task_defer ")),
            BotCommand("browse", self._handle_browse_command, prefixes=("/browse ",)),
            *self._terminal_handler.commands(),
            *self._chrome_handler.commands(),
            *self._computer_handler.commands(),
            BotCommand("buddy", self._handle_buddy_command, exact=("/buddy", "/buddy card", "/buddy hatch", "/buddy stats"), prefixes=("/buddy rename ",)),
            *self._wiki_handler.commands(),
            BotCommand("playbooks", self._handle_playbook_command, exact=("/playbooks",), prefixes=("/playbook ",)),
            BotCommand("backtest", self._handle_backtest_command, exact=("/backtest",), prefixes=("/backtest ",)),
            BotCommand("grill", self._handle_grill_command, exact=("/grill",), prefixes=("/grill ",)),
            BotCommand("tdd", self._handle_tdd_command, exact=("/tdd",), prefixes=("/tdd ",)),
            BotCommand("improve_arch", self._handle_improve_arch_command, exact=("/improve_arch",), prefixes=("/improve_arch ",)),
            BotCommand("effort", self._handle_effort_command, exact=("/effort",), prefixes=("/effort ",)),
            BotCommand("verify", self._handle_verify_command, exact=("/verify",), prefixes=("/verify ",)),
            BotCommand("focus", self._handle_focus_command, exact=("/focus",)),
            BotCommand("voice", self._handle_voice_command, exact=("/voice",), prefixes=("/voice ",)),
            *self._design_handler.commands(),
            *(self._checkpoint_handler.commands() if self._checkpoint_handler is not None else []),
        ]

    def _build_post_shortcut_commands(self) -> list[BotCommand]:
        return [
            *self._agent_handler.commands(),
            BotCommand("approvals", self._handle_approvals_command, exact=("/approvals",), prefixes=("/approval_status ", "/approve ", "/task_approve ", "/task_abort ")),
            BotCommand("traces", self._handle_traces_command, exact=("/traces",), prefixes=("/traces ", "/trace ")),
            BotCommand("feedback", self._handle_feedback_command, exact=("/feedback",), prefixes=("/feedback ",)),
            BotCommand("pipeline", self._handle_pipeline_command, exact=("/pipeline", "/pipeline_approve", "/pipeline_merge", "/pipeline_status"), prefixes=("/pipeline_approve ", "/pipeline_merge ", "/pipeline_merge_confirm ", "/pipeline ")),
            BotCommand("social", self._handle_social_command, exact=("/social_preview", "/social_publish", "/social_status"), prefixes=("/social_preview ", "/social_publish ", "/social_approve ")),
            *self._nlm_handler.commands(),
        ]

    def _build_pre_brain_routes(self) -> list[Route]:
        """Semantic dispatch routes (incremental migration of the 15 legacy
        handlers in handle_text). Order matches the legacy chain — each
        migrated handler keeps its slot until all of them live here.
        """
        return [
            Route("operational_alert", self._route_operational_alert),
            Route("boot_context_status", self._route_boot_context_status),
        ]

    def _route_operational_alert(self, ctx: RouteContext) -> RouteOutcome:
        response = self._maybe_handle_operational_alert(ctx.stripped, session_id=ctx.session_id)
        if response is None:
            return RouteOutcome.fall_through(reason="operational_alert_no_match")
        return RouteOutcome.intercepted(response, reason="operational_alert_matched")

    def _route_boot_context_status(self, ctx: RouteContext) -> RouteOutcome:
        response = self._maybe_handle_boot_context_status(
            ctx.stripped,
            session_id=ctx.session_id,
            runtime_channel=ctx.runtime_channel,
        )
        if response is None:
            return RouteOutcome.fall_through(reason="boot_context_status_no_match")
        return RouteOutcome.intercepted(
            response,
            reason="boot_context_status_matched",
            store_memory_limit=3000,
        )

    def _emit_route_decision(self, name: str, outcome: RouteOutcome, ctx: RouteContext) -> None:
        self._emit_dispatch_decision(
            handler=name,
            route=outcome.route,
            reason=outcome.reason,
            session_id=ctx.session_id,
            text=ctx.stripped,
            captured=outcome.captured,
        )

    def _post_capture_intercepted(
        self,
        session_id: str,
        stripped: str,
        response: str,
        *,
        assistant_limit: int = 2000,
    ) -> None:
        self._store_memory_turn(session_id, stripped, response, assistant_limit=assistant_limit)
        self._remember_assistant_turn_state(session_id, stripped, response)

    def _handle_help_command(self, context: CommandContext) -> str:
        if context.stripped == "/help":
            return self._help_response()
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return self._help_response()
        return self._help_response(parts[1])

    def _handle_status_command(self, context: CommandContext) -> str:
        return self._format_status_summary(self.heartbeat.collect())

    @staticmethod
    def _format_status_summary(snapshot: HeartbeatSnapshot) -> str:
        agents = snapshot.agents or {}
        active_agents = sum(1 for info in agents.values() if not info.get("paused"))
        paused_agents = len(agents) - active_agents
        warning_agents = [
            f"{name}:{health}"
            for name, info in sorted(agents.items())
            if (health := _compute_health(info)) != "OK"
        ]
        metrics = snapshot.lane_metrics or {}
        invocations = sum(int(item.get("invocations") or 0) for item in metrics.values())
        degraded = sum(int(item.get("degraded_invocations") or 0) for item in metrics.values())
        total_cost = sum(float(item.get("total_cost") or 0.0) for item in metrics.values())

        lines = [
            "Estoy vivo.",
            f"Aprobaciones: {snapshot.pending_approvals} pendientes.",
        ]
        if warning_agents:
            suffix = "" if len(warning_agents) <= 3 else f" (+{len(warning_agents) - 3})"
            lines.append(
                f"Agentes: {active_agents} activos, {paused_agents} pausados; "
                f"alertas: {', '.join(warning_agents[:3])}{suffix}."
            )
        else:
            lines.append(f"Agentes: {active_agents} activos, {paused_agents} pausados; alertas: ninguna.")
        if invocations:
            degraded_text = f", {degraded} degradadas" if degraded else ""
            lines.append(f"Uso hoy: {invocations} llamadas, ${total_cost:.4f}{degraded_text}.")
        else:
            lines.append("Uso hoy: sin llamadas registradas.")
        lines.append("Más detalle: `/approvals`, `/jobs`, `/tasks`, `/budget_status`.")
        return "\n".join(lines)

    def _handle_restart_command(self, context: CommandContext) -> str:
        if self.observe is not None:
            try:
                self.observe.emit(
                    "telegram_restart_requested",
                    payload={
                        "session_id": context.session_id,
                        "user_id": context.user_id,
                        "pid": os.getpid(),
                    },
                )
            except Exception:
                logger.exception("failed to emit telegram_restart_requested")
        thread = threading.Thread(
            target=self._delayed_self_restart,
            daemon=True,
            name="telegram-restart",
        )
        thread.start()
        return "Reiniciando Claw producción. Si launchd está activo, vuelvo en unos segundos."

    @staticmethod
    def _delayed_self_restart(delay_seconds: float = 2.0) -> None:
        time.sleep(delay_seconds)
        os.kill(os.getpid(), signal.SIGTERM)

    def _handle_config_command(self, context: CommandContext) -> str:
        if self.config is None:
            return "config not available"
        c = self.config
        overrides = model_overrides_from_state(self.brain.memory.get_session_state(context.session_id))
        lanes = {}
        for lane in ("brain", "worker", "worker_heavy", "verifier", "research", "judge"):
            override = overrides.get(lane)
            provider = override.provider if override else c.provider_for_lane(lane)
            model = override.model if override else c.model_for_lane(lane)
            ref = override or self.model_registry.resolve(f"{provider}:{model}")
            lanes[lane] = {
                "provider": provider,
                "model": model,
                "effort": override.effort if override and override.effort else c.effort_for_lane(lane),
                "billing": ref.billing,
                "override": override is not None,
                "context_window": c.context_window_for_lane(lane),
                "max_output": c.max_output_for_lane(lane),
            }
        return json.dumps({"lanes": lanes, "max_budget_usd": c.max_budget_usd, "daily_token_budget": c.daily_token_budget}, indent=2)

    def _handle_model_command(self, context: CommandContext) -> str:
        if self.config is None:
            return "config not available"
        if context.stripped == "/models":
            payload = [model.to_dict() for model in self.model_registry.list_models()]
            return json.dumps(payload, indent=2, sort_keys=True)
        if context.stripped in {"/model", "/model status"}:
            return json.dumps(self._model_status_payload(context.session_id), indent=2, sort_keys=True)
        parts = context.stripped.split()
        if len(parts) < 2:
            return "Uso: /model status | /models | /model set <lane> <provider:model> [effort=low|medium|high|xhigh|max] | /model clear <lane>"
        action = parts[1].lower()
        if action == "status":
            return json.dumps(self._model_status_payload(context.session_id), indent=2, sort_keys=True)
        if action == "clear":
            if len(parts) < 3:
                return "Uso: /model clear <lane>"
            try:
                lane = normalize_model_lane(parts[2])
            except ValueError as exc:
                return str(exc)
            overrides = model_overrides_from_state(self.brain.memory.get_session_state(context.session_id))
            overrides.pop(lane, None)
            self._store_model_overrides(context.session_id, overrides)
            return json.dumps(self._model_status_payload(context.session_id), indent=2, sort_keys=True)
        if action != "set":
            return f"Acción inválida: {action}"
        if len(parts) < 4:
            return "Uso: /model set <lane> <provider:model> [effort=low|medium|high|xhigh|max]"
        try:
            lane = normalize_model_lane(parts[2])
        except ValueError as exc:
            return str(exc)
        selector = parts[3]
        effort = None
        for item in parts[4:]:
            if item.startswith("effort="):
                effort = item.split("=", maxsplit=1)[1].strip().lower()
        try:
            override = self.model_registry.override_from_selector(selector, effort=effort)
        except ValueError as exc:
            return str(exc)
        overrides = model_overrides_from_state(self.brain.memory.get_session_state(context.session_id))
        overrides[lane] = override
        self._store_model_overrides(context.session_id, overrides)
        warning = ""
        if override.billing == "api":
            warning = "\nAviso: `openai:*` usa API billing. Para suscripción ChatGPT/Codex usa `codex:<modelo>`."
        return (
            f"Modelo para {lane}: `{override.key}`\n"
            f"Billing: {override.billing}\n"
            f"Effort: {override.effort or self.config.effort_for_lane(lane)}"
            f"{warning}"
        )

    def _handle_tokens_command(self, context: CommandContext) -> str:
        return self._tokens_info_response(context.session_id)

    def _model_status_payload(self, session_id: str) -> dict[str, Any]:
        if self.config is None:
            return {"error": "config not available"}
        overrides = model_overrides_from_state(self.brain.memory.get_session_state(session_id))
        lanes: dict[str, Any] = {}
        for lane in ("brain", "worker", "worker_heavy", "research", "verifier", "judge"):
            override = overrides.get(lane)
            if override is not None:
                lanes[lane] = {
                    **override.to_dict(),
                    "effective": True,
                    "override": True,
                }
                continue
            provider = self.config.provider_for_lane(lane)
            model = self.config.model_for_lane(lane)
            ref = self.model_registry.resolve(f"{provider}:{model}")
            lanes[lane] = {
                **ref.to_dict(),
                "effort": self.config.effort_for_lane(lane),
                "effective": True,
                "override": False,
            }
        return {
            "lanes": lanes,
            "rules": {
                "openai": "API billing",
                "codex": "ChatGPT subscription via Codex CLI",
            },
        }

    def _store_model_overrides(self, session_id: str, overrides: dict[str, ModelOverride]) -> None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["model_overrides"] = serialize_model_overrides(overrides)
        self.brain.memory.update_session_state(session_id, active_object=active_object)

    def _handle_spending_command(self, context: CommandContext) -> str:
        return self._spending_response()

    def _handle_freeze_command(self, context: CommandContext) -> str:
        if self.observation_window is None:
            return "observation window unavailable"
        self.observation_window.freeze(reason="manual_telegram", actor=f"telegram:{context.user_id}")
        return "Freeze activado. Autoexec queda pausado; el chat sigue disponible."

    def _handle_unfreeze_command(self, context: CommandContext) -> str:
        if self.observation_window is None:
            return "observation window unavailable"
        self.observation_window.unfreeze(actor=f"telegram:{context.user_id}")
        return "Freeze desactivado. Autoexec reactivado."

    def _handle_budget_status_command(self, context: CommandContext) -> str:
        if self.observation_window is None:
            return "observation window unavailable"
        payload = self.observation_window.status_payload()
        return json.dumps(
            {
                "frozen": payload["frozen"],
                "freeze_reason": payload["freeze_reason"],
                "cost_today": payload["cost_today"],
                "daily_budget_cap": payload["daily_budget_cap"],
                "daily_budget_remaining": payload["daily_budget_remaining"],
                "rolling_cost_per_hour": payload["rolling_cost_per_hour"],
                "actions_per_minute": payload["actions_per_minute"],
                "tripped_breakers": payload["tripped_breakers"],
            },
            indent=2,
            sort_keys=True,
        )

    def _handle_quality_command(self, context: CommandContext) -> str:
        return self._quality_response()

    def _handle_diagnose_task_command(self, context: CommandContext) -> str:
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /diagnose_task <task_id>"
        return self._diagnose_task_response(parts[1].strip())

    def _handle_task_run_command(self, context: CommandContext) -> str:
        if context.stripped == "/task_run":
            return "usage: /task_run <objective>"
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /task_run <objective>"
        return self._task_handler.coordinated_task_response(context.session_id, parts[1], forced=True)

    def _handle_autonomy_command(self, context: CommandContext) -> str:
        if context.stripped == "/autonomy":
            return json.dumps(self.brain.memory.get_session_state(context.session_id), indent=2, sort_keys=True)
        if context.stripped == "/autonomy_policy":
            return json.dumps(_autonomy_policy_payload(self.brain.memory.get_session_state(context.session_id)), indent=2, sort_keys=True)
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /autonomy <manual|assisted|autonomous>"
        try:
            return self._set_autonomy_mode_response(context.session_id, parts[1])
        except ValueError as exc:
            return str(exc)

    def _handle_task_state_command(self, context: CommandContext) -> str:
        state = self.brain.memory.get_session_state(context.session_id)
        if context.stripped == "/tasks" and self.task_ledger is not None:
            payload = {
                "summary": self.task_ledger.summary(session_id=context.session_id),
                "tasks": [
                    task.to_dict()
                    for task in self.task_ledger.list(session_id=context.session_id, limit=20)
                ],
            }
            return json.dumps(payload, indent=2, sort_keys=True)
        if context.stripped == "/task_status" and self.task_ledger is not None:
            return json.dumps(self.task_ledger.summary(session_id=context.session_id), indent=2, sort_keys=True)
        if context.stripped in {"/tasks", "/task_status", "/task_loop"}:
            return json.dumps(state, indent=2, sort_keys=True)
        if context.stripped == "/task_queue":
            return json.dumps(state.get("task_queue") or [], indent=2, sort_keys=True)
        if context.stripped.startswith("/task_queue "):
            parts = context.stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /task_queue [mode]"
            return json.dumps(_filter_task_queue_by_mode(state.get("task_queue") or [], parts[1]), indent=2, sort_keys=True)
        if context.stripped == "/task_pending":
            return json.dumps(state.get("pending_approvals") or [], indent=2, sort_keys=True)
        return json.dumps(state, indent=2, sort_keys=True)

    def _handle_jobs_command(self, context: CommandContext) -> str:
        if self.task_ledger is None:
            return "task ledger unavailable"
        parts = context.stripped.split()
        command = parts[0]
        if command == "/jobs":
            include_all = len(parts) > 1 and parts[1].lower() == "all"
            session_id = None if include_all else context.session_id
            payload = {
                "summary": self.task_ledger.summary(session_id=session_id),
                "jobs": [
                    task.to_dict()
                    for task in self.task_ledger.list(session_id=session_id, limit=20)
                ],
            }
            if self.job_service is not None:
                payload["system_summary"] = self.job_service.summary()
                payload["system_jobs"] = [
                    job.to_dict()
                    for job in self.job_service.list(limit=20)
                ]
            return json.dumps(payload, indent=2, sort_keys=True)
        if command == "/job_status":
            if len(parts) != 2:
                return "usage: /job_status <task_id>"
            record = self.task_ledger.get(parts[1])
            if record is not None:
                payload = {"source": "task_ledger", **record.to_dict()}
            elif self.job_service is not None and (job := self.job_service.get(parts[1])) is not None:
                payload = {"source": "job_service", **job.to_dict()}
            else:
                payload = {"error": "job not found"}
            return json.dumps(payload, indent=2, sort_keys=True)
        if command == "/job_trace":
            if len(parts) not in {2, 3}:
                return "usage: /job_trace <task_id> [limit]"
            limit = 100
            if len(parts) == 3:
                try:
                    limit = _parse_positive_int(parts[2], field_name="limit")
                except ValueError as exc:
                    return str(exc)
            return self._job_trace_response(parts[1], limit=limit)
        if command in {"/task_resume", "/job_resume"}:
            if len(parts) != 2:
                return f"usage: {command} <task_id>"
            return self._task_handler.resume_task_response(context.session_id, parts[1])
        if command in {"/task_cancel", "/job_cancel"}:
            if len(parts) != 2:
                return f"usage: {command} <task_id>"
            if command == "/job_cancel" and self.task_ledger.get(parts[1]) is None:
                if self.job_service is None:
                    return "job service unavailable"
                job = self.job_service.get(parts[1])
                if job is None:
                    return f"job {parts[1]} not found"
                linked_task_id = job.payload.get("task_id") if isinstance(job.payload, dict) else None
                if isinstance(linked_task_id, str) and self.task_ledger.get(linked_task_id) is not None:
                    return self._task_handler.cancel_task_response(context.session_id, linked_task_id)
                self.job_service.cancel(parts[1], reason=f"cancelled_by:{context.session_id}")
                return f"Job cancelado: `{parts[1]}`"
            return self._task_handler.cancel_task_response(context.session_id, parts[1])
        return "usage: /jobs | /job_status <task_id> | /task_resume <task_id> | /task_cancel <task_id>"

    def _handle_task_transition_command(self, context: CommandContext) -> str:
        if context.stripped == "/task_done":
            return "usage: /task_done <task_id>"
        if context.stripped == "/task_defer":
            return "usage: /task_defer <task_id>"
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /task_done <task_id>" if context.stripped.startswith("/task_done") else "usage: /task_defer <task_id>"
        to_status = "done" if context.stripped.startswith("/task_done") else "deferred"
        return self._task_handler.task_queue_transition_response(context.session_id, parts[1], to_status=to_status)

    def _handle_browse_command(self, context: CommandContext) -> str:
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /browse <url>"
        return self._browse_handler.browse_response(parts[1], session_id=context.session_id)

    def _handle_buddy_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/buddy hatch":
            return self._buddy_hatch_response(context.user_id)
        if stripped == "/buddy stats":
            return self._buddy_stats_response(context.user_id)
        if stripped.startswith("/buddy rename "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /buddy rename <name>"
            return self._buddy_rename_response(context.user_id, parts[2])
        return self._buddy_card_response(context.user_id)


    def _handle_playbook_command(self, context: CommandContext) -> str:
        playbooks = self.brain.playbooks
        if not playbooks._loaded:
            playbooks.load()
        if context.stripped == "/playbooks":
            if not playbooks.playbooks:
                return "No hay playbooks disponibles."
            lines = [f"- **{pb.name}** (triggers: {', '.join(pb.triggers[:4])})" for pb in playbooks.playbooks]
            return "Playbooks disponibles:\n" + "\n".join(lines)
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /playbook <name>"
        name = parts[1].strip().lower()
        for pb in playbooks.playbooks:
            if pb.name.lower() == name or name in pb.name.lower():
                return f"## {pb.name}\n{pb.content}"
        return f"Playbook no encontrado: {name}\nUsa /playbooks para ver disponibles."

    def _handle_backtest_command(self, context: CommandContext) -> str:
        playbooks = self.brain.playbooks
        if not playbooks._loaded:
            playbooks.load()
        pb_context = ""
        for pb in playbooks.playbooks:
            if "backtest" in pb.name.lower() or "qts" in pb.name.lower():
                pb_context = pb.content
                break
        if context.stripped == "/backtest":
            if pb_context:
                return f"QTS Backtesting listo.\n\nUso: /backtest <instrucción>\nEjemplo: /backtest corre ICT strategy para BTC 1h últimos 30 días\n\n{pb_context[:500]}"
            return "usage: /backtest <instrucción>"
        parts = context.stripped.split(maxsplit=1)
        instruction = parts[1]
        prompt = f"{instruction}\n\n<playbook-context>\n{pb_context}\n</playbook-context>" if pb_context else instruction
        return self._brain_text_response(context.session_id, prompt)

    def _load_skill_content(self, skill_name: str) -> str:
        skill_path = Path(__file__).parent.parent / "skills" / skill_name / "skill.md"
        if not skill_path.is_file():
            return ""
        text = skill_path.read_text(encoding="utf-8")
        # Strip YAML frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:].strip()
        return text

    def _handle_grill_command(self, context: CommandContext) -> str:
        if context.stripped == "/grill":
            return "Uso: /grill <descripción del plan o diseño>\nEjemplo: /grill migrar auth a OAuth2 con refresh tokens"
        parts = context.stripped.split(maxsplit=1)
        skill_content = self._load_skill_content("grill-me")
        prompt = f"<skill-context>\n{skill_content}\n</skill-context>\n\n{parts[1]}" if skill_content else parts[1]
        return self._brain_text_response(context.session_id, prompt)

    def _handle_tdd_command(self, context: CommandContext) -> str:
        if context.stripped == "/tdd":
            return "Uso: /tdd <feature o bug a implementar>\nEjemplo: /tdd agregar validación de email en registro"
        parts = context.stripped.split(maxsplit=1)
        skill_content = self._load_skill_content("tdd")
        prompt = f"<skill-context>\n{skill_content}\n</skill-context>\n\n{parts[1]}" if skill_content else parts[1]
        return self._brain_text_response(context.session_id, prompt)

    def _handle_improve_arch_command(self, context: CommandContext) -> str:
        skill_content = self._load_skill_content("improve-codebase-architecture")
        if context.stripped == "/improve_arch":
            instruction = "Analiza la arquitectura del codebase actual y sugiere mejoras."
        else:
            parts = context.stripped.split(maxsplit=1)
            instruction = parts[1]
        prompt = f"<skill-context>\n{skill_content}\n</skill-context>\n\n{instruction}" if skill_content else instruction
        return self._brain_text_response(context.session_id, prompt)

    _VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def _handle_effort_command(self, context: CommandContext) -> str:
        if self.config is None:
            return "config not available"
        if context.stripped == "/effort":
            return (
                f"Effort actual:\n"
                f"  brain: {self.config.brain_effort}\n"
                f"  worker: {self.config.worker_effort}\n"
                f"  worker_heavy: {self.config.worker_heavy_effort}\n"
                f"  judge: {self.config.judge_effort}\n"
                f"\nUso: /effort <level> [lane]\n"
                f"Niveles: {', '.join(self._VALID_EFFORTS)}\n"
                f"Lanes: brain, worker, worker_heavy, judge (omitir = todas)"
            )
        parts = context.stripped.split()
        level = parts[1].lower() if len(parts) >= 2 else ""
        if level not in self._VALID_EFFORTS:
            return f"Nivel inválido: {level}\nVálidos: {', '.join(self._VALID_EFFORTS)}"
        lane = parts[2].lower() if len(parts) >= 3 else None
        if lane and lane not in ("brain", "worker", "worker_heavy", "judge"):
            return f"Lane inválido: {lane}\nVálidos: brain, worker, worker_heavy, judge"
        if lane:
            setattr(self.config, f"{lane}_effort", level)
        else:
            self.config.brain_effort = level
            self.config.worker_effort = level
            self.config.worker_heavy_effort = level
            self.config.judge_effort = level
        applied = lane or "todas las lanes"
        return f"Effort → **{level}** para {applied}"

    def _handle_verify_command(self, context: CommandContext) -> str:
        playbooks = self.brain.playbooks
        if not playbooks._loaded:
            playbooks.load()
        pb_context = ""
        for pb in playbooks.playbooks:
            if "verification" in pb.name.lower():
                pb_context = pb.content
                break
        if context.stripped == "/verify":
            instruction = (
                "Ejecuta el Verification Pipeline completo sobre el trabajo actual:\n"
                "Phase 1: Tests — corre pytest, reporta resultados\n"
                "Phase 2: Simplify — revisa git diff, busca mejoras de calidad\n"
                "Phase 3: PR — resume y pregunta si crear PR"
            )
        else:
            parts = context.stripped.split(maxsplit=1)
            instruction = f"Ejecuta verification pipeline sobre: {parts[1]}"
        prompt = f"<playbook-context>\n{pb_context}\n</playbook-context>\n\n{instruction}" if pb_context else instruction
        return self._brain_text_response(context.session_id, prompt)

    def _handle_focus_command(self, context: CommandContext) -> str:
        if not hasattr(self, "_focus_sessions"):
            self._focus_sessions: set[str] = set()
        sid = context.session_id
        if sid in self._focus_sessions:
            self._focus_sessions.discard(sid)
            return "Focus mode **desactivado**. Verás trabajo intermedio."
        self._focus_sessions.add(sid)
        return "Focus mode **activado**. Solo verás resultados finales."

    _VALID_VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")

    def _handle_voice_command(self, context: CommandContext) -> str:
        if not hasattr(self, "_voice_sessions"):
            self._voice_sessions: dict[str, str] = {}
        sid = context.session_id
        if context.stripped == "/voice":
            if sid in self._voice_sessions:
                voice = self._voice_sessions.pop(sid)
                return f"Voice mode **desactivado** (era: {voice})."
            self._voice_sessions[sid] = "nova"
            return "Voice mode **activado** (voz: nova). Responderé por audio.\nUsa `/voice <voz>` para cambiar: alloy, echo, fable, onyx, nova, shimmer"
        parts = context.stripped.split(maxsplit=1)
        voice = parts[1].lower().strip()
        if voice == "off":
            self._voice_sessions.pop(sid, None)
            return "Voice mode **desactivado**."
        if voice not in self._VALID_VOICES:
            return f"Voz inválida: {voice}\nVálidas: {', '.join(self._VALID_VOICES)}"
        self._voice_sessions[sid] = voice
        return f"Voice mode **activado** (voz: {voice})."

    def is_voice_mode(self, session_id: str) -> str | None:
        if not hasattr(self, "_voice_sessions"):
            return None
        return self._voice_sessions.get(session_id)

    def _handle_approvals_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/approvals":
            return json.dumps(self.approvals.list_pending(), indent=2, sort_keys=True)
        if stripped.startswith("/approval_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /approval_status <approval_id>"
            return self.approvals.status(parts[1])
        if stripped.startswith("/approve "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /approve <approval_id> <token|CONFIRMO risk_code>"
            approved = self.approvals.approve(parts[1], parts[2])
            return "approval recorded" if approved else "approval rejected"
        if stripped.startswith("/task_approve "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /task_approve <approval_id> <token>"
            return self._task_handler.task_approve_response(parts[1], parts[2])
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /task_abort <approval_id>"
        return self._task_handler.task_abort_response(parts[1])

    def _handle_traces_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/traces":
            return self._traces_response(limit=10)
        if stripped.startswith("/traces "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /traces [limit]"
            try:
                limit = _parse_positive_int(parts[1], field_name="limit")
            except ValueError as exc:
                return str(exc)
            return self._traces_response(limit=limit)
        parts = stripped.split(maxsplit=2)
        if len(parts) < 2:
            return "usage: /trace <trace_id> [limit]"
        limit = 100
        if len(parts) == 3:
            try:
                limit = _parse_positive_int(parts[2], field_name="limit")
            except ValueError as exc:
                return str(exc)
        return self._trace_replay_response(parts[1], limit=limit)

    def _handle_feedback_command(self, context: CommandContext) -> str:
        if self.learning is None:
            return "learning loop not available"
        parts = context.stripped.split(maxsplit=2)
        if len(parts) < 2:
            return "usage: /feedback <positive|negative|note> [outcome_id]"
        rating = parts[1]
        oid = int(parts[2]) if len(parts) == 3 else None
        return self.learning.feedback(oid, rating)

    def _handle_pipeline_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped.startswith("/pipeline_approve "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /pipeline_approve <approval_id> <token|CONFIRMO risk_code>"
            approved = self.approvals.approve(parts[1], parts[2])
            if not approved:
                return "approval rejected"
            if self.pipeline is None:
                return "pipeline service unavailable"
            for run in self.pipeline.list_active():
                if run.approval_id == parts[1]:
                    result = self.pipeline.complete_pipeline(run.issue_id)
                    response = {"status": result.status, "pr_url": result.pr_url}
                    # #7/P2: if a trivial automerge staged a separate
                    # pipeline_merge gate, surface it like /pipeline_merge so the
                    # human can actually confirm the merge (id + token + command),
                    # instead of leaving a silent, hard-to-find second gate.
                    if (
                        self.approvals is not None
                        and result.approval_id
                        and result.approval_id != parts[1]
                    ):
                        try:
                            merge_payload = self.approvals.read(result.approval_id)
                        except FileNotFoundError:
                            merge_payload = {}
                        merge_meta = merge_payload.get("metadata") or {}
                        if (
                            str(merge_payload.get("action", "")).startswith("pipeline_merge:")
                            and merge_payload.get("status") == "pending"
                        ):
                            confirmation = merge_meta.get("required_confirmation") or result.approval_token
                            response["approval_id"] = result.approval_id
                            response["approval_token"] = result.approval_token
                            response["confirm_with"] = (
                                f"/pipeline_merge_confirm {result.approval_id} {confirmation}"
                            )
                            if merge_meta.get("risk_code"):
                                response["risk_code"] = merge_meta["risk_code"]
                    return json.dumps(response, indent=2)
            return "approval recorded but no matching pipeline run found"
        if stripped.startswith("/pipeline_merge_confirm "):
            if self.pipeline is None:
                return "pipeline service unavailable"
            if self.approvals is None:
                return "approval manager unavailable"
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /pipeline_merge_confirm <approval_id> <token>"
            approval_id, token = parts[1], parts[2]
            try:
                payload = self.approvals.read(approval_id)
            except FileNotFoundError:
                return f"approval not found: {approval_id}"
            action = payload.get("action") or ""
            if not action.startswith("pipeline_merge:"):
                return f"approval {approval_id} is not a pipeline_merge approval"
            issue_id = action.split(":", 1)[1]
            approved = self.approvals.approve(approval_id, token)
            if not approved:
                return "approval rejected"
            try:
                run = self.pipeline.merge_and_close(issue_id)
                return json.dumps({"issue": run.issue_id, "status": run.status, "pr_url": run.pr_url}, indent=2)
            except Exception:
                logger.exception("pipeline merge error for %s", issue_id)
                return "merge error — check logs for details"
        if stripped.startswith("/pipeline_merge "):
            if self.pipeline is None:
                return "pipeline service unavailable"
            if self.approvals is None:
                return "approval manager unavailable — cannot merge without approval"
            parts = stripped.split(maxsplit=1)
            issue_id = parts[1].strip()
            try:
                run = self.pipeline._load_run(issue_id)
            except FileNotFoundError:
                return f"pipeline run not found: {issue_id}"
            except Exception:
                logger.exception("pipeline_merge load error for %s", issue_id)
                return "merge error — check logs for details"
            if run.status != "pr_created" or not run.pr_url:
                return json.dumps(
                    {
                        "status": "not_mergeable",
                        "issue": run.issue_id,
                        "current_status": run.status,
                        "pr_url": run.pr_url,
                        "reason": "PR is not in pr_created state or has no URL",
                    },
                    indent=2,
                )
            pending = self.approvals.create(
                action=f"pipeline_merge:{issue_id}",
                summary=f"Merge PR {run.pr_url} for {issue_id}",
                metadata={
                    "tier": "tier3",
                    "issue_id": issue_id,
                    "pr_url": run.pr_url,
                    "branch": run.branch_name,
                },
                risk_basis="pipeline_merge_requires_human_hmac_confirmation",
            )
            approval_payload = self.approvals.read(pending.approval_id)
            approval_metadata = approval_payload.get("metadata") or {}
            return json.dumps(
                {
                    "status": "approval_required",
                    "issue": issue_id,
                    "pr_url": run.pr_url,
                    "approval_id": pending.approval_id,
                    "approval_token": pending.token,
                    "confirm_with": f"/pipeline_merge_confirm {pending.approval_id} {pending.token}",
                    "risk_basis": approval_payload.get("risk_basis"),
                    "diff_summary": approval_metadata.get("diff_summary"),
                    "sensitive_paths": approval_metadata.get("sensitive_paths"),
                    "risk_code": approval_metadata.get("risk_code"),
                },
                indent=2,
            )
        if stripped.startswith("/pipeline_status"):
            if self.pipeline is None:
                return "pipeline service unavailable"
            active = self.pipeline.list_active()
            if not active:
                return "no active pipeline runs"
            return json.dumps([{"issue": r.issue_id, "status": r.status, "branch": r.branch_name} for r in active], indent=2)
        if stripped.startswith("/pipeline "):
            if self.pipeline is None:
                return "pipeline service unavailable"
            parts = stripped.split(maxsplit=2)
            issue_id = parts[1]
            repo_root = Path(parts[2]) if len(parts) == 3 else None
            try:
                run = self.pipeline.process_issue(issue_id, repo_root=repo_root)
                return json.dumps({"issue": run.issue_id, "status": run.status, "branch": run.branch_name, "approval_id": run.approval_id, "approve_command": f"/pipeline_approve {run.approval_id}"}, indent=2)
            except Exception:
                logger.exception("pipeline error for %s", issue_id)
                return "pipeline error — check logs for details"
        return f"usage: {stripped} <argument>"

    def _handle_social_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/social_status":
            if self.content_engine is None:
                return "social content engine unavailable"
            accounts_root = self.content_engine.accounts_root
            accounts = sorted(p.name for p in accounts_root.iterdir() if p.is_dir())
            return json.dumps([{"account": a} for a in accounts], indent=2)
        if stripped.startswith("/social_preview "):
            if self.content_engine is None:
                return "social content engine unavailable"
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                return json.dumps([{"platform": d.platform, "text": d.text, "hashtags": d.hashtags} for d in drafts], indent=2)
            except FileNotFoundError:
                return f"account not found: {account}"
            except Exception:
                logger.exception("social_preview error for %s", account)
                return "error generating preview — check logs"
        if stripped.startswith("/social_publish "):
            if self.content_engine is None or self.social_publisher is None:
                return "social services unavailable"
            if self.approvals is None:
                return "approval manager unavailable — cannot publish without approval"
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
            except FileNotFoundError:
                return f"account not found: {account}"
            except Exception:
                logger.exception("social_publish preview error for %s", account)
                return "error generating drafts — check logs"
            preview = [
                {"platform": d.platform, "text": d.text, "hashtags": list(d.hashtags)}
                for d in drafts
            ]
            pending = self.approvals.create(
                action=f"social_publish:{account}",
                summary=f"Publish {len(drafts)} draft(s) for {account}",
                metadata={
                    "tier": "tier3",
                    "account": account,
                    "drafts": [
                        {
                            "account": d.account,
                            "platform": d.platform,
                            "text": d.text,
                            "hashtags": list(d.hashtags),
                            "media_prompt": d.media_prompt,
                            "scheduled_for": d.scheduled_for,
                        }
                        for d in drafts
                    ],
                },
            )
            return json.dumps(
                {
                    "status": "approval_required",
                    "account": account,
                    "approval_id": pending.approval_id,
                    "approval_token": pending.token,
                    "confirm_with": f"/social_approve {pending.approval_id} {pending.token}",
                    "drafts": preview,
                },
                indent=2,
            )
        if stripped.startswith("/social_approve "):
            if self.content_engine is None or self.social_publisher is None:
                return "social services unavailable"
            if self.approvals is None:
                return "approval manager unavailable"
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /social_approve <approval_id> <token>"
            approval_id, token = parts[1], parts[2]
            from claw_v2.content import PostDraft
            try:
                payload = self.approvals.read(approval_id)
            except FileNotFoundError:
                return f"approval not found: {approval_id}"
            action = payload.get("action") or ""
            if not action.startswith("social_publish:"):
                return f"approval {approval_id} is not a social_publish approval"
            approved = self.approvals.approve(approval_id, token)
            if not approved:
                return "approval rejected"
            try:
                drafts_meta = (payload.get("metadata") or {}).get("drafts") or []
                drafts = [
                    PostDraft(
                        account=d["account"],
                        platform=d["platform"],
                        text=d["text"],
                        hashtags=list(d.get("hashtags") or []),
                        media_prompt=d.get("media_prompt"),
                        scheduled_for=d.get("scheduled_for"),
                    )
                    for d in drafts_meta
                ]
                results = []
                for draft in drafts:
                    with approved_tool_invocation(
                        tool="social.publish",
                        approval_id=approval_id,
                        reason="social_publish_confirmed",
                    ):
                        results.append(self.social_publisher.publish(draft))
                return json.dumps(
                    [{"platform": r.platform, "post_id": r.post_id, "url": r.url} for r in results],
                    indent=2,
                )
            except Exception:
                logger.exception("social_publish execution error for %s", approval_id)
                return "error publishing — check logs"
        return f"usage: {stripped} <argument>"

    def _help_response(self, topic: str | None = None) -> str:
        return _help_response(topic)

    def _brain_text_response(
        self,
        session_id: str,
        text: str,
        *,
        memory_text: str | None = None,
        runtime_channel: str | None = None,
    ) -> str:
        prompt_text = text
        source_text = memory_text or text
        runtime_capability_question = _looks_like_runtime_capability_question(text)
        link_analysis_context = _extract_link_analysis_context(text)
        enriched = _enrich_tweet_urls(text)
        if enriched != text:
            prompt_text = _format_tweet_analysis_prompt(text, enriched)
        if runtime_capability_question:
            prompt_text = _format_runtime_capability_prompt(prompt_text)
        prompt_text = self._with_runtime_capability_context(prompt_text, runtime_channel=runtime_channel)
        pre_turn_message_id = self.brain.memory.last_message_id(session_id)
        try:
            response = self.brain.handle_message(
                session_id,
                prompt_text,
                memory_text=source_text,
                task_type="telegram_message",
            )
        except ApprovalPending as exc:
            self._record_pending_tool_approval(
                session_id=session_id,
                user_text=source_text,
                exc=exc,
            )
            return _format_approval_pending(exc)

        raw_content = response.content or ""
        if self._response_has_internal_trace_suppressed(response):
            content = self._recover_internal_trace_suppression(
                session_id=session_id,
                source_text=source_text,
                response=response,
                runtime_capability_question=runtime_capability_question,
                link_analysis_context=link_analysis_context,
                runtime_channel=runtime_channel,
                pre_turn_message_id=pre_turn_message_id,
            )
            self._remember_assistant_turn_state(session_id, source_text, content)
            return content
        content = self._prepare_visible_brain_content(
            session_id,
            source_text,
            raw_content,
            response=response,
            runtime_capability_question=runtime_capability_question,
            link_analysis_context=link_analysis_context,
        )
        content = self._quality_guard_response(
            session_id,
            source_text,
            content,
            source="brain_fallback",
        )
        # Slice 2 of P0-3+P0-2+P1-6 block: apply the central render+sanitize
        # funnel to the brain path. Must stay inside _brain_text_response so
        # it runs inside the `with meta_introspection_context(...)` set by
        # the meta_introspection_guard caller (bot.py:2570). See
        # INTERNAL_WIRING.md §1 final_render_brain_path_inside_meta_context.
        content = self._final_render(session_id, content)
        if content != raw_content:
            self.brain.memory.replace_latest_assistant_message(session_id, raw_content, content)
        # P0-E: attach the brain tool-use ledger FIRST so the learning
        # outcome can be derived from the ledger's terminal state
        # (success / completed_unverified / failed) instead of being
        # hardcoded to "success" for every non-empty reply.
        ledger_deferred = self._defer_brain_tool_use_ledger(
            session_id=session_id,
            response=response,
            source_text=source_text,
            runtime_channel=runtime_channel,
        )
        if not ledger_deferred:
            self._attach_brain_tool_use_ledger(
                session_id=session_id,
                response=response,
                source_text=source_text,
                runtime_channel=runtime_channel,
            )
        contract_content = self._enforce_background_monitor_contract(
            session_id=session_id,
            user_text=source_text,
            content=content,
            raw_content=raw_content,
        )
        if contract_content != content:
            try:
                self.brain.memory.replace_latest_assistant_message(session_id, content, contract_content)
            except Exception:
                logger.debug("background monitor contract memory replacement failed", exc_info=True)
            content = contract_content
        brain_tool_use_record = (
            None if ledger_deferred else self._lookup_recent_brain_tool_use_record(session_id)
        )
        self._remember_assistant_turn_state(session_id, source_text, content)
        if content == "Recibido. ¿Qué quieres que haga con esto?":
            outcome = self._classify_brain_outcome_value(
                brain_tool_use_record, fallback="failure"
            )
            self._browse_handler._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Bot returned fallback for message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome=outcome,
                error_snippet=(raw_content or "empty_response")[:500],
                lesson="When the brain returns empty output, ask a clarifying question and inspect prompt/context assembly.",
                predicted_confidence=self.brain._last_confidence.get(session_id) or None,
            )
        else:
            outcome = self._classify_brain_outcome_value(
                brain_tool_use_record, fallback="success"
            )
            lesson = (
                "The brain produced a usable reply but the tool-use ledger is unverified; verifier should reconcile."
                if outcome == "usable_reply_unverified"
                else "The brain produced a usable reply for this conversational request."
            )
            self._browse_handler._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Handled message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome=outcome,
                lesson=lesson,
                predicted_confidence=self.brain._last_confidence.get(session_id) or None,
            )
        return content

    def _defer_brain_tool_use_ledger(
        self,
        *,
        session_id: str,
        response: Any,
        source_text: str,
        runtime_channel: str | None,
    ) -> bool:
        channel = (runtime_channel or "").strip().lower()
        if channel != "telegram":
            return False
        if os.getenv("CLAW_TELEGRAM_DEFER_BRAIN_TOOLUSE_LEDGER", "1") == "0":
            return False
        try:
            artifacts = getattr(response, "artifacts", None) or {}
        except Exception:
            artifacts = {}
        trace_id = str(artifacts.get("trace_id") or "")
        if not trace_id:
            return False

        turn_id = current_turn_id()

        def _run() -> None:
            try:
                if turn_id:
                    with turn_id_context(turn_id):
                        self._attach_brain_tool_use_ledger(
                            session_id=session_id,
                            response=response,
                            source_text=source_text,
                            runtime_channel=runtime_channel,
                        )
                else:
                    self._attach_brain_tool_use_ledger(
                        session_id=session_id,
                        response=response,
                        source_text=source_text,
                        runtime_channel=runtime_channel,
                    )
            except Exception:
                logger.exception("deferred brain tool-use ledger failed for %s", session_id)
                observe = getattr(self, "observe", None)
                if observe is not None:
                    try:
                        observe.emit(
                            "brain_tooluse_ledger_deferred_failed",
                            payload={"session_id": session_id, "trace_id": trace_id},
                        )
                    except Exception:
                        logger.debug("brain_tooluse_ledger_deferred_failed emit failed", exc_info=True)

        try:
            threading.Thread(
                target=_run,
                name=f"brain-tooluse-ledger-{trace_id[:8]}",
                daemon=True,
            ).start()
        except Exception:
            logger.exception("could not defer brain tool-use ledger for %s", session_id)
            return False
        observe = getattr(self, "observe", None)
        if observe is not None:
            try:
                observe.emit(
                    "brain_tooluse_ledger_deferred",
                    payload={"session_id": session_id, "trace_id": trace_id},
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_deferred emit failed", exc_info=True)
        return True

    def _lookup_recent_brain_tool_use_record(self, session_id: str) -> Any:
        """P0-E: return the most recent brain-fallback ledger row for this
        session if one was created in the current turn (≤ 60s window).
        Returns None when no brain tool-use ledger row exists yet — meaning
        the turn was a pure chat with no tool calls.
        """
        if getattr(self, "task_ledger", None) is None:
            return None
        try:
            candidates = self.task_ledger.list(session_id=session_id, limit=3)
        except Exception:
            return None
        now = time.time()
        for candidate in candidates:
            if getattr(candidate, "mode", "") != "brain_fallback":
                continue
            updated_at = float(getattr(candidate, "updated_at", 0.0) or 0.0)
            if updated_at <= 0.0 or now - updated_at > 60.0:
                continue
            return candidate
        return None

    @staticmethod
    def _classify_brain_outcome_value(record: Any, *, fallback: str = "success") -> str:
        """P0-E: choose the task_outcomes.outcome value that matches the
        brain tool-use ledger's terminal state.

        Args:
          record: the brain tool-use ``TaskRecord`` (or None when no tools ran).
          fallback: outcome to return when ``record`` is None (i.e. pure chat
            with no tools). Caller may pass ``"failure"`` for empty replies.

        Returns one of: "success", "usable_reply_unverified", "failure".
        Behavioral audit found 144 success rows aligned against 91
        completed_unverified ledger rows; this classifier closes that gap.
        """
        if record is None:
            return fallback
        status = str(getattr(record, "status", "") or "").lower()
        verification = str(getattr(record, "verification_status", "") or "").lower()
        if status == "failed" or verification == "failed":
            return "failure"
        if status == "completed_unverified" or verification in {
            "needs_verification",
            "missing_evidence",
            "unverified",
        }:
            return "usable_reply_unverified"
        if status == "succeeded" and verification in {"passed", "verified", "ok"}:
            return "success"
        # Defensive default: if the brain produced tools and we cannot
        # confidently call them verified, treat the outcome as
        # usable_reply_unverified rather than silently inflating success.
        return "usable_reply_unverified"

    def _attach_brain_tool_use_ledger(
        self,
        *,
        session_id: str,
        response: Any,
        source_text: str,
        runtime_channel: str | None,
    ) -> None:
        """PR 0E — Brain Tool-Use Ledger.

        After a brain fallback turn returns, scan observe events for the
        turn's trace_id. If any tools ran, either attach to an existing
        active task (owner-delegation/coordinator) or create a synthetic
        agent_tasks row so the tool-use leaves a durable audit trail.

        Decision matrix:
          - 0 tool events                  → emit noop, no task created
          - any tool event + active task   → attach (no second task)
          - any tool event + no active task→ create synthetic task; keep it
                                              running until verification passes
          - approval-gated tool blocked    → recorded as
                                              brain_tooluse_ledger_skipped_sensitive

        Never marks an unverified brain fallback as succeeded. The manifest is
        activity evidence, not a verifier pass.
        """
        if self.task_ledger is None or self.observe is None:
            return
        try:
            artifacts = getattr(response, "artifacts", None) or {}
        except Exception:
            artifacts = {}
        trace_id = str(artifacts.get("trace_id") or "")
        if not trace_id:
            return
        try:
            events = self.observe.trace_events(trace_id)
        except Exception:
            logger.exception(
                "brain_tooluse_ledger trace_events failed for trace_id=%s", trace_id
            )
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_observe_failed",
                    payload={"session_id": session_id, "trace_id": trace_id},
                )
            except Exception:
                logger.debug(
                    "brain_tooluse_ledger_observe_failed emit suppressed",
                    exc_info=True,
                )
            return
        tool_events: list[dict[str, Any]] = []
        tool_failure_events: list[dict[str, Any]] = []
        approval_events: list[dict[str, Any]] = []
        for ev in events:
            etype = str(ev.get("event_type") or "")
            if etype == "sdk_post_tool_use":
                tool_events.append(ev)
            elif etype == "sdk_post_tool_use_failure":
                tool_failure_events.append(ev)
            elif etype in {"approval_required", "tool_blocked_by_freeze", "tool_hard_denylist_blocked"}:
                approval_events.append(ev)
        if not tool_events and not tool_failure_events and not approval_events:
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_noop_no_tools",
                    payload={"session_id": session_id, "trace_id": trace_id},
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_noop_no_tools emit failed", exc_info=True)
            return
        # Attach to existing active task if one is present.
        try:
            state = self.brain.memory.get_session_state(session_id)
        except Exception:
            state = {}
        active_object = state.get("active_object") or {} if isinstance(state, dict) else {}
        active_task = active_object.get("active_task") or {} if isinstance(active_object, dict) else {}
        existing_task_id = ""
        if isinstance(active_task, dict):
            existing_task_id = str(active_task.get("task_id") or "")
            existing_status = str(active_task.get("status") or "")
            if existing_task_id and existing_status not in ("running",):
                existing_task_id = ""
        if existing_task_id:
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_attached_existing",
                    payload={
                        "session_id": session_id,
                        "existing_task_id": existing_task_id,
                        "trace_id": trace_id,
                        "tools_count": len(tool_events),
                        "failures_count": len(tool_failure_events),
                    },
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_attached_existing emit failed", exc_info=True)
            return
        # Approval-required tool was blocked — record as skipped/sensitive,
        # do NOT mark success.
        if approval_events and not tool_events and not tool_failure_events:
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_skipped_sensitive",
                    payload={
                        "session_id": session_id,
                        "trace_id": trace_id,
                        "blocked_events": len(approval_events),
                    },
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_skipped_sensitive emit failed", exc_info=True)
            return
        # Synthetic task creation.
        task_id = f"brain-tooluse:{session_id}:{time.time_ns()}"
        started_at_ts = time.time()
        (
            tools_run,
            files_touched,
            commands_run,
            files_read,
            files_written,
            grep_patterns,
            glob_patterns,
            output_summaries,
            first_error,
        ) = self._summarize_brain_tool_events(tool_events, tool_failure_events)
        # PR 0F: scrub secret-shaped tokens out of the user message
        # summary before it lands in metadata or the manifest. The lower
        # `redact_sensitive` only matches well-known providers' shapes;
        # this catches mixed-alphanumeric tokens shaped like opaque
        # session/api credentials (same heuristic PR 0B/0D use).
        source_summary, sensitive_redactions = self._redact_secret_tokens(
            (source_text or "")[:200]
        )
        metadata = {
            "origin": "brain_fallback",
            "brain_tool_use": True,
            "channel": (runtime_channel or "").lower() or None,
            "source_message_summary": source_summary,
            "verification_required": True,
            "manual_handoff_forbidden": bool(active_task),
            "created_by": "brain_tool_use_ledger",
            "trace_id": trace_id,
        }
        task_objective = (
            source_summary
            or f"brain fallback tool-use turn ({len(tool_events)} tool calls, trace {trace_id[:8]})"
        )
        # PR 0F: evidence_manifest is the proof that this brain tool-use
        # turn really did the work it claims. task_completion recognizes
        # the shape and lets the row close terminally without forcing
        # `succeeded+passed` (which we have no right to claim without an
        # explicit verifier).
        evidence_manifest: dict[str, Any] = {
            "version": 1,
            "task_id": task_id,
            "session_id": session_id,
            "origin": "brain_fallback",
            "channel": (runtime_channel or "").lower() or None,
            "user_message_summary": source_summary,
            "started_at": started_at_ts,
            "trace_id": trace_id,
            "tools_run": tools_run[:50],
            "files_read": sorted(files_read)[:50],
            "files_written": sorted(files_written)[:50],
            "files_touched": sorted(files_touched)[:50],
            "commands_run": commands_run[:20],
            "grep_patterns": grep_patterns[:20],
            "glob_patterns": glob_patterns[:20],
            "checks_run": [],
            "outputs_summarized": "\n".join(output_summaries[:20]),
            "tool_event_count": len(tool_events),
            "tool_failure_count": len(tool_failure_events),
            "approval_event_count": len(approval_events),
            "blockers": [],
            "sensitive_redactions_applied": sensitive_redactions > 0,
            "verification_result": "unknown",
        }
        outcome_manifest: dict[str, Any] = {
            "version": 1,
            "task_id": task_id,
            "session_id": session_id,
            "origin": "brain_fallback",
            "channel": (runtime_channel or "").lower() or None,
            "trace_id": trace_id,
            "started_at": started_at_ts,
            "final_outcome": "running",
            "async_jobs": [],
            "pending_async_jobs": [],
            "deliveries": [],
            "verifications": [],
            "blockers": [],
        }

        def _finish_outcome(
            final_outcome: str,
            *,
            blockers: list[str] | None = None,
            verification_result: str | None = None,
        ) -> None:
            outcome_manifest["completed_at"] = time.time()
            outcome_manifest["final_outcome"] = final_outcome
            outcome_manifest["blockers"] = list(blockers or [])
            outcome_manifest["pending_async_jobs"] = []
            if verification_result:
                outcome_manifest["verifications"] = [
                    {"kind": "brain_tooluse_verifier", "result": verification_result}
                ]

        requires_verified_completion = self._brain_tooluse_requires_verified_completion(source_summary)
        brain_artifacts = {
            "evidence_manifest": evidence_manifest,
            "outcome_manifest": outcome_manifest,
            # Legacy top-level fields kept for backwards-compat with the
            # PR 0E tests and any downstream audit query that reads
            # artifacts.tools_run directly.
            "tools_run": tools_run[:50],
            "files_touched": sorted(files_touched)[:50],
            "commands_run": commands_run[:20],
            "trace_id": trace_id,
            "tool_event_count": len(tool_events),
            "tool_failure_count": len(tool_failure_events),
            "approval_event_count": len(approval_events),
            "substeps": self._brain_tooluse_substeps(tool_events, tool_failure_events, approval_events),
        }
        # P0-D: populate `route` so the agent_tasks.channel column is
        # non-NULL. Behavioral audit found 99/100 brain-tooluse rows had
        # channel=NULL because this caller historically omitted `route=`.
        # Channel preference: explicit runtime_channel → infer from session_id
        # prefix → omit (column stays NULL for unknown surfaces).
        route_channel = (runtime_channel or "").strip().lower() or (
            "telegram" if session_id.startswith("tg-") else None
        )
        route_payload: dict[str, Any] = {}
        if route_channel:
            route_payload["channel"] = route_channel
            route_payload["external_session_id"] = session_id
        try:
            self.task_ledger.create(
                task_id=task_id,
                session_id=session_id,
                objective=task_objective,
                runtime=(runtime_channel or "brain_fallback"),
                mode="brain_fallback",
                status="running",
                notify_policy="none",
                route=route_payload,
                metadata=metadata,
                artifacts=brain_artifacts,
            )
        except Exception:
            logger.exception("brain tool-use ledger create failed")
            return
        try:
            self.observe.emit(
                "brain_tooluse_ledger_started",
                payload={
                    "session_id": session_id,
                    "task_id": task_id,
                    "trace_id": trace_id,
                    "tools_count": len(tool_events),
                    "failures_count": len(tool_failure_events),
                },
            )
        except Exception:
            logger.debug("brain_tooluse_ledger_started emit failed", exc_info=True)
        # Decide terminal status.
        if tool_failure_events:
            if requires_verified_completion:
                evidence_manifest["completed_at"] = time.time()
                evidence_manifest["verification_result"] = "failed"
                blockers = [first_error[:200] or "action tool execution failed"]
                evidence_manifest["blockers"] = blockers
                _finish_outcome(
                    "failed",
                    blockers=blockers,
                    verification_result="failed",
                )
                self.task_ledger.mark_terminal(
                    task_id,
                    status="failed",
                    summary="brain action tool-use failed before passed verification",
                    error=first_error[:300] or "action tool execution failed before passed verification",
                    verification_status="failed",
                    artifacts=brain_artifacts,
                )
                try:
                    self.observe.emit(
                        "brain_tooluse_ledger_failed",
                        payload={
                            "task_id": task_id,
                            "error_count": len(tool_failure_events),
                            "first_error_kind": first_error[:60],
                            "action_requires_verification": True,
                        },
                    )
                except Exception:
                    logger.debug("brain_tooluse_ledger_failed emit failed", exc_info=True)
                return
            useful_result = self._brain_response_has_useful_result(response)
            if useful_result:
                evidence_manifest["completed_at"] = time.time()
                evidence_manifest["verification_result"] = "succeeded_with_warnings"
                blockers = [first_error[:200] or "nonfatal tool failure"]
                evidence_manifest["blockers"] = blockers
                _finish_outcome("needs_verification", blockers=blockers)
                self.task_ledger.mark_terminal(
                    task_id,
                    status="completed_unverified",
                    summary=f"brain tool-use completed with warnings: {len(tool_failure_events)} substep failure(s)",
                    error=first_error[:300],
                    verification_status="needs_verification",
                    artifacts=brain_artifacts,
                )
                try:
                    self.observe.emit(
                        "brain_tooluse_ledger_completed_with_warnings",
                        payload={
                            "task_id": task_id,
                            "error_count": len(tool_failure_events),
                            "first_error_kind": first_error[:60],
                            "task_status": "completed_unverified",
                        },
                    )
                except Exception:
                    logger.debug("brain_tooluse_ledger_completed_with_warnings emit failed", exc_info=True)
                return
            evidence_manifest["completed_at"] = time.time()
            evidence_manifest["verification_result"] = "failed"
            blockers = [first_error[:200] or "tool execution failed"]
            evidence_manifest["blockers"] = blockers
            _finish_outcome("failed", blockers=blockers, verification_result="failed")
            self.task_ledger.mark_terminal(
                task_id,
                status="failed",
                summary=f"brain tool-use failed: {first_error[:120]}",
                error=first_error[:300],
                verification_status="failed",
                artifacts=brain_artifacts,
            )
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_failed",
                    payload={
                        "task_id": task_id,
                        "error_count": len(tool_failure_events),
                        "first_error_kind": first_error[:60],
                    },
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_failed emit failed", exc_info=True)
            return
        # B1: when explicitly enabled, verify substantive brain tool-use turns
        # against the artifacts that actually ran instead of the request text.
        performed_mutation = bool(files_written) or bool(commands_run)
        coordinator = getattr(getattr(self, "_task_handler", None), "coordinator", None)
        verify_enabled = bool(
            getattr(getattr(self, "config", None), "brain_tooluse_verify", False)
        )
        should_verify = (
            verify_enabled
            and coordinator is not None
            and (requires_verified_completion or performed_mutation)
        )
        if should_verify:
            lane_overrides: dict[str, dict[str, Any]] | None = None
            try:
                lane_overrides = self._task_handler._lane_model_overrides(session_id)
            except Exception:
                logger.debug("brain_tooluse verifier lane override lookup failed", exc_info=True)
            verdict = verify_brain_tooluse(
                coordinator,
                task_id=task_id,
                objective=source_text,
                files_written=sorted(files_written)[:50],
                commands_run=commands_run[:20],
                output_summaries=output_summaries[:20],
                response_excerpt=str(getattr(response, "content", "") or ""),
                lane_overrides=lane_overrides,
            )
            if verdict == "passed":
                evidence_manifest["completed_at"] = time.time()
                evidence_manifest["verification_result"] = "passed"
                _finish_outcome("passed", verification_result="passed")
                self.task_ledger.mark_terminal(
                    task_id,
                    status="succeeded",
                    summary=f"brain tool-use verified: {len(tool_events)} tool calls",
                    verification_status="passed",
                    artifacts=brain_artifacts,
                )
                try:
                    self.observe.emit(
                        "brain_tooluse_ledger_verified",
                        payload={
                            "task_id": task_id,
                            "task_status": "succeeded",
                            "verification_status": "passed",
                            "tools_count": len(tool_events),
                        },
                    )
                except Exception:
                    logger.debug("brain_tooluse_ledger_verified emit failed", exc_info=True)
                return
            if verdict == "failed":
                evidence_manifest["completed_at"] = time.time()
                evidence_manifest["verification_result"] = "failed"
                _finish_outcome("failed", verification_result="failed")
                self.task_ledger.mark_terminal(
                    task_id,
                    status="failed",
                    summary="brain tool-use failed verification",
                    verification_status="failed",
                    artifacts=brain_artifacts,
                )
                try:
                    self.observe.emit(
                        "brain_tooluse_ledger_verification_failed",
                        payload={
                            "task_id": task_id,
                            "task_status": "failed",
                            "verification_status": "failed",
                            "tools_count": len(tool_events),
                        },
                    )
                except Exception:
                    logger.debug("brain_tooluse_ledger_verification_failed emit failed", exc_info=True)
                return
        if self._brain_tooluse_has_verified_readonly_browser_evidence(
            source_summary,
            response=response,
            tools_run=tools_run,
            files_read=files_read,
            files_written=files_written,
            commands_run=commands_run,
        ):
            evidence_manifest["completed_at"] = time.time()
            evidence_manifest["verification_result"] = "passed_readonly"
            _finish_outcome("passed", verification_result="passed_readonly")
            self.task_ledger.mark_terminal(
                task_id,
                status="succeeded",
                summary=f"brain read-only browser review verified: {len(tool_events)} tool calls",
                verification_status="passed",
                artifacts=brain_artifacts,
            )
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_verified_readonly_browser",
                    payload={
                        "task_id": task_id,
                        "task_status": "succeeded",
                        "verification_status": "passed",
                        "tools_count": len(tool_events),
                    },
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_verified_readonly_browser emit failed", exc_info=True)
            return
        # Tools ran without failure, but no verifier has passed yet.
        # PR2 Checkpoint B: block on executed mutation (files_written /
        # commands_run), not only on the 6 request-text regex. A Write/Edit/Bash
        # turn that ran without a passed verifier must not benign-close as
        # completed_unverified — it closes blocked regardless of the verify flag
        # (the audit found 96% of the backlog had mutating tools while the
        # text-only blocker almost never fired). Read-only turns (no mutation, no
        # action text) still fall through to the conservative completed_unverified
        # close below. See INTERNAL_WIRING brain_tooluse_verify_flag_gated.
        if requires_verified_completion or performed_mutation:
            evidence_manifest["completed_at"] = time.time()
            evidence_manifest["verification_result"] = "blocked"
            blockers = ["passed_verification_missing_for_action"]
            evidence_manifest["blockers"] = blockers
            _finish_outcome("blocked", blockers=blockers, verification_result="blocked")
            self.task_ledger.mark_terminal(
                task_id,
                status="failed",
                summary="brain action tool-use blocked without passed verification",
                error=(
                    "Action-like brain tool-use cannot close without passed "
                    "verification, a durable task/job, or a concrete blocker."
                ),
                verification_status="blocked",
                artifacts=brain_artifacts,
            )
            try:
                self.observe.emit(
                    "brain_tooluse_ledger_blocked_unverified_action",
                    payload={
                        "task_id": task_id,
                        "task_status": "failed",
                        "verification_status": "blocked",
                        "tools_count": len(tool_events),
                    },
                )
            except Exception:
                logger.debug("brain_tooluse_ledger_blocked_unverified_action emit failed", exc_info=True)
            return
        evidence_manifest["completed_at"] = time.time()
        evidence_manifest["verification_result"] = "needs_verification"
        _finish_outcome("needs_verification")
        self.task_ledger.mark_terminal(
            task_id,
            status="completed_unverified",
            summary=f"brain tool-use turn: {len(tool_events)} tool calls (unverified)",
            verification_status="needs_verification",
            artifacts=brain_artifacts,
        )
        try:
            self.observe.emit(
                "brain_tooluse_ledger_needs_verification",
                payload={
                    "task_id": task_id,
                    "task_status": "completed_unverified",
                    "verification_status": "needs_verification",
                    "tools_count": len(tool_events),
                },
            )
        except Exception:
            logger.debug("brain_tooluse_ledger_completed emit failed", exc_info=True)

    @staticmethod
    def _redact_secret_tokens(text: str) -> tuple[str, int]:
        """Scrub secret-shaped tokens (mixed alphanumeric, ≥16 chars,
        no spaces, contains upper/lower/digit) from a text snippet
        before it gets persisted. Returns (redacted_text, redaction_count).
        """
        if not text:
            return text, 0
        redactions = 0
        out_tokens: list[str] = []
        # Preserve original whitespace boundaries roughly — token-level
        # substitution is enough for telemetry summaries.
        for token in text.split(" "):
            if _is_secret_shaped_token(token):
                redactions += 1
                out_tokens.append(f"<REDACTED:secret-shape:len={len(token)}>")
            else:
                out_tokens.append(token)
        return " ".join(out_tokens), redactions

    @staticmethod
    def _brain_response_has_useful_result(response: Any) -> bool:
        content = str(getattr(response, "content", "") or "").strip()
        if not content or content == "(no result)":
            return False
        if content.strip(" .,…!¡?¿-_") == "":
            return False
        return len(content) >= 20

    @classmethod
    def _brain_tooluse_has_verified_readonly_browser_evidence(
        cls,
        source_text: str,
        *,
        response: Any,
        tools_run: list[str],
        files_read: set[str],
        files_written: set[str],
        commands_run: list[str],
    ) -> bool:
        """Allow read-only Instagram/browser research to close as verified.

        The mutation blocker intentionally treats Bash as risky by default.
        This carve-out is narrow: it requires an Instagram review/research
        request, concrete screenshot/JSON evidence, no written files, and no
        shell command shaped like publish/upload/delete/write.
        """
        normalized = _normalize_command_text(source_text or "")
        if "instagram" not in normalized and "insta" not in normalized:
            return False
        publish_markers = (
            "publica",
            "publicaste",
            "postea",
            "sube",
            "subelo",
            "súbelo",
            "compart",
            "publish",
            "share",
            "upload",
        )
        if any(marker in normalized for marker in publish_markers):
            return False
        review_markers = (
            "revisa",
            "repaso",
            "busca",
            "investiga",
            "tips",
            "tendencia",
            "trending",
            "feed",
            "perfil",
            "link",
        )
        if not any(marker in normalized for marker in review_markers):
            return False
        if not cls._brain_response_has_useful_result(response):
            return False
        if files_written:
            return False
        dangerous_command_markers = (
            "publish",
            "share",
            "upload",
            "send",
            "rm ",
            "mv ",
            "cp ",
            "touch ",
            "mkdir ",
            "write",
            "_publish",
            "_share",
        )
        for command in commands_run:
            low = command.lower()
            if any(marker in low for marker in dangerous_command_markers):
                return False
        evidence_paths = [p.lower() for p in files_read]
        has_instagram_artifact = any(
            ("artifacts/ig_feed/" in path or "artifacts/instagram/" in path)
            and (path.endswith(".png") or path.endswith(".jpg") or path.endswith(".jpeg") or path.endswith(".json"))
            for path in evidence_paths
        )
        if not has_instagram_artifact:
            return False
        allowed_tools = {"Read", "Grep", "Glob", "Bash"}
        return bool(tools_run) and set(tools_run).issubset(allowed_tools)

    @staticmethod
    def _brain_tooluse_requires_verified_completion(source_text: str) -> bool:
        """True for user turns that are asking the runtime to complete an
        operational action, not just produce a conversational lookup.

        These turns must not terminally close as ``completed_unverified``:
        they need passed evidence, a durable task/job, or a concrete blocker.
        """
        normalized = _normalize_command_text(source_text or "")
        if not normalized:
            return False
        return any(
            pattern.search(normalized)
            for pattern in _BRAIN_TOOLUSE_VERIFIED_ACTION_PATTERNS
        )

    @staticmethod
    def _tool_payload(event: dict[str, Any]) -> dict[str, Any]:
        raw_payload = event.get("payload")
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except Exception:
                payload = {}
        elif isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            payload = {}
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _brain_tooluse_substeps(
        cls,
        tool_events: list[dict[str, Any]],
        failure_events: list[dict[str, Any]],
        approval_events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        substeps: list[dict[str, Any]] = []
        for event in tool_events:
            payload = cls._tool_payload(event)
            substeps.append(
                {
                    "tool": str(payload.get("tool_name") or payload.get("tool") or "unknown")[:80],
                    "status": "succeeded",
                }
            )
        for event in failure_events:
            payload = cls._tool_payload(event)
            error = str(payload.get("error") or "")[:300]
            reason = "file_too_large" if "exceeds maximum allowed tokens" in error else "tool_failed"
            substeps.append(
                {
                    "tool": str(payload.get("tool_name") or payload.get("tool") or "unknown")[:80],
                    "status": "failed",
                    "reason": reason,
                    "error": error,
                }
            )
        for event in approval_events:
            payload = cls._tool_payload(event)
            substeps.append(
                {
                    "tool": str(payload.get("tool_name") or payload.get("tool") or "unknown")[:80],
                    "status": "blocked",
                    "reason": str(payload.get("reason") or "approval_required")[:120],
                }
            )
        return substeps

    @staticmethod
    def _summarize_brain_tool_events(
        tool_events: list[dict[str, Any]],
        failure_events: list[dict[str, Any]],
    ) -> tuple[
        list[str],
        set[str],
        list[str],
        set[str],
        set[str],
        list[str],
        list[str],
        list[str],
        str,
    ]:
        """Pull (tools_run, files_touched, commands_run, files_read,
        files_written, grep_patterns, glob_patterns, output_summaries,
        first_error) from
        observe rows. Payloads are JSON strings; we parse defensively and
        cap every captured string so secrets can't accidentally bloat the
        artifacts blob (the ledger also runs `redact_sensitive` on write).
        """
        tools_run: list[str] = []
        files_touched: set[str] = set()
        files_read: set[str] = set()
        files_written: set[str] = set()
        commands_run: list[str] = []
        grep_patterns: list[str] = []
        glob_patterns: list[str] = []
        output_summaries: list[str] = []
        first_error = ""
        for ev in list(tool_events) + list(failure_events):
            raw_payload = ev.get("payload")
            if isinstance(raw_payload, str):
                try:
                    payload = json.loads(raw_payload)
                except Exception:
                    payload = {}
            elif isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = {}
            tool_name = str(payload.get("tool_name") or "unknown")[:60]
            tools_run.append(tool_name)
            tool_input = payload.get("tool_input") or {}
            if isinstance(tool_input, dict):
                if tool_name in ("Read", "NotebookEdit"):
                    fp = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if fp:
                        capped = str(fp)[:200]
                        files_touched.add(capped)
                        files_read.add(capped)
                elif tool_name in ("Edit", "Write"):
                    fp = tool_input.get("file_path")
                    if fp:
                        capped = str(fp)[:200]
                        files_touched.add(capped)
                        files_written.add(capped)
                elif tool_name == "Bash":
                    cmd = tool_input.get("command") or tool_input.get("cmd")
                    if cmd:
                        commands_run.append(str(cmd)[:200])
                elif tool_name == "Grep":
                    pattern = tool_input.get("pattern")
                    path = tool_input.get("path")
                    if pattern:
                        grep_patterns.append(str(pattern)[:200])
                    if path:
                        files_touched.add(str(path)[:200])
                elif tool_name == "Glob":
                    pattern = tool_input.get("pattern")
                    if pattern:
                        glob_patterns.append(str(pattern)[:200])
            tool_response = payload.get("tool_response") or {}
            if isinstance(tool_response, dict):
                response_summary = BotService._summarize_tool_response_evidence(
                    tool_name,
                    tool_response,
                )
                if response_summary:
                    output_summaries.append(response_summary)
            err = payload.get("error")
            if err and not first_error:
                first_error = str(err)[:300]
        return (
            tools_run,
            files_touched,
            commands_run,
            files_read,
            files_written,
            grep_patterns,
            glob_patterns,
            output_summaries,
            first_error,
        )

    @staticmethod
    def _summarize_tool_response_evidence(tool_name: str, tool_response: dict[str, Any]) -> str:
        parts = [str(tool_name or "unknown")[:60]]
        if "returncode" in tool_response:
            parts.append(f"returncode={tool_response.get('returncode')}")
        if "is_error" in tool_response:
            parts.append(f"is_error={bool(tool_response.get('is_error'))}")
        markers = tool_response.get("json_markers")
        if isinstance(markers, list) and markers:
            safe_markers = []
            for marker in markers[:3]:
                if isinstance(marker, dict):
                    safe_markers.append(marker)
            if safe_markers:
                parts.append(f"json_markers={json.dumps(safe_markers, sort_keys=True)}")
        if "stdout_chars" in tool_response:
            parts.append(f"stdout_chars={tool_response.get('stdout_chars')}")
        if "stderr_chars" in tool_response:
            parts.append(f"stderr_chars={tool_response.get('stderr_chars')}")
        return "; ".join(parts)

    def _with_runtime_capability_context(self, prompt_text: str, *, runtime_channel: str | None = None) -> str:
        context = self._runtime_capability_context(runtime_channel=runtime_channel)
        if not context:
            return prompt_text
        return f"{context}\n\n# User request\n{prompt_text}"

    def _runtime_capability_context(self, *, runtime_channel: str | None = None) -> str:
        lines = ["# Runtime capability context", "Use this as current local runtime evidence before claiming a capability is unavailable."]
        lines.append("- External identity: Dr. Strange, Hector's local autonomous operator.")
        lines.append("- Claude, Claude Code, Codex, OpenAI, ChatGPT, and provider models are internal tools/providers, not the agent identity.")
        lines.append("- Binding rule: do not answer as Claude Code/Codex/the model; answer as Dr. Strange operating through the verified runtime.")
        if runtime_channel:
            normalized_channel = runtime_channel.strip().lower()
            lines.append(f"- Current inbound channel: {normalized_channel}")
            lines.append("- Runtime process: daemon/local runtime")
            lines.append("- CLI channel active: false" if normalized_channel != "cli" else "- CLI channel active: true")
            lines.append("Rule: Telegram is the Telegram channel; do not describe Telegram as CLI unless the current inbound channel is cli and local evidence confirms it.")
            lines.append("Rule: contexto interno != respuesta externa; summarize source names/status, never print private boot context wholesale.")
        if self._capability_available("chrome_cdp") and self.browser is not None and self.managed_chrome is not None:
            cdp_url = str(getattr(self.managed_chrome, "cdp_url", "") or "")
            detail = f"available ({cdp_url})" if cdp_url else "available"
            lines.append(f"- Chrome CDP: {detail}")
        else:
            reason = self._capability_unavailable_message("chrome_cdp", "unavailable")
            lines.append(f"- Chrome CDP: unavailable{f' - {reason}' if reason else ''}")
        if self._capability_available("browser_use") and self.browser_use is not None:
            lines.append("- Browser automation: available")
        else:
            reason = self._capability_unavailable_message("browser_use", "unavailable")
            lines.append(f"- Browser automation: unavailable{f' - {reason}' if reason else ''}")
        if self._capability_available("computer_use") and self.computer is not None:
            lines.append("- Desktop/screenshot: available")
        else:
            reason = self._capability_unavailable_message("computer_use", "unavailable")
            lines.append(f"- Desktop/screenshot: unavailable{f' - {reason}' if reason else ''}")
        if self._capability_available("computer_control") and self.computer is not None:
            lines.append("- Desktop actions: available")
        else:
            reason = self._capability_unavailable_message("computer_control", "unavailable")
            lines.append(f"- Desktop actions: unavailable{f' - {reason}' if reason else ''}")
        if self.terminal_bridge is not None:
            lines.append("- Terminal bridge: available")
        else:
            lines.append("- Terminal bridge: unavailable")
        lines.append("Rule: do not say 'no tengo acceso/no puedo usar navegador/herramientas' unless the relevant line above is unavailable or a concrete attempted route failed.")
        return "\n".join(lines)

    def _identity_binding_response(self) -> str:
        return (
            "Soy Dr. Strange en el daemon local de Hector. "
            "Claude, Codex, OpenAI y ChatGPT son proveedores o herramientas internas, no mi identidad. "
            "Retomo desde el runtime: ejecuto la accion concreta disponible, pido aprobacion o reporto el bloqueo verificado."
        )

    def _identity_drift_corrected_response(self, content: str) -> str:
        if not _contains_substantive_operational_content(content):
            return self._identity_binding_response()
        parts = re.split(r"(?<=[.!?])\s+|\n+", content)
        # Re-join title abbreviations ("Dr. Strange") that the sentence split
        # severs — otherwise only "...no Dr." is recognized as drift and the
        # trailing "Strange." is orphaned into the cleaned output.
        merged: list[str] = []
        for part in parts:
            if merged and re.search(r"\b(?:Dr|Dra|Sr|Sra|Srta|Mr|Mrs|Ms|St)\.\s*$", merged[-1]):
                merged[-1] = f"{merged[-1]} {part}".strip()
            else:
                merged.append(part)
        kept: list[str] = []
        dropped = False
        for part in merged:
            stripped = part.strip()
            if not stripped:
                continue
            if _looks_like_identity_drift(stripped):
                dropped = True
                continue
            kept.append(stripped)
        if not dropped:
            return self._identity_binding_response()
        cleaned = "\n".join(kept).strip()
        if not cleaned or not _contains_substantive_operational_content(cleaned):
            return self._identity_binding_response()
        # Return the substantive content with the drifting sentences removed and
        # no user-facing announcement. Prepending a "Contexto corregido" header
        # would re-surface the very provider names the persona/redaction policy
        # forbids in chat (msg 1047). The correction is recorded in
        # observe_stream by the caller (identity_drift_guard_triggered).
        return cleaned

    def _operator_handoff_binding_response(self) -> str:
        return (
            "No cierro esto con handoff manual. "
            "Detecte una accion operativa y la respuesta intento delegarte el ultimo paso. "
            "La ruta correcta es: ejecutar desde el runtime, crear una tarea durable, pedir aprobacion "
            "o reportar un bloqueo de capacidad/politica verificado. "
            "No marco la accion como completada sin ejecucion verificable."
        )

    def _record_evidence_gate_explicit_blocker(
        self,
        *,
        session_id: str,
        source_text: str,
        blocked_content: str,
        reason: str,
    ) -> str | None:
        task_id = f"{session_id}:evidence-gate:{time.time_ns()}"
        objective = _compact_summary(source_text, limit=220) or "Evidence gate blocked an unverified action claim"
        artifacts = {
            "action_result": {
                "status": "explicit_blocker",
                "reason": reason,
                "source_message_hash": self._stable_text_hash(source_text),
            },
            "evidence": {
                "gate": {
                    "reason": reason,
                    "blocked_response_preview": blocked_content[:500],
                }
            },
        }
        if self.task_ledger is not None:
            try:
                self.task_ledger.create(
                    task_id=task_id,
                    session_id=session_id,
                    objective=objective,
                    runtime="evidence_gate",
                    mode=_infer_session_mode(source_text),
                    status="running",
                    notify_policy="none",
                    metadata={
                        "origin": "evidence_gate",
                        "reason": reason,
                        "source_message_hash": self._stable_text_hash(source_text),
                    },
                    artifacts=artifacts,
                )
                self.task_ledger.mark_terminal(
                    task_id,
                    status="failed",
                    summary=f"Evidence gate explicit blocker: {reason}",
                    error=reason,
                    verification_status="blocked",
                    artifacts=artifacts,
                )
            except Exception:
                logger.debug("failed to record evidence gate explicit blocker", exc_info=True)
                task_id = None
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["last_action_result"] = {
            "task_id": task_id,
            "status": "explicit_blocker",
            "reason": reason,
            "updated_at": time.time(),
        }
        self.brain.memory.update_session_state(
            session_id,
            verification_status="blocked",
            active_object=active_object,
            last_checkpoint={
                "summary": f"Evidence gate blocked unverified action claim: {reason}",
                "verification_status": "blocked",
                "task_id": task_id,
                "reason": reason,
            },
        )
        self._emit_safe(
            "evidence_gate_explicit_blocker_recorded",
            {
                "session_id": session_id,
                "task_id": task_id,
                "reason": reason,
            },
        )
        return task_id

    def _pending_evidence_response(self, task_id: str | None = None) -> str:
        return "Decime qué disparo y te lo ejecuto con evidencia."

    def _unconfirmed_record_response(self, task_id: str | None = None) -> str:
        return (
            "Corrijo: la búsqueda de dueño falló (tool con exit distinto de cero), "
            "así que NO tengo un registro confirmado ni una fuente verificada. "
            "No baso ninguna decisión de gasto en eso. Decime si reintento el lookup "
            "con evidencia antes de avanzar."
        )

    def _unexecuted_start_response(self, task_id: str | None = None) -> str:
        if task_id:
            return "No arranqué nada todavía. Decime qué disparo y te lo ejecuto con evidencia."
        return "Decime qué disparo y te lo ejecuto con evidencia."

    def _emit_identity_capability_binding_guard(
        self,
        event_type: str,
        session_id: str,
        *,
        reason: str,
        original: str,
        sanitized: str,
    ) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(
                event_type,
                payload={
                    "session_id": session_id,
                    "reason": reason,
                    "original_length": len(original),
                    "sanitized_length": len(sanitized),
                    "runtime_identity": "dr_strange",
                },
            )
        except Exception:
            logger.debug("failed to emit %s", event_type, exc_info=True)

    def _correct_unverified_capability_denial(self, content: str) -> str | None:
        available = []
        if self._capability_available("chrome_cdp") and self.browser is not None and self.managed_chrome is not None:
            cdp_url = str(getattr(self.managed_chrome, "cdp_url", "") or "")
            available.append(f"Chrome/CDP{f' ({cdp_url})' if cdp_url else ''}")
        if self._capability_available("browser_use") and self.browser_use is not None:
            available.append("browser automation")
        if self._capability_available("computer_use") and self.computer is not None:
            available.append("desktop/screenshot")
        if self._capability_available("computer_control") and self.computer is not None:
            available.append("desktop actions")
        if self.terminal_bridge is not None:
            available.append("terminal bridge")
        if not available:
            return None
        return (
            "No cierro esto como falta de acceso sin una verificación real. "
            "Intentaré la acción concreta o te diré el bloqueo verificado."
        )

    def _record_pending_tool_approval(
        self,
        *,
        session_id: str,
        user_text: str,
        exc: ApprovalPending,
    ) -> None:
        self._store_memory_turn(
            session_id,
            user_text,
            _format_approval_pending_for_memory(exc),
            assistant_limit=2000,
        )
        # AM-STATEWR/M16 (2026-06-12): atomic merge — a concurrent worker
        # thread writing active_task must not be overwritten by this turn.
        self.brain.memory.merge_active_object(
            session_id,
            {
                "pending_tool_approval": {
                    "approval_id": exc.approval_id,
                    "tool": exc.tool,
                    "summary": exc.summary,
                    "args_hash": exc.args_hash,
                    "original_text": user_text,
                    "created_at": time.time(),
                }
            },
            pending_action=user_text,
            verification_status="awaiting_tool_approval",
        )

    def _clear_pending_tool_approval(self, session_id: str, approval_id: str | None = None) -> None:
        state = self.brain.memory.get_session_state(session_id)
        pending = (state.get("active_object") or {}).get("pending_tool_approval")
        if approval_id is None or not isinstance(pending, dict) or pending.get("approval_id") == approval_id:
            self.brain.memory.merge_active_object(
                session_id,
                {},
                remove=("pending_tool_approval",),
                pending_action="",
                verification_status="unknown",
            )

    def _handle_pending_tool_approval_grant_response(self, session_id: str, text: str) -> str | None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        pending = active_object.get("pending_tool_approval")
        if not isinstance(pending, dict):
            return None
        approval_id = str(pending.get("approval_id") or "")
        tool = str(pending.get("tool") or "")
        args_hash = str(pending.get("args_hash") or "") or None
        original_text = str(pending.get("original_text") or "").strip()
        if not approval_id or not tool or not original_text:
            return "Hay una aprobación pendiente, pero le falta contexto para reintentar. Reenvíame el objetivo concreto."
        try:
            approval_payload = self.approvals.read(approval_id)
        except FileNotFoundError:
            self._clear_pending_tool_approval(session_id, approval_id)
            return f"La aprobación pendiente `{approval_id}` ya no existe. Reenvíame el objetivo concreto."
        status = str(approval_payload.get("status") or "")
        metadata = approval_payload.get("metadata") or {}
        required_confirmation = str(metadata.get("required_confirmation") or "").strip()
        if required_confirmation:
            if text.strip() != required_confirmation:
                if _looks_like_pending_tool_approval_grant(text) or text.strip().upper().startswith("CONFIRMO"):
                    return (
                        "Esta aprobación toca cambios sensibles. No alcanza con `ok`, `sí` o `dale`.\n"
                        f"Responde exactamente: `{required_confirmation}`"
                    )
                return None
        elif not _looks_like_pending_tool_approval_grant(text):
            return None
        if status == "pending":
            if not self.approvals.approve_confirmation(approval_id, text):
                return "No pude registrar la aprobación pendiente. Usa `/approvals` para revisar el estado."
        elif status != "approved":
            self._clear_pending_tool_approval(session_id, approval_id)
            return f"La aprobación `{approval_id}` está en estado `{status}`. Reenvíame el objetivo si quieres intentarlo de nuevo."
        elif not self.approvals.verify_resolution(approval_payload):
            # AH1 (2026-06-11): the record file lives under a writable root —
            # "approved" on disk only counts if the manager stamped it.
            self._clear_pending_tool_approval(session_id, approval_id)
            return (
                f"La aprobación `{approval_id}` figura aprobada pero no pasó la validación de integridad. "
                "Usa `/approve <id> <token>` con el token original."
            )
        self._clear_pending_tool_approval(session_id, approval_id)
        with approved_tool_invocation(
            tool=tool,
            approval_id=approval_id,
            reason="telegram_owner_followup",
            args_hash=args_hash,
        ):
            result = self._brain_text_response(session_id, original_text, memory_text=original_text)
        return f"Aprobación registrada. Reintenté la acción original.\n\n{result}"

    def _handle_pending_computer_approval_response(
        self,
        session_id: str,
        text: str,
        *,
        semantic_turn: SemanticTurn | None = None,
    ) -> str | None:
        # A clear new task skips this handler — unless the text carries an
        # explicit grant/reject verb: restating the task with "te autorizo"
        # must resume the pending action, not start it over.
        if (
            semantic_turn is not None
            and semantic_turn.intent == "new_task"
            and semantic_turn.clear_goal
            and not _looks_like_computer_approval_grant(text)
            and not _looks_like_computer_approval_reject(text)
        ):
            return None
        pending = self._latest_pending_computer_approval(session_id)
        if pending is None:
            return None
        approval_id = str(pending.get("approval_id") or "")
        if not approval_id:
            return None
        if _looks_like_computer_approval_reject(text):
            return self._computer_handler.action_abort_response(approval_id)
        if not _looks_like_computer_approval_grant(text):
            return None
        return self._computer_handler.action_approve_internal_response(
            approval_id,
            session_id=session_id,
        )

    def _latest_pending_computer_approval(self, session_id: str) -> dict[str, Any] | None:
        try:
            pending_items = self.approvals.list_pending()
        except Exception:
            logger.debug("listing pending computer approvals failed", exc_info=True)
            return None
        matches: list[dict[str, Any]] = []
        for item in pending_items:
            metadata = item.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if metadata.get("kind") != "computer_use":
                continue
            if metadata.get("session_id") != session_id:
                continue
            matches.append(item)
        if not matches:
            return None
        return max(matches, key=lambda item: float(item.get("created_at") or 0.0))

    def _maybe_augment_pre_hook_block(self, content: str) -> str:
        parsed = _parse_pre_hook_block(content)
        if parsed is None or self.observe is None:
            return content
        hook_name, reason = parsed
        try:
            recent = self.observe.recent_events(limit=200)
        except Exception:
            return content
        cutoff_minutes = PRE_HOOK_BLOCK_REPEATED_WINDOW_MINUTES
        # LOW (2026-06-12): the window was announced but never applied — old
        # events among the last 200 inflated the count forever.
        cutoff_ts = time.time() - cutoff_minutes * 60
        same_hook_count = 0
        for event in recent:
            if event.get("event_type") != "llm_pre_hook_blocked":
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("blocked_by") != hook_name:
                continue
            event_ts = _parse_observe_timestamp(event.get("timestamp"))
            if event_ts is not None and event_ts < cutoff_ts:
                continue
            same_hook_count += 1
        if same_hook_count <= PRE_HOOK_BLOCK_REPEATED_THRESHOLD:
            return content
        try:
            self.observe.emit(
                "pre_hook_blocked_repeated",
                payload={
                    "hook": hook_name,
                    "reason": reason,
                    "count": same_hook_count,
                    "window_minutes": cutoff_minutes,
                },
            )
        except Exception:
            logger.exception("failed to emit pre_hook_blocked_repeated")
        alert = (
            f"⚠️ Atención: hook `{hook_name}` está bloqueando llamadas LLM repetidamente "
            f"({same_hook_count} en últimos {cutoff_minutes} min). Razón: {reason}. "
            "Revisa configuración.\n\n"
        )
        return alert + content

    def handle_multimodal(
        self,
        *,
        user_id: str,
        session_id: str,
        content_blocks: list[dict[str, Any]],
        memory_text: str,
        runtime_channel: str | None = None,
    ) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        multimodal_text = self._text_from_multimodal_blocks(content_blocks)
        failure_summary_response = self._maybe_handle_operational_failure_summary(
            multimodal_text,
            session_id=session_id,
        )
        self._emit_dispatch_decision(
            handler="operational_failure_summary",
            route="intercepted" if failure_summary_response is not None else "fall_through",
            reason=(
                "operational_failure_summary_matched"
                if failure_summary_response is not None
                else "operational_failure_summary_no_match"
            ),
            session_id=session_id,
            text=multimodal_text,
            captured=failure_summary_response is not None,
        )
        if failure_summary_response is not None:
            failure_summary_response = self._quality_guard_response(
                session_id,
                multimodal_text,
                failure_summary_response,
                source="operational_failure_summary",
            )
            self._store_memory_turn(
                session_id,
                memory_text or multimodal_text,
                failure_summary_response,
                assistant_limit=3000,
            )
            self._remember_assistant_turn_state(session_id, multimodal_text, failure_summary_response)
            return failure_summary_response
        runtime_context = self._runtime_capability_context(runtime_channel=runtime_channel) if runtime_channel else ""
        prompt_blocks = list(content_blocks)
        if runtime_context:
            prompt_blocks = [{"type": "text", "text": runtime_context}, *prompt_blocks]
        try:
            return self.brain.handle_message(
                session_id,
                prompt_blocks,
                memory_text=memory_text,
                task_type="telegram_message",
            ).content
        except ApprovalPending as exc:
            # Persist the pending approval like the text path does, so the
            # natural follow-up ("sí, dale") can resolve it — image/document
            # turns used to drop it, forcing a manual /approvals lookup.
            self._record_pending_tool_approval(
                session_id=session_id,
                user_text=memory_text or multimodal_text,
                exc=exc,
            )
            return _format_approval_pending(exc)

    @staticmethod
    def _text_from_multimodal_blocks(content_blocks: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()


    def _tokens_info_response(self, session_id: str) -> str:
        if self.observation_window is None:
            return json.dumps({
                "session_id": session_id,
                "token_window": {"available": False},
            }, indent=2, sort_keys=True)
        try:
            status_payload = self.observation_window.status_payload()
        except Exception:
            return json.dumps({
                "session_id": session_id,
                "token_window": {"available": False},
            }, indent=2, sort_keys=True)

        token_window = dict(status_payload.get("token_window") or {})
        usage_pct = float(token_window.get("usage_ratio") or 0.0) * 100

        if token_window.get("hard_limit_reached"):
            status = "critical"
            status_emoji = "🔴"
            recommendation = "Autonomía no read-only congelada hasta que baje la ventana o haya override humano"
        elif token_window.get("soft_limit_reached"):
            status = "warning"
            status_emoji = "🟡"
            recommendation = "Compacta antes de nuevas llamadas grandes"
        else:
            status = "healthy"
            status_emoji = "🟢"
            recommendation = "Ventana de tokens saludable"

        max_output = self.config.brain_max_output if self.config else 128_000
        return json.dumps({
            "session_id": session_id,
            "model": "Claude Opus 4.7 / Sonnet 4.6",
            "max_output": max_output,
            "token_window": {
                **token_window,
                "available": True,
                "usage_percentage": round(usage_pct, 1),
            },
            "status": status,
            "status_display": f"{status_emoji} {status.title()}",
            "recommendation": recommendation,
            "frozen": bool(status_payload.get("frozen")),
            "freeze_reason": status_payload.get("freeze_reason") or "",
        }, indent=2, sort_keys=True)

    def _quality_response(self) -> str:
        ledger_summary: dict[str, int] = {}
        if self.task_ledger is not None:
            try:
                ledger_summary = self.task_ledger.summary()
            except Exception:
                ledger_summary = {}
        verified = int(ledger_summary.get("succeeded", 0))
        pending = int(ledger_summary.get("running", 0)) + int(ledger_summary.get("queued", 0))
        blocked = int(ledger_summary.get("blocked", 0))
        failed = int(ledger_summary.get("failed", 0)) + int(ledger_summary.get("timed_out", 0))
        interrupted = int(ledger_summary.get("lost", 0))
        total = verified + pending + blocked + failed + interrupted
        events: list[dict[str, Any]] = []
        if self.observe is not None:
            try:
                events = self.observe.recent_events(limit=500)
            except Exception:
                events = []
        counts: dict[str, int] = {}
        for event in events:
            kind = event.get("event_type")
            if isinstance(kind, str):
                counts[kind] = counts.get(kind, 0) + 1
        false_success_prevented = counts.get("task_false_success_prevented", 0)
        provider_session_reset = counts.get("provider_session_reset", 0)
        stream_interrupted = counts.get("stream_interrupted_checkpointed", 0)
        llm_fallback = counts.get("llm_fallback", 0)
        pre_hook_blocked_count = counts.get("llm_pre_hook_blocked", 0)
        pre_hook_blocked_repeated_count = counts.get("pre_hook_blocked_repeated", 0)
        capability_route_selected_count = counts.get("capability_route_selected", 0)
        capability_route_blocked_count = counts.get("capability_route_blocked", 0)
        notebook_context_resolved_count = counts.get(
            "notebook_context_resolved", 0
        )
        telegram_imperative_detected_total = counts.get("telegram_imperative_detected", 0)
        telegram_imperative_routed_total = counts.get("telegram_imperative_routed", 0)
        telegram_imperative_executed_total = counts.get("telegram_imperative_executed", 0)
        telegram_imperative_execution_failed_total = counts.get("telegram_imperative_execution_failed", 0)
        telegram_imperative_pending_approval_total = counts.get("telegram_imperative_pending_approval", 0)
        telegram_imperative_blocked_total = counts.get("telegram_imperative_blocked", 0)
        telegram_imperative_clarification_total = counts.get("telegram_imperative_clarification", 0)
        telegram_actionable_no_match_total = counts.get("actionable_no_match", 0) + counts.get("telegram_actionable_no_match", 0)
        brain_fallback_for_actionable_total = counts.get("brain_fallback_for_actionable", 0)
        quality_guard_triggered_total = counts.get("quality_guard_triggered", 0)
        identity_drift_guard_triggered_total = counts.get("identity_drift_guard_triggered", 0)
        capability_binding_guard_triggered_total = counts.get("capability_binding_guard_triggered", 0)
        operator_handoff_guard_triggered_total = counts.get("operator_handoff_guard_triggered", 0)
        owner_delegation_detected_total = counts.get("owner_delegation_match", 0)
        active_mission_resolution_success_total = counts.get("active_mission_resolution_success", 0)
        active_mission_resolution_failed_total = counts.get("active_mission_resolution_failed", 0)
        idle_executor_would_advance_total = counts.get("idle_executor_would_advance", 0)
        idle_executor_did_advance_total = counts.get("idle_executor_did_advance", 0)
        idle_executor_circuit_broke_total = counts.get("idle_executor_circuit_broke", 0)
        pre_hook_top_hooks: dict[str, int] = {}
        for event in events:
            if event.get("event_type") != "llm_pre_hook_blocked":
                continue
            payload = event.get("payload") or {}
            hook = payload.get("blocked_by") if isinstance(payload, dict) else None
            if isinstance(hook, str) and hook:
                pre_hook_top_hooks[hook] = pre_hook_top_hooks.get(hook, 0) + 1
        top_pre_hooks = sorted(pre_hook_top_hooks.items(), key=lambda kv: kv[1], reverse=True)[:3]
        failure_reasons: dict[str, int] = {}
        for event in events:
            if event.get("event_type") not in {
                "task_false_success_prevented",
                "scheduled_job_skipped",
                "self_improve_blocked",
            }:
                continue
            payload = event.get("payload") or {}
            reason = payload.get("reason") if isinstance(payload, dict) else None
            if isinstance(reason, str) and reason:
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
        top_reasons = sorted(failure_reasons.items(), key=lambda kv: kv[1], reverse=True)[:5]
        verified_rate = round(verified / total, 3) if total else 0.0
        payload = {
            "window_hours": 24,
            "tasks": {
                "total": total,
                "verified_success": verified,
                "pending": pending,
                "blocked": blocked,
                "failed": failed,
                "interrupted": interrupted,
            },
            "quality": {
                "verified_success_rate": verified_rate,
                "false_success_prevented": false_success_prevented,
            },
            "top_failure_reasons": [
                {"reason": reason, "count": count}
                for reason, count in top_reasons
            ],
            "provider_health": {
                "provider_session_reset": provider_session_reset,
                "stream_interrupted_checkpointed": stream_interrupted,
                "llm_fallback": llm_fallback,
            },
            "pre_hook_blocks": {
                "count": pre_hook_blocked_count,
                "repeated_count": pre_hook_blocked_repeated_count,
                "top_hooks": [
                    {"hook": hook, "count": count}
                    for hook, count in top_pre_hooks
                ],
            },
            "autonomy_routing": {
                "capability_route_selected_count": capability_route_selected_count,
                "capability_route_blocked_count": capability_route_blocked_count,
                "notebook_context_resolved_count": notebook_context_resolved_count,
                "telegram_imperative_detected_total": telegram_imperative_detected_total,
                "telegram_imperative_routed_total": telegram_imperative_routed_total,
                "telegram_imperative_executed_total": telegram_imperative_executed_total,
                "telegram_imperative_execution_failed_total": telegram_imperative_execution_failed_total,
                "telegram_imperative_pending_approval_total": telegram_imperative_pending_approval_total,
                "telegram_imperative_blocked_total": telegram_imperative_blocked_total,
                "telegram_imperative_clarification_total": telegram_imperative_clarification_total,
                "telegram_actionable_no_match_total": telegram_actionable_no_match_total,
                "brain_fallback_for_actionable_total": brain_fallback_for_actionable_total,
                "quality_guard_triggered_total": quality_guard_triggered_total,
                "identity_drift_guard_triggered_total": identity_drift_guard_triggered_total,
                "capability_binding_guard_triggered_total": capability_binding_guard_triggered_total,
                "operator_handoff_guard_triggered_total": operator_handoff_guard_triggered_total,
                "owner_delegation_detected_total": owner_delegation_detected_total,
                "active_mission_resolution_success_total": active_mission_resolution_success_total,
                "active_mission_resolution_failed_total": active_mission_resolution_failed_total,
                "idle_executor_would_advance_total": idle_executor_would_advance_total,
                "idle_executor_did_advance_total": idle_executor_did_advance_total,
                "idle_executor_circuit_broke_total": idle_executor_circuit_broke_total,
                "diagnostic": (
                    "ok"
                    if brain_fallback_for_actionable_total == 0
                    else "brain_fallback_for_actionable_total_nonzero"
                ),
            },
        }
        return redact_sensitive(json.dumps(payload, indent=2, sort_keys=True), limit=0)

    def _diagnose_task_response(self, task_id: str) -> str:
        if self.task_ledger is None:
            return "task ledger unavailable"
        record = self.task_ledger.get(task_id)
        if record is None:
            return f"task not found: {task_id}"
        status = str(getattr(record, "status", "unknown"))
        verification = str(getattr(record, "verification_status", "unknown") or "unknown")
        objective = str(getattr(record, "objective", "") or "").strip()
        summary = str(getattr(record, "summary", "") or "").strip()
        error = str(getattr(record, "error", "") or "").strip()
        artifacts = dict(getattr(record, "artifacts", {}) or {})
        metadata = dict(getattr(record, "metadata", {}) or {})
        task_kind = str(metadata.get("intent") or metadata.get("task_kind") or "unknown")
        evidence_keys = [
            key for key in (
                "handler_result", "notebook_id", "notebook_title", "review_summary",
                "diff", "test_output", "changed_files", "pr_url", "sources",
                "screenshot_after", "partial_output",
            )
            if artifacts.get(key)
        ]
        last_event = "n/a"
        if self.observe is not None:
            try:
                events = self.observe.recent_events(limit=200)
                for event in events:
                    payload = event.get("payload") or {}
                    if isinstance(payload, dict) and payload.get("task_id") == task_id:
                        last_event = f"{event.get('event_type')}: {payload.get('reason') or payload.get('verification_status') or ''}"
                        break
            except Exception:
                last_event = "n/a"
        can_resume = (
            status in {"running", "lost", "failed", "queued"}
            or verification in {"pending", "missing_evidence", "interrupted", "blocked"}
        )
        lines = [
            f"Task: `{task_id}`",
            f"Estado: {status} / verificación: {verification}",
            f"Task kind: {task_kind}",
            f"Objetivo: {objective[:300] or '(sin objetivo)'}",
        ]
        if evidence_keys:
            lines.append(f"Evidencia presente: {', '.join(evidence_keys)}")
        else:
            lines.append("Evidencia presente: ninguna")
        if verification in {"missing_evidence", "pending"}:
            lines.append("Falta evidencia para cerrarla.")
        if error:
            lines.append(f"Error registrado: {error[:300]}")
        elif summary:
            lines.append(f"Último resumen: {summary[:300]}")
        if last_event != "n/a":
            lines.append(f"Último evento relevante: {last_event}")
        if can_resume:
            lines.append(f"Siguiente paso: `/task_resume {task_id}`")
        return redact_sensitive("\n".join(lines), limit=0)

    def _spending_response(self) -> str:
        if self.observe is None:
            return "observe stream unavailable"
        if hasattr(self.observe, "spending_today"):
            payload = self.observe.spending_today()
        else:
            total = self.observe.total_cost_today()
            payload = {"total": round(float(total), 6), "by_lane": {}, "by_provider": {}, "by_model": {}, "rows": []}
        return json.dumps(payload, indent=2, sort_keys=True)

    def _traces_response(self, *, limit: int) -> str:
        if self.observe is None:
            return "observe stream unavailable"
        events = self.observe.recent_events(limit=max(limit * 10, 50))
        traces: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in events:
            trace_id = event.get("trace_id")
            if not trace_id or trace_id in seen:
                continue
            seen.add(trace_id)
            traces.append(
                {
                    "trace_id": trace_id,
                    "timestamp": event.get("timestamp"),
                    "last_event_type": event.get("event_type"),
                    "lane": event.get("lane"),
                    "provider": event.get("provider"),
                    "model": event.get("model"),
                    "artifact_id": event.get("artifact_id"),
                    "job_id": event.get("job_id"),
                }
            )
            if len(traces) >= limit:
                break
        return json.dumps({"traces": traces}, indent=2, sort_keys=True)

    def _trace_replay_response(self, trace_id: str, *, limit: int) -> str:
        if self.observe is None:
            return "observe stream unavailable"
        events = self.observe.trace_events(trace_id, limit=limit)
        if not events:
            return f"trace not found: {trace_id}"

        html_path: str | None = None
        try:
            from claw_v2.visualizer import TraceVisualizerService
            viz = TraceVisualizerService(self.observe)
            html_path = str(viz.render(trace_id, limit=limit))
        except Exception as exc:
            logger.debug("Trace visualizer failed: %s", exc)

        replay = [
            {
                "timestamp": event["timestamp"],
                "event_type": event["event_type"],
                "lane": event["lane"],
                "provider": event["provider"],
                "model": event["model"],
                "span_id": event["span_id"],
                "parent_span_id": event["parent_span_id"],
                "artifact_id": event["artifact_id"],
                "job_id": event["job_id"],
                "payload": event["payload"],
            }
            for event in events
        ]
        result: dict[str, Any] = {"trace_id": trace_id, "event_count": len(replay), "events": replay}
        if html_path:
            result["html"] = html_path
        return redact_sensitive(json.dumps(result, indent=2, sort_keys=True), limit=0)

    def _job_trace_response(self, job_id: str, *, limit: int) -> str:
        if self.observe is None:
            return "observe stream unavailable"
        if not hasattr(self.observe, "job_events"):
            return "observe stream does not support job replay"
        events = self.observe.job_events(job_id, limit=limit)
        if not events:
            return f"job trace not found: {job_id}"
        replay = [
            {
                "timestamp": event["timestamp"],
                "event_type": event["event_type"],
                "lane": event["lane"],
                "provider": event["provider"],
                "model": event["model"],
                "trace_id": event["trace_id"],
                "span_id": event["span_id"],
                "parent_span_id": event["parent_span_id"],
                "artifact_id": event["artifact_id"],
                "payload": event["payload"],
            }
            for event in events
        ]
        return redact_sensitive(
            json.dumps({"job_id": job_id, "event_count": len(replay), "events": replay}, indent=2, sort_keys=True),
            limit=0,
        )



    # -- Buddy handlers --------------------------------------------------------

    def _buddy_hatch_response(self, user_id: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        state = self.buddy.hatch(user_id)
        return f"{state.species_emoji} Hatched **{state.species_name}** ({state.rarity})!\n{self.buddy.show_card(user_id)}"

    def _buddy_card_response(self, user_id: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        return self.buddy.show_card(user_id)

    def _buddy_stats_response(self, user_id: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        return self.buddy.show_stats(user_id)

    def _buddy_rename_response(self, user_id: str, new_name: str) -> str:
        if not hasattr(self, "buddy") or self.buddy is None:
            return "buddy service not available"
        return self.buddy.rename(user_id, new_name)


    def _set_autonomy_mode_response(self, session_id: str, value: str) -> str:
        mode = _parse_autonomy_mode(value)
        current = self.brain.memory.get_session_state(session_id)
        active_object = dict(current.get("active_object") or {})
        active_object["autonomy_configured"] = {
            "mode": mode,
            "source": "command",
        }
        state = self.brain.memory.update_session_state(
            session_id,
            autonomy_mode=mode,
            step_budget=_default_step_budget(mode),
            active_object=active_object,
        )
        return json.dumps(state, indent=2, sort_keys=True)

    def _handle_autonomy_grant_response(self, session_id: str, text: str) -> str:
        current = self.brain.memory.get_session_state(session_id)
        active_object = dict(current.get("active_object") or {})
        active_object["autonomy_grant"] = {
            "mode": "autonomous",
            "source": text[:500],
            "scope": "development_tasks",
            "allowed_without_phase_approval": ["inspect", "edit", "test", "commit", "push"],
            "still_blocked": ["deploy", "publish", "destructive"],
        }
        active_object["autonomy_configured"] = {
            "mode": "autonomous",
            "source": "natural_language_grant",
        }
        state = self.brain.memory.update_session_state(
            session_id,
            autonomy_mode="autonomous",
            step_budget=_default_step_budget("autonomous"),
            active_object=active_object,
        )
        reply = (
            "Autonomía activada para esta sesión. "
            "Ejecutaré fases de desarrollo sin pedir autorización intermedia: inspección, edición, tests, commit y git push. "
            "Solo pausaré por deploy/publicación, acciones destructivas, credenciales faltantes o bloqueo real de sandbox."
        )
        self._store_memory_turn(session_id, text, reply, assistant_limit=len(reply))
        self._remember_assistant_turn_state(session_id, text, reply)
        if state.get("pending_action"):
            return f"{reply}\nPending action: {state['pending_action']}"
        return reply

    def _ensure_default_autonomy(self, session_id: str) -> None:
        if not session_id.startswith("tg-"):
            return
        current = self.brain.memory.get_session_state(session_id)
        active_object = dict(current.get("active_object") or {})
        if active_object.get("autonomy_configured"):
            return
        active_object["autonomy_configured"] = {
            "mode": "assisted",
            "source": "telegram_default",
        }
        self.brain.memory.update_session_state(
            session_id,
            autonomy_mode="assisted",
            step_budget=_default_step_budget("assisted"),
            active_object=active_object,
        )

    def _remember_user_turn_state(self, session_id: str, text: str) -> None:
        self._state_handler.remember_user_turn_state(session_id, text)

    def _remember_assistant_turn_state(self, session_id: str, user_text: str, reply_text: str) -> None:
        self._state_handler.remember_assistant_turn_state(session_id, user_text, reply_text)
        try:
            self._idle_executor.inspect_turn(session_id=session_id)
        except Exception:
            logger.debug("idle ownership executor inspection failed", exc_info=True)

    def _maybe_resolve_stateful_followup(self, text: str, *, session_id: str) -> str | _BrainShortcut | None:
        return self._state_handler.maybe_resolve_stateful_followup(text, session_id=session_id)

    def _handle_stateful_brain_shortcut(
        self,
        session_id: str,
        user_text: str,
        shortcut: _BrainShortcut,
        *,
        runtime_channel: str | None,
    ) -> str | None:
        is_pending_execution = self._stateful_shortcut_is_pending_execution(shortcut)
        if is_pending_execution and (runtime_channel or "").strip().lower() == "telegram":
            self._emit_safe(
                "stateful_continuation_sent_to_brain",
                {"session_id": session_id, "source_text": user_text[:80]},
            )
        result = self._brain_text_response(
            session_id,
            shortcut.text,
            memory_text=shortcut.memory_text,
            runtime_channel=runtime_channel,
        )
        if is_pending_execution and self.observe is not None:
            try:
                self.observe.emit(
                    "pending_action_execution_completed",
                    payload={"session_id": session_id, "response_length": len(result)},
                )
            except Exception:
                logger.debug("failed to emit pending_action_execution_completed", exc_info=True)
        return result

    @staticmethod
    def _stateful_shortcut_is_pending_execution(shortcut: _BrainShortcut) -> bool:
        return (
            shortcut.text.startswith("Continúa con esta acción pendiente:")
            or shortcut.text.startswith("Continúa con este siguiente paso")
            or shortcut.text.startswith("Continúa con esta acción propuesta previamente:")
        )

    def _maybe_handle_shortcut(self, text: str, *, session_id: str) -> str | _BrainShortcut | None:
        if not text or text.startswith("/"):
            return None

        normalized = _normalize_command_text(text)
        extracted_url = _extract_url_candidate(text)
        normalized_url: str | None = None
        if extracted_url is not None:
            try:
                normalized_url = _normalize_url(extracted_url)
            except ValueError:
                normalized_url = None

        if extracted_url is not None:
            if (
                normalized_url is not None
                and _is_tweet_url(normalized_url)
                and not _looks_like_standalone_url(text, extracted_url)
                and any(token in normalized for token in _TWEET_ANALYSIS_SHORTCUT_TOKENS)
            ):
                self._browse_handler.remember_recent_browse_url(session_id, normalized_url)
                return _BrainShortcut(text)
            if "chrome" in normalized and (any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url)):
                return self._chrome_handler.browse_response(extracted_url, session_id=session_id)
            if normalized_url is not None and (
                _is_local_url(normalized_url)
                or (_has_url_query(normalized_url) and "://" not in extracted_url)
            ):
                return self._browse_handler.browse_response(extracted_url, session_id=session_id)
            if any(token in normalized for token in _LINK_ANALYSIS_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._browse_handler.link_review_shortcut(text, extracted_url, session_id=session_id)
            if any(token in normalized for token in _BROWSE_OPEN_TOKENS):
                # Open/operate intent on a site: a single site goes to the
                # authenticated Chrome CDP path (not jina/markdown); multiple
                # sites hand the full request to the brain so it can open and
                # operate every tab, instead of reading just the first URL (H6).
                if len(_extract_url_candidates(text)) > 1:
                    return _BrainShortcut(text)
                return self._chrome_handler.browse_response(extracted_url, session_id=session_id)
            if any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._browse_handler.browse_response(extracted_url, session_id=session_id)

        if _looks_like_tweet_followup_request(normalized):
            recent_tweet_url = self._browse_handler.recent_tweet_url(session_id)
            if recent_tweet_url is not None:
                return _BrainShortcut(f"{text}\n\n{recent_tweet_url}", memory_text=text)

        chatgpt_response = self._maybe_handle_chatgpt_browser_request(
            text,
            normalized=normalized,
            session_id=session_id,
        )
        if chatgpt_response is not None:
            return chatgpt_response

        if _computer_instruction_requires_actions(text):
            return self._computer_handler.action_response(text, session_id)

        if _looks_like_computer_read_request(normalized):
            return self._computer_handler.computer_response(text, session_id)

        if any(token in normalized for token in ("abre", "abrir", "open", "inicia", "iniciar", "run", "corre")):
            if "terminal" in normalized and "claude" in normalized:
                return self._terminal_handler._open_response("claude", cwd=None)
            if "terminal" in normalized and "codex" in normalized:
                return self._terminal_handler._open_response("codex", cwd=None)

        if "google ads" in normalized or "ads.google.com" in normalized:
            if any(token in normalized for token in ("abre", "abrir", "open", "revisa", "revisa", "revisalo", "review", "check")):
                return self._chrome_handler.browse_response("https://ads.google.com", session_id=session_id)

        return None

    def _maybe_handle_chatgpt_browser_request(
        self,
        text: str,
        *,
        normalized: str,
        session_id: str,
    ) -> str | None:
        if not _looks_like_chatgpt_browser_request(normalized):
            return None
        if _looks_like_chatgpt_interactive_request(normalized):
            return self._computer_handler.action_response(
                _chatgpt_browser_task_instruction(text),
                session_id,
            )
        return self._chrome_handler.chatgpt_new_chat_response(session_id=session_id)

    def _maybe_handle_task_status_question(self, text: str, *, session_id: str) -> str | None:
        if not _looks_like_task_completion_question(text):
            return None
        return self._task_status_question_response(session_id)

    def _maybe_handle_change_status_question(self, text: str, *, session_id: str) -> str | None:
        if not _looks_like_change_status_question(text):
            return None
        return self._change_status_question_response(session_id)

    def _maybe_handle_operational_alert(self, text: str, *, session_id: str) -> str | None:
        if not _looks_like_operational_alert(text):
            return None
        fields = _parse_operational_alert_fields(text)
        title = text.splitlines()[0].split(":", 1)[1].strip() if ":" in text.splitlines()[0] else "unknown"
        severity = fields.get("severidad") or fields.get("severity") or "unknown"
        agent = fields.get("agent") or fields.get("agente") or "unknown"
        reason = fields.get("reason") or fields.get("razon") or "unknown"
        error = fields.get("error") or ""
        if self.observe is not None:
            # LOW (2026-06-12): a failed diagnostic emit must not kill the
            # operational-alert turn.
            try:
                self.observe.emit(
                    "operational_alert_input_handled",
                    payload={
                        "session_id": session_id,
                        "title": title,
                        "severity": severity,
                        "agent": agent,
                        "reason": reason,
                        "error": error[:500],
                    },
                )
            except Exception:
                logger.debug("operational_alert_input_handled emit failed", exc_info=True)
        lines = [
            "Alerta operacional registrada; no la voy a convertir en tarea autónoma.",
            f"Tipo: {title}",
            f"Severidad: {severity}",
        ]
        if agent != "unknown":
            lines.append(f"Agente: {agent}")
        if reason != "unknown":
            lines.append(f"Razón: {reason}")
        if error:
            lines.append(f"Error: {error[:300]}")
        if agent == "perf-optimizer" and reason == "codex_timeout":
            lines.append("Acción: mantener `perf-optimizer` pausado y revisar/reanudar manualmente cuando quieras validar el proveedor.")
        else:
            lines.append("Acción: revisar `/jobs`, `/tasks` o `scripts/diagnose.sh` antes de reintentar.")
        return "\n".join(lines)

    def _classify_task_intent(self, text: str, *, session_id: str) -> dict[str, Any]:
        if _looks_like_operational_alert(text):
            return {"intent": "operational_alert", "should_start_task": False}
        if _looks_like_task_diagnostic_question(text) or _looks_like_short_meta_question(text):
            return {"intent": "failure_diagnostic", "should_start_task": False}
        if _looks_like_task_completion_question(text):
            return {"intent": "status_question", "should_start_task": False}
        if _looks_like_previous_task_followup(text):
            return {"intent": "resume_previous", "should_start_task": False}
        if text.strip().startswith("/"):
            return {"intent": "command", "should_start_task": False}
        return {"intent": "unknown", "should_start_task": True}

    def _maybe_handle_task_intent(self, text: str, *, session_id: str) -> str | None:
        # HOTFIX: brittle canned task intent router over-triggers on generic
        # task-related questions and bypasses the brain. Reversible via env
        # flag CLAW_DISABLE_TASK_INTENT_ROUTER=1 (default ON until evidence-
        # aware classifier is in place).
        # TODO: replace with explicit task_id / recent-context check.
        # Carve-out: if the message contains a literal task_id (e.g.
        # "estado de la task nlm-abc123"), the router IS allowed to capture
        # it even when the canned-disable flag is set. The brain-bypass risk
        # comes from regex over-triggers, not from explicit references.
        if (
            os.getenv("CLAW_DISABLE_TASK_INTENT_ROUTER", "1") == "1"
            and not _has_literal_task_id(text)
        ):
            return None

        intent = self._classify_task_intent(text, session_id=session_id)
        kind = intent["intent"]
        if kind == "status_question":
            return self._task_status_question_response(session_id)
        if kind == "failure_diagnostic":
            return self._task_failure_diagnostic_response(session_id)
        if kind == "resume_previous":
            return self._task_resume_previous_response(session_id)
        return None

    # F3b.1.5 (2026-05-26) — markers that turn a message into an imperative
    # command instead of a status query. Regression of 2026-05-17 fix:
    # mensajes largos con instrucciones + "Procede con esto" deben caer al
    # brain, no al handler de pending-tasks.
    #
    # F3b.1.5.1 (2026-05-26) — "go" eliminado del set: substring matching lo
    # disparaba en falsos positivos como "tengo", "algo", "luego", "pago".
    # Conservamos "go ahead" (8 chars, no ambiguo) y "dale ya" (compuesto).
    # Cualquier marker corto (<= 3 chars) usa word-boundary matching.
    _IMPERATIVE_INTENT_MARKERS: tuple[str, ...] = (
        "procede",
        "procedé",
        "procedere",
        "ejecuta",
        "ejecutalo",
        "ejecutar",
        "hazlo",
        "haz lo",
        "implementa",
        "implementar",
        "aprobado",
        "aprobar",
        "autorizado",
        "ok final",
        "ok final:",
        "arranca",
        "arrancar",
        "arrancalo",
        "continua",
        "continuar",
        "continúa",
        "continualo",
        "marca como done",
        "marca f",
        "marca el",
        "márcalo",
        "marcalo",
        "dale",
        "dale ya",
        "go ahead",
        "publica",
        "publícalo",
        "publicalo",
        "borra",
        "elimina",
        "push",
        "merge",
        "deploy",
        "commit",
        "corre el",
        "corre este",
        "corre los",
        "run the",
        "execute",
        "apply",
    )

    def _has_imperative_intent(self, text: str) -> bool:
        """Detect imperative-command intent that should bypass status handlers.

        F3b.1.5: pending-tasks handler must only fire for explicit status
        queries. Long imperative blocks with "Procede con esto" / "OK final"
        must fall through to the brain.

        F3b.1.5.1: markers ≤ 3 chars use word-boundary regex to avoid
        substring false positives (e.g. "go" inside "tengo", "luego").
        """
        normalized = _normalize_command_text(text)
        # Long messages (>= 300 chars after normalisation) carrying instruction
        # blocks are almost certainly imperatives, not status queries.
        if len(normalized) >= 300:
            return True
        for marker in self._IMPERATIVE_INTENT_MARKERS:
            if len(marker) <= 3:
                # Word-boundary match for short tokens — F3b.1.5.1 safety
                if re.search(rf"(?<![a-záéíóúñ]){re.escape(marker)}(?![a-záéíóúñ])", normalized):
                    return True
            elif marker in normalized:
                return True
        return False

    def _pending_tasks_query_matches(self, text: str) -> bool:
        normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        base_match = (
            ("tareas" in normalized and "pendient" in normalized)
            or self._task_status_overview_query_matches(normalized)
            or compact in {"pendientes", "tareaspendientes", "pendietes", "tareaspendietes"}
        )
        if not base_match:
            return False
        # F3b.1.5 — veto when the message carries imperative intent or is a
        # long instruction block. Emit observability event so the bypass is
        # visible in telemetry.
        if self._has_imperative_intent(text):
            self._emit_dispatcher_fallthrough(
                source="pending_tasks_query_matches",
                reason="imperative_intent_detected",
                text=text,
            )
            return False
        return True

    def _emit_dispatcher_fallthrough(self, *, source: str, reason: str, text: str) -> None:
        """F3b.1.5 — emit observability when a pre-brain dispatcher would
        have captured an imperative message but explicitly falls through."""
        payload = {
            "source": source,
            "reason": reason,
            "text_head": (text or "")[:120],
            "text_len": len(text or ""),
        }
        try:
            self.observe.emit("dispatcher_fallthrough_imperative", payload=payload)
        except Exception:
            logger.debug("dispatcher_fallthrough_imperative emit failed", exc_info=True)

    def _handle_pending_tasks_query(
        self,
        text: str,
        *,
        session_id: str,
        runtime_channel: str | None = None,
    ) -> str:
        if self._task_status_overview_query_matches(text):
            response = self._pending_tasks_summary_response(session_id)
            self._store_memory_turn(session_id, text, response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, text, response)
            self._emit_pending_tasks_synthesis("deterministic_status", session_id=session_id)
            return response
        evidence = self._pending_tasks_evidence_pack(session_id)
        prompt = self._format_pending_tasks_synthesis_prompt(text, evidence=evidence)
        try:
            response = self.brain.handle_message(
                session_id,
                prompt,
                memory_text=text,
                task_type="telegram_message",
            )
            raw_content = response.content or ""
            content = self._prepare_visible_brain_content(
                session_id,
                text,
                raw_content,
                response=response,
                runtime_capability_question=False,
                link_analysis_context=None,
            )
            content = self._quality_guard_response(
                session_id,
                text,
                content,
                source="pending_tasks_synthesis",
            )
            if self._pending_tasks_synthesis_usable(content):
                if content != raw_content:
                    self.brain.memory.replace_latest_assistant_message(session_id, raw_content, content)
                self._remember_assistant_turn_state(session_id, text, content)
                self._emit_pending_tasks_synthesis("brain", session_id=session_id)
                return content
            fallback = self._pending_tasks_summary_response(session_id)
            self.brain.memory.replace_latest_assistant_message(session_id, raw_content, fallback)
            self._remember_assistant_turn_state(session_id, text, fallback)
            self._emit_pending_tasks_synthesis("fallback_unusable_brain", session_id=session_id)
            return fallback
        except ApprovalPending as exc:
            reply = _format_approval_pending(exc)
            self._store_memory_turn(session_id, text, reply, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, text, reply)
            return reply
        except Exception as exc:
            fallback = self._pending_tasks_summary_response(session_id)
            self._store_memory_turn(session_id, text, fallback, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, text, fallback)
            self._emit_pending_tasks_synthesis(
                "fallback_exception",
                session_id=session_id,
                error=str(exc)[:300],
            )
            return fallback

    @staticmethod
    def _task_status_overview_query_matches(text: str) -> bool:
        normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        if compact in {
            "estatusdelastareas",
            "estadodelastareas",
            "statusdelastareas",
            "estatusdetareas",
            "estadodetareas",
            "statusdetareas",
            "estatusdelledger",
            "estadodelledger",
            "statusdelledger",
        }:
            return True
        return (
            any(token in normalized for token in ("estatus", "estado", "status"))
            and any(token in normalized for token in ("tareas", "tasks", "ledger"))
        )

    def _emit_pending_tasks_synthesis(self, mode: str, *, session_id: str, error: str | None = None) -> None:
        payload: dict[str, Any] = {"session_id": session_id, "mode": mode}
        if error:
            payload["error"] = error
        try:
            self.observe.emit("pending_tasks_synthesis", payload=payload)
        except Exception:
            logger.debug("pending_tasks_synthesis emit failed", exc_info=True)

    def _pending_tasks_synthesis_usable(self, content: str) -> bool:
        normalized = _normalize_command_text(content)
        if len(normalized) < 80:
            return False
        if normalized in {"handled", "ok", "recibido"}:
            return False
        return any(
            token in normalized
            for token in ("tarea", "pendient", "aprob", "corriendo", "cola", "bloque")
        )

    def _pending_tasks_evidence_pack(self, session_id: str) -> dict[str, Any]:
        state = self.brain.memory.get_session_state(session_id)
        recent_messages = self.brain.memory.get_recent_messages(session_id, limit=12)
        fact_queries = (
            "pendiente",
            "pending",
            "objetivo",
            "goal",
            "codex",
            "ai lead gen",
            "job search",
            "pachano design",
        )
        facts: list[dict[str, Any]] = []
        seen_fact_keys: set[str] = set()
        for query in fact_queries:
            try:
                hits = self.brain.memory.search_facts(query, limit=5)
            except Exception:
                hits = []
            for hit in hits:
                key = str(hit.get("key") or "")
                if not key or key in seen_fact_keys:
                    continue
                source = str(hit.get("source") or "")
                if key.startswith("soul_update_suggestion.") or source == "learning_loop":
                    continue
                seen_fact_keys.add(key)
                facts.append(
                    {
                        "key": key,
                        "value": str(hit.get("value") or "")[:500],
                        "source": hit.get("source"),
                        "confidence": hit.get("confidence"),
                    }
                )
        approvals: list[dict[str, Any]]
        try:
            approvals = self.approvals.list_pending() if self.approvals is not None else []
        except Exception:
            approvals = []
        approval_audit = self._approval_evidence_summary(approvals)
        records: list[Any] = []
        if self.task_ledger is not None:
            try:
                records = self.task_ledger.list(session_id=session_id, limit=30)
            except Exception:
                records = []
        task_rows = [self._task_record_evidence(record) for record in records[:20]]
        return {
            "session_state": {
                "autonomy_mode": state.get("autonomy_mode"),
                "mode": state.get("mode"),
                "current_goal": state.get("current_goal"),
                "pending_action": state.get("pending_action"),
                "verification_status": state.get("verification_status"),
                "task_queue": state.get("task_queue") or [],
                "last_checkpoint": state.get("last_checkpoint") or {},
                "rolling_summary": str(state.get("rolling_summary") or "")[:1200],
            },
            "approvals": approval_audit,
            "task_ledger": task_rows,
            "memory_facts": facts[:16],
            "recent_messages": [
                {
                    "role": row.get("role"),
                    "content": str(row.get("content") or "")[:700],
                    "created_at": row.get("created_at"),
                }
                for row in recent_messages[-10:]
            ],
        }

    def _approval_evidence_summary(self, approvals: list[dict[str, Any]]) -> dict[str, Any]:
        approvals_for_audit = self._sorted_approvals_for_audit(approvals)
        audit = self._classify_pending_approvals(approvals_for_audit)
        seen_actions: set[str] = set()
        active: list[dict[str, Any]] = []
        duplicate = 0
        stale_or_expired = 0
        for item in approvals_for_audit:
            classification = self._classify_approval(item, seen_actions=seen_actions)
            if classification == "still_needed":
                active.append(
                    {
                        "summary": str(item.get("summary") or item.get("action") or "")[:300],
                        "age_hours": round(self._approval_age_hours(item), 1),
                        "risk_tier": self._approval_risk_tier(item),
                    }
                )
            elif classification == "duplicate":
                duplicate += 1
            else:
                stale_or_expired += 1
        return {
            "total": len(approvals),
            "counts": audit,
            "active": active[:5],
            "duplicate_omitted": duplicate,
            "stale_or_expired_omitted": stale_or_expired,
        }

    def _task_record_evidence(self, record: Any) -> dict[str, Any]:
        try:
            updated_at = float(getattr(record, "updated_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        age_hours = round(max(0.0, (time.time() - updated_at) / 3600.0), 1) if updated_at else None
        return {
            "status": str(getattr(record, "status", "") or ""),
            "verification_status": str(getattr(record, "verification_status", "") or ""),
            "runtime": str(getattr(record, "runtime", "") or ""),
            "objective": str(getattr(record, "objective", "") or "")[:500],
            "summary": str(getattr(record, "summary", "") or "")[:500],
            "error": str(getattr(record, "error", "") or "")[:250],
            "age_hours": age_hours,
            "active": str(getattr(record, "status", "") or "") in {"queued", "running"},
            "actionable_blocker": self._is_pending_task_blocker(record),
        }

    def _format_pending_tasks_synthesis_prompt(self, text: str, *, evidence: dict[str, Any]) -> str:
        return (
            "El usuario preguntó por sus tareas pendientes. Responde como Dr. Strange, "
            "agente operativo, no como un endpoint ni con una plantilla fija.\n\n"
            "Instrucciones:\n"
            "- Consulta la evidencia incluida y la memoria reciente.\n"
            "- Contesta en español natural, breve y lógico.\n"
            "- Distingue entre tareas reales pendientes, aprobaciones que requieren decisión, ruido duplicado e historial cerrado.\n"
            "- Si no hay tareas corriendo, dilo claro sin sonar a error.\n"
            "- No pegues JSON, IDs internos, tokens, ni errores crudos de herramientas.\n"
            "- No inventes tareas. Si algo viene sólo de memoria antigua o inferencia, dilo como contexto, no como pendiente activo.\n"
            "- Si hay una acción concreta razonable, ofrécela en una frase.\n\n"
            f"Pregunta original: {text}\n\n"
            "<evidencia_operativa>\n"
            f"{json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)}\n"
            "</evidencia_operativa>"
        )

    def _pending_tasks_summary_response(self, session_id: str) -> str:
        try:
            approvals = self.approvals.list_pending() if self.approvals is not None else []
        except Exception:
            approvals = []
        records = []
        if self.task_ledger is not None:
            try:
                records = self.task_ledger.list(session_id=session_id, limit=20)
            except Exception:
                records = []
        active = [record for record in records if str(getattr(record, "status", "")) in {"queued", "running"}]
        blocked = [record for record in records if self._is_pending_task_blocker(record)]
        state = self.brain.memory.get_session_state(session_id)
        pending_action = self._pending_action_for_task_summary(state)
        lines: list[str] = []
        if active:
            count = len(active)
            lines.append(f"Ahora mismo tengo {count} tarea{'s' if count != 1 else ''} corriendo o en cola en esta sesion:")
            for record in active[:5]:
                detail = str(getattr(record, "summary", "") or getattr(record, "objective", "") or "sin resumen").strip()
                verification = str(getattr(record, "verification_status", "unknown") or "unknown")
                lines.append(f"- {detail[:180]} ({verification}).")
            if len(active) > 5:
                lines.append(f"- {len(active) - 5} mas omitidas del resumen corto.")
        else:
            lines.append("Ahora mismo no tengo tareas corriendo ni en cola para esta sesion.")
        if blocked:
            lines.append("")
            lines.append("Lo que si necesita accion para poder avanzar:")
            for record in blocked[:5]:
                detail = self._pending_task_blocker_detail(record)
                lines.append(f"- {detail[:220]}")
        if pending_action:
            lines.append("")
            lines.append(f"Tambien tengo una accion pendiente de sesion: {pending_action[:180]}")
        approval_lines = self._pending_tasks_approval_summary_lines(approvals)
        if approval_lines:
            lines.append("")
            lines.extend(approval_lines)
        if not active and not blocked and not pending_action and not approvals:
            lines.append("")
            lines.append("No veo nada que este esperando de mi ahora mismo.")
        return "\n".join(lines)

    @staticmethod
    def _pending_action_for_task_summary(state: dict[str, Any]) -> str:
        pending_action = str(state.get("pending_action") or "").strip()
        if not pending_action:
            return ""
        active_object = state.get("active_object") or {}
        meta = active_object.get("pending_action_meta") if isinstance(active_object, dict) else None
        source = str((meta or {}).get("source") or "") if isinstance(meta, dict) else ""
        normalized = _normalize_command_text(pending_action)
        if source == "assistant_proposal_question" and (
            normalized.startswith("voy ahora con eso")
            or "retome alguna otra de las que quedaron perdidas" in normalized
            or "estatus rapido del ledger" in normalized
            or "resumen del dia" in normalized
        ):
            return ""
        return pending_action

    def _pending_tasks_approval_summary_lines(self, approvals: list[dict[str, Any]]) -> list[str]:
        if not approvals:
            return []
        approvals_for_audit = self._sorted_approvals_for_audit(approvals)
        audit = self._classify_pending_approvals(approvals_for_audit)
        seen_actions: set[str] = set()
        active_items: list[tuple[dict[str, Any], str]] = []
        duplicate_count = 0
        stale_or_expired_count = 0
        for item in approvals_for_audit:
            classification = self._classify_approval(item, seen_actions=seen_actions)
            if classification == "still_needed":
                active_items.append((item, classification))
            elif classification == "duplicate":
                duplicate_count += 1
            else:
                stale_or_expired_count += 1
        lines: list[str] = []
        active_count = len(active_items)
        if active_count:
            lines.append(
                f"Hay {active_count} aprobacion{'es' if active_count != 1 else ''} que si merece{'n' if active_count != 1 else ''} decision:"
            )
        elif approvals:
            lines.append("No veo aprobaciones vivas; solo ruido viejo o duplicado.")
        for item, _classification in active_items[:3]:
            summary = str(item.get("summary") or item.get("action") or "aprobacion pendiente").strip()
            age_hours = self._approval_age_hours(item)
            lines.append(f"- {summary[:180]} (edad ~{age_hours:.1f}h).")
        if len(active_items) > 3:
            lines.append(f"- {len(active_items) - 3} aprobaciones activas adicionales no entran en este resumen.")
        if duplicate_count:
            lines.append(f"Tambien hay {duplicate_count} aprobaciones duplicadas; no las cuento como trabajo pendiente real.")
        if stale_or_expired_count:
            lines.append(f"Ademas hay {stale_or_expired_count} aprobaciones viejas/expiradas fuera del resumen operativo.")
        if duplicate_count or stale_or_expired_count:
            lines.append("Si quieres, puedo limpiarlas con `Limpia aprobaciones duplicadas`.")
        return lines

    def _is_pending_task_blocker(self, record: Any) -> bool:
        status = str(getattr(record, "status", "") or "").lower()
        verification = str(getattr(record, "verification_status", "") or "").lower()
        if not self._is_recent_pending_blocker(record):
            return False
        if status in {"queued", "running", "succeeded", "cancelled", "lost", "timed_out"}:
            return False
        if verification not in {"blocked", "missing_evidence", "pending_approval"}:
            return False
        text = " ".join(
            str(getattr(record, field, "") or "")
            for field in ("error", "summary", "objective")
        ).lower()
        actionable_markers = (
            "waiting_for_user_input",
            "blocked_by_capability",
            "blocked_by_policy",
            "policy_blocked",
            "approval_required",
            "requires_approval",
            "missing_capability",
            "capability",
            "credential",
            "permission",
            "secret",
        )
        return any(marker in text for marker in actionable_markers)

    def _is_recent_pending_blocker(self, record: Any, *, max_age_seconds: float = 24 * 3600) -> bool:
        try:
            updated_at = float(getattr(record, "updated_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            return True
        if updated_at <= 0:
            return True
        return time.time() - updated_at <= max_age_seconds

    def _pending_task_blocker_detail(self, record: Any) -> str:
        error = str(getattr(record, "error", "") or "").strip()
        summary = str(getattr(record, "summary", "") or "").strip()
        objective = str(getattr(record, "objective", "") or "").strip()
        source = error or summary or objective or "bloqueo sin detalle"
        normalized = _normalize_command_text(source)
        if "waiting_for_user_input" in normalized:
            source = re.sub(r"(?i)^waiting_for_user_input:\s*", "", source).strip()
            return f"necesito confirmacion o dato faltante: {source[:180]}"
        if "blocked_by_capability" in normalized or "missing_capability" in normalized or "capability" in normalized:
            return f"bloqueada por capacidad faltante: {source[:180]}"
        if "blocked_by_policy" in normalized or "policy_blocked" in normalized:
            return f"bloqueada por politica: {source[:180]}"
        if "approval_required" in normalized or "requires_approval" in normalized:
            return f"requiere aprobacion explicita: {source[:180]}"
        if "credential" in normalized or "secret" in normalized or "permission" in normalized:
            return f"bloqueada por credenciales/permisos: {source[:180]}"
        return (summary or objective or source)[:220]

    def _maybe_handle_cleanup_status_query(self, text: str, *, session_id: str) -> str | None:
        normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        if compact not in {"limpiaste", "yalimpiaste", "cleaned", "didyouclean", "didyoucleanup"}:
            return None
        state = self.brain.memory.get_session_state(session_id)
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            active_object = {}
        last_result = active_object.get("last_action_result") or {}
        if not isinstance(last_result, dict):
            last_result = {}
        if last_result.get("intent") == "approvals.cleanup_stale_duplicates":
            archived = int(last_result.get("archived_count") or 0)
            kept = int(last_result.get("kept_count") or 0)
            failed = int(last_result.get("failed_count") or 0)
            status = str(last_result.get("status") or "unknown")
            return (
                f"Sí. Última limpieza registrada: `{status}`.\n"
                f"Archivadas: {archived}; conservadas: {kept}; fallidas: {failed}."
            )
        try:
            approvals = self.approvals.list_pending() if self.approvals is not None else []
        except Exception:
            approvals = []
        audit = self._classify_pending_approvals(approvals)
        return (
            "No tengo una limpieza registrada en esta sesión.\n"
            "Estado actual: "
            f"{len(approvals)} aprobaciones pendientes; "
            f"{audit['still_needed']} activas; {audit['stale']} stale; "
            f"{audit['duplicate']} duplicadas."
        )

    def _quality_guard_response(
        self,
        session_id: str,
        user_text: str,
        response: str | None,
        *,
        source: str,
    ) -> str:
        text = str(response or "").strip()
        if text and text.strip(" .,…!¡?¿-_") != "":
            return text
        safe = self._safe_status_response(session_id, reason=f"invalid_response:{source}")
        self._emit_safe(
            "quality_guard_triggered",
            {
                "session_id": session_id,
                "source": source,
                "user_text_preview": user_text[:120],
                "original_length": len(str(response or "")),
            },
        )
        return safe

    def _safe_status_response(self, session_id: str, *, reason: str) -> str:
        web_port = int(getattr(self.config, "web_chat_port", 8765)) if self.config is not None else 8765
        runtime = "vivo" if self._runtime_alive() else "sin respuesta local"
        approvals = []
        try:
            approvals = self.approvals.list_pending() if self.approvals is not None else []
        except Exception:
            approvals = []
        return "\n".join(
            [
                "Estoy vivo, pero reemplacé una respuesta inválida.",
                f"Motivo: {reason}",
                f"Runtime local: {runtime} en :{web_port}",
                f"Aprobaciones pendientes: {len(approvals)}",
            ]
        )

    def _approval_summary_lines(self, approvals: list[dict[str, Any]]) -> list[str]:
        approvals_for_audit = self._sorted_approvals_for_audit(approvals)
        audit = self._classify_pending_approvals(approvals_for_audit)
        lines = [
            (
                "Aprobaciones pendientes: "
                f"{len(approvals)} total; "
                f"{audit['still_needed']} activas; "
                f"{audit['stale']} stale; "
                f"{audit['expired']} expiradas; "
                f"{audit['duplicate']} duplicadas."
            )
        ]
        seen_actions: set[str] = set()
        for item in approvals_for_audit[:5]:
            summary = str(item.get("summary") or item.get("action") or "aprobacion pendiente").strip()
            age_hours = self._approval_age_hours(item)
            classification = self._classify_approval(item, seen_actions=seen_actions)
            lines.append(
                f"- {classification}: {summary[:160]} (edad ~{age_hours:.1f}h). "
                "No auto-apruebo; revisar vigencia antes de aprobar."
            )
        return lines

    def _sorted_approvals_for_audit(self, approvals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(approvals, key=lambda item: self._approval_created_at(item), reverse=True)

    def _approval_created_at(self, item: dict[str, Any]) -> float:
        try:
            return float(item.get("created_at") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _approval_age_hours(self, item: dict[str, Any]) -> float:
        created_at = self._approval_created_at(item)
        if created_at <= 0:
            return 0.0
        return max(0.0, (time.time() - created_at) / 3600.0)

    def _approval_risk_tier(self, item: dict[str, Any]) -> str:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        raw = str(metadata.get("risk_tier") or item.get("risk_tier") or "").lower()
        if "critical" in raw or "tier_3" in raw or "tier3" in raw:
            return "critical"
        if "medium" in raw or "tier_2" in raw or "tier2" in raw:
            return "medium"
        action = str(item.get("action") or item.get("summary") or "")
        if is_destructive_or_external_objective(action):
            return "critical"
        return "low"

    def _classify_approval(self, item: dict[str, Any], *, seen_actions: set[str]) -> str:
        status = str(item.get("status") or "")
        if status == "expired":
            return "expired"
        action_key = str(item.get("action") or item.get("summary") or "").strip()
        if action_key in seen_actions:
            return "duplicate"
        seen_actions.add(action_key)
        age_hours = self._approval_age_hours(item)
        risk = self._approval_risk_tier(item)
        if risk == "low" and age_hours >= 24:
            return "stale"
        if risk == "medium" and age_hours >= 72:
            return "stale"
        if risk == "critical" and age_hours >= 72:
            return "stale"
        return "still_needed"

    def _classify_pending_approvals(self, approvals: list[dict[str, Any]]) -> dict[str, int]:
        counts = {
            "still_needed": 0,
            "stale": 0,
            "superseded": 0,
            "blocked": 0,
            "expired": 0,
            "duplicate": 0,
        }
        seen_actions: set[str] = set()
        for item in self._sorted_approvals_for_audit(approvals):
            classification = self._classify_approval(item, seen_actions=seen_actions)
            counts[classification] = counts.get(classification, 0) + 1
        return counts

    def _maybe_handle_telegram_imperative_request(
        self,
        text: str,
        *,
        session_id: str,
        runtime_channel: str | None,
    ) -> tuple[str | _BrainShortcut | None, str, str | None]:
        if (runtime_channel or "").strip().lower() != "telegram":
            return None, "not_telegram_channel", None
        intent = detect_telegram_imperative(text)
        if intent is None:
            if looks_like_actionable_telegram_message(text):
                response = self._handle_actionable_no_match(text, session_id=session_id)
                return response, "actionable_no_match", "actionable_no_match"
            return None, "telegram_imperative_no_match", None
        self._emit_safe(
            "telegram_imperative_detected",
            {
                "session_id": session_id,
                "intent": intent.intent,
                "target_hint": intent.target_hint,
                "artifact_hint": intent.artifact_hint,
                "requires_ui_read": intent.requires_ui_read,
                "requires_ui_write": intent.requires_ui_write,
                "requires_submit": intent.requires_submit,
            },
        )
        if self._telegram_ui_imperative_should_fallthrough(intent, text, session_id=session_id):
            self._emit_safe(
                "telegram_imperative_contextual_fallthrough",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "reason": "contextual_ui_target_or_artifact_unresolved",
                    "target_hint": intent.target_hint,
                    "artifact_hint": intent.artifact_hint,
                },
            )
            return None, f"telegram_imperative:{intent.intent}:contextual_fallthrough", intent.matched_pattern
        if intent.intent == "task.continue_active_mission":
            stateful_followup, resolution_source = self._maybe_resolve_telegram_continuation(
                text,
                session_id=session_id,
            )
            if stateful_followup is not None:
                self._emit_safe(
                    "telegram_continuation_stateful_resolved",
                    {
                        "session_id": session_id,
                        "intent": intent.intent,
                        "resolution_source": resolution_source,
                        "resolution_kind": (
                            "brain_shortcut"
                            if isinstance(stateful_followup, _BrainShortcut)
                            else "clarification"
                        ),
                    },
                )
                return stateful_followup, f"telegram_imperative:{intent.intent}:stateful", intent.matched_pattern
            # SOUL routing policy: a continuation this layer cannot resolve
            # deterministically belongs to the brain (it has session_state +
            # reply_context). Pre-brain routers must not ask for clarification.
            self._emit_safe(
                "telegram_imperative_contextual_fallthrough",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "reason": resolution_source or "continuation_context_not_found",
                },
            )
            return None, f"telegram_imperative:{intent.intent}:fallthrough_to_brain", intent.matched_pattern
        response = self._handle_telegram_imperative(intent, text, session_id=session_id)
        if response is None:
            return None, f"telegram_imperative:{intent.intent}:fallthrough_to_brain", intent.matched_pattern
        return response, f"telegram_imperative:{intent.intent}", intent.matched_pattern

    def _telegram_ui_imperative_should_fallthrough(
        self,
        intent: TelegramImperativeIntent,
        text: str,
        *,
        session_id: str,
    ) -> bool:
        if intent.intent not in {"ui.submit_prompt", "ui.paste_text"}:
            return False
        normalized = _normalize_command_text(text)
        contextual_terms = (
            "esto",
            "eso",
            "aqui",
            "aca",
            "lo",
            "la",
            "prototipo",
            "prompt",
            "envialo",
            "mandalo",
            "pegalo",
            "descarga",
        )
        if not any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in contextual_terms):
            return False
        try:
            state = self.brain.memory.get_session_state(session_id)
            mission = self._active_mission_context(state)
            target = self._resolve_telegram_target(intent, mission)
            artifact = self._resolve_telegram_artifact(intent, state, mission)
            artifact_text = self._resolve_telegram_artifact_text(intent, state, mission)
        except Exception:
            return True
        if not target:
            return True
        if intent.artifact_hint and not (artifact or artifact_text):
            return True
        return False

    def _maybe_resolve_telegram_continuation(
        self,
        text: str,
        *,
        session_id: str,
    ) -> tuple[str | _BrainShortcut | None, str | None]:
        state = self.brain.memory.get_session_state(session_id)
        missions = self._telegram_active_mission_candidates(session_id, state)
        if len(missions) > 1:
            # Ambiguity is context-dependent resolution: the brain presents
            # the options with full transcript context (SOUL routing policy).
            return None, "active_mission_ambiguous"
        if len(missions) == 1:
            objective = self._mission_continuation_objective(missions[0], state)
            if objective:
                return self._telegram_continuation_shortcut(
                    session_id,
                    text,
                    objective,
                    source="active_mission",
                    state=state,
                ), "active_mission"

        proposal = self._proposal_from_reply_context(state)
        if proposal:
            return self._telegram_continuation_shortcut(
                session_id,
                text,
                proposal,
                source="reply_context",
                state=state,
            ), "reply_context"

        pending_action = str(state.get("pending_action") or "").strip()
        if pending_action:
            return self._telegram_continuation_shortcut(
                session_id,
                text,
                pending_action,
                source="pending_action",
                state=state,
            ), "pending_action"

        proposal = self._last_actionable_proposal(state)
        if proposal:
            return self._telegram_continuation_shortcut(
                session_id,
                text,
                proposal,
                source="last_actionable_proposal",
                state=state,
            ), "last_actionable_proposal"

        pending_approval = self._latest_pending_approval_context(session_id, state)
        if pending_approval is not None:
            approval_id = str(pending_approval.get("approval_id") or "").strip()
            summary = str(pending_approval.get("summary") or pending_approval.get("action") or "").strip()
            lines = ["Hay una aprobación pendiente antes de continuar."]
            if approval_id:
                lines.append(f"approval_id: `{approval_id}`")
            if summary:
                lines.append(f"Acción: {summary[:240]}")
            lines.append("Usa `/task_pending` para ver el comando de aprobación o responde con autorización explícita.")
            return "\n".join(lines), "pending_approval"

        waiting_task = self._recent_waiting_for_user_task(session_id)
        if waiting_task is not None:
            return self._telegram_continuation_shortcut(
                session_id,
                text,
                waiting_task.objective,
                source="waiting_for_user_input_task",
                state=state,
                task_id=waiting_task.task_id,
            ), "waiting_for_user_input_task"

        continuable_task = self._recent_continuable_durable_task(session_id)
        if continuable_task is not None:
            return self._telegram_continuation_shortcut(
                session_id,
                text,
                continuable_task.objective,
                source="recent_durable_task",
                state=state,
                task_id=continuable_task.task_id,
            ), "recent_durable_task"

        proposal = self._proposal_from_recent_assistant(session_id)
        if proposal:
            return self._telegram_continuation_shortcut(
                session_id,
                text,
                proposal,
                source="recent_assistant",
                state=state,
            ), "recent_assistant"

        if len(missions) == 1:
            target = self._mission_target(missions[0])
            if target:
                objective = f"Continuar misión activa para {target}"
                return self._telegram_continuation_shortcut(
                    session_id,
                    text,
                    objective,
                    source="active_mission_target",
                    state=state,
                ), "active_mission_target"

        return None, None

    def _telegram_continuation_shortcut(
        self,
        session_id: str,
        user_text: str,
        objective: str,
        *,
        source: str,
        state: dict[str, Any],
        task_id: str | None = None,
    ) -> _BrainShortcut:
        objective = " ".join(str(objective or "").split()).strip()
        active_object = dict(state.get("active_object") or {})
        if source == "reply_context":
            # Single-use: once a continuation consumed the quoted reply, a
            # later bare "Procede" must not re-execute the same proposal.
            active_object.pop("reply_context", None)
        active_object["last_continuation_resolution"] = {
            "source": source,
            "objective": objective[:300],
            "task_id": task_id,
            "updated_at": time.time(),
        }
        active_object["pending_action_meta"] = {
            "created_at": time.time(),
            "source": source,
            "ttl_seconds": 30 * 60,
            "tier_hint": "unknown",
            "topic": objective[:140],
        }
        self.brain.memory.update_session_state(
            session_id,
            pending_action=objective,
            active_object=active_object,
            verification_status="pending",
        )
        checkpoint = state.get("last_checkpoint") or {}
        checkpoint_text = json.dumps(checkpoint, ensure_ascii=True, sort_keys=True) if checkpoint else "{}"
        task_line = f"\nTask previa: {task_id}" if task_id else ""
        self._emit_safe(
            "continuation_resolved",
            {
                "session_id": session_id,
                "source": source,
                "proposal_preview": objective[:160],
                "user_text_preview": user_text[:80],
                "task_id": task_id,
            },
        )
        self._emit_safe(
            "pending_action_execution_started",
            {"session_id": session_id, "pending_action_preview": objective[:160]},
        )
        return _BrainShortcut(
            text=(
                f"Continúa con esta acción pendiente: {objective}\n"
                f"Mensaje de aprobación del usuario: {user_text}\n"
                f"Origen del contexto: {source}{task_line}\n"
                f"Checkpoint actual: {checkpoint_text}"
            ),
            memory_text=user_text,
        )

    def _telegram_active_mission_candidates(
        self,
        session_id: str,
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        def add_from_state(candidate_state: dict[str, Any], source_session_id: str) -> None:
            active_object = candidate_state.get("active_object") or {}
            if not isinstance(active_object, dict):
                return
            raw_missions: list[Any] = []
            for key in ("active_mission", "_mission"):
                value = active_object.get(key)
                if isinstance(value, dict):
                    raw_missions.append(value)
            for key in ("active_missions", "missions"):
                value = active_object.get(key)
                if isinstance(value, list):
                    raw_missions.extend(item for item in value if isinstance(item, dict))
            for raw in raw_missions:
                mission = dict(raw)
                if not self._telegram_mission_matches_session(mission, session_id, source_session_id):
                    continue
                if not self._telegram_mission_is_active(mission):
                    continue
                mission["_source_session_id"] = source_session_id
                key = str(mission.get("mission_id") or f"{source_session_id}:{mission.get('active_target')}")
                if any(str(existing.get("mission_id") or "") == key for existing in candidates):
                    continue
                candidates.append(mission)

        add_from_state(state, session_id)
        if not candidates:
            try:
                recent_states = self.brain.memory.list_session_states(limit=20)
            except Exception:
                recent_states = []
            for item in recent_states:
                source_session_id = str(item.get("session_id") or "")
                if source_session_id == session_id:
                    continue
                add_from_state(item, source_session_id)
        return candidates[:5]

    @staticmethod
    def _telegram_session_aliases(session_id: str) -> set[str]:
        aliases = {str(session_id)}
        if session_id.startswith("tg-"):
            aliases.add(session_id[3:])
        return {alias for alias in aliases if alias}

    def _telegram_mission_matches_session(
        self,
        mission: dict[str, Any],
        session_id: str,
        source_session_id: str,
    ) -> bool:
        if source_session_id == session_id:
            return True
        channel = str(mission.get("channel") or "").strip().lower()
        if channel and channel != "telegram":
            return False
        aliases = self._telegram_session_aliases(session_id)
        for key in ("chat_id", "user_id", "session_id", "external_session_id", "external_user_id"):
            value = str(mission.get(key) or "").strip()
            if value and value in aliases:
                return True
        return False

    @staticmethod
    def _telegram_mission_is_active(mission: dict[str, Any]) -> bool:
        status = str(mission.get("status") or mission.get("state") or "active").strip().lower()
        if status in {"completed", "succeeded", "failed", "cancelled", "closed", "done"}:
            return False
        expires_at = mission.get("expires_at")
        try:
            if expires_at is not None and time.time() > float(expires_at):
                return False
        except (TypeError, ValueError):
            return False
        return True

    def _mission_continuation_objective(
        self,
        mission: dict[str, Any],
        state: dict[str, Any],
    ) -> str | None:
        active_task = mission.get("active_task")
        if isinstance(active_task, dict):
            objective = str(active_task.get("objective") or "").strip()
            if objective:
                return objective
        for key in ("pending_action", "objective", "current_goal", "last_user_goal", "summary"):
            objective = str(mission.get(key) or "").strip()
            if objective:
                return objective
        active_object = state.get("active_object") or {}
        if isinstance(active_object, dict):
            active_task = active_object.get("active_task") or {}
            if isinstance(active_task, dict):
                objective = str(active_task.get("objective") or "").strip()
                status = str(active_task.get("status") or "").strip().lower()
                if objective and status not in {"completed", "succeeded", "done"}:
                    return objective
        pending_action = str(state.get("pending_action") or "").strip()
        if pending_action:
            return pending_action
        target = self._mission_target(mission)
        if target:
            return f"Continuar misión activa para {target}"
        return None

    def _proposal_from_reply_context(self, state: dict[str, Any]) -> str | None:
        try:
            proposal = self._state_handler._extract_proposal_from_reply_context(state)
        except Exception:
            proposal = None
        return str(proposal or "").strip() or None

    def _proposal_from_recent_assistant(self, session_id: str) -> str | None:
        try:
            proposal = self._state_handler._extract_proposal_from_recent_assistant(session_id)
        except Exception:
            proposal = None
        return str(proposal or "").strip() or None

    @staticmethod
    def _last_actionable_proposal(state: dict[str, Any]) -> str | None:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return None
        proposal = active_object.get("last_actionable_proposal") or {}
        if isinstance(proposal, str):
            return proposal.strip() or None
        if not isinstance(proposal, dict):
            return None
        for key in ("objective", "pending_action", "summary", "text"):
            value = str(proposal.get(key) or "").strip()
            if value:
                return value
        return None

    def _latest_pending_approval_context(
        self,
        session_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any] | None:
        pending = state.get("pending_approvals") or []
        if isinstance(pending, list):
            for item in reversed(pending):
                if isinstance(item, dict) and str(item.get("status") or "pending") == "pending":
                    return item
        checkpoint = state.get("last_checkpoint") or {}
        if isinstance(checkpoint, dict) and checkpoint.get("approval_id"):
            return {
                "approval_id": checkpoint.get("approval_id"),
                "summary": checkpoint.get("summary") or checkpoint.get("pending_action") or "",
            }
        if self.approvals is not None:
            try:
                approvals = self.approvals.list_pending()
            except Exception:
                approvals = []
            aliases = self._telegram_session_aliases(session_id)
            for item in reversed(approvals):
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                item_session = str(metadata.get("session_id") or metadata.get("external_session_id") or "").strip()
                if item_session and item_session in aliases:
                    return item
        return None

    def _recent_waiting_for_user_task(self, session_id: str) -> Any | None:
        if self.task_ledger is None:
            return None
        try:
            records = self.task_ledger.list(session_id=session_id, limit=20)
        except Exception:
            return None
        for record in records:
            haystack = " ".join(
                [
                    str(getattr(record, "error", "") or ""),
                    str(getattr(record, "summary", "") or ""),
                    json.dumps(getattr(record, "metadata", {}) or {}, sort_keys=True),
                ]
            ).lower()
            if "waiting_for_user_input" in haystack and str(getattr(record, "objective", "") or "").strip():
                return record
        return None

    def _recent_continuable_durable_task(self, session_id: str) -> Any | None:
        if self.task_ledger is None:
            return None
        try:
            records = self.task_ledger.list(session_id=session_id, limit=20)
        except Exception:
            return None
        now = time.time()
        for record in records:
            try:
                age = now - float(getattr(record, "updated_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                age = 0.0
            if age > 24 * 3600:
                continue
            verification = str(getattr(record, "verification_status", "") or "").strip().lower()
            status = str(getattr(record, "status", "") or "").strip().lower()
            metadata = getattr(record, "metadata", {}) or {}
            metadata_status = str(metadata.get("status") or metadata.get("result_status") or "").strip().lower()
            if (
                status in {"running", "queued"}
                or verification in {"awaiting_continue", "needs_verification", "pending"}
                or metadata_status in {"awaiting_continue", "needs_verification"}
            ) and str(getattr(record, "objective", "") or "").strip():
                return record
        return None

    def _handle_actionable_no_match(self, text: str, *, session_id: str) -> str | None:
        """Telemetry-only emit; returns None so dispatch chain continues to
        the brain. The brain has session memory/mission context and produces
        a natural reply instead of a robotic plantilla."""
        state = self.brain.memory.get_session_state(session_id)
        mission = self._active_mission_context(state)
        candidate_target = self._mission_target(mission) or "desconocido"
        normalized = _normalize_command_text(text)
        candidate_action = "unknown"
        if "app" in normalized:
            candidate_action = "ui.unknown_app_action"
        elif any(token in normalized for token in ("eso", "lo", "haz", "orquesta", "orchestrate")):
            candidate_action = "task.contextual_action"
        self._emit_safe(
            "actionable_no_match",
            {
                "channel": "telegram",
                "session_id": session_id,
                "message_text_hash": self._stable_text_hash(text),
                "candidate_action": candidate_action,
                "candidate_target": candidate_target,
                "active_mission_id": str((mission or {}).get("mission_id") or ""),
                "reason": "no_route_match",
            },
        )
        self._emit_safe("telegram_actionable_no_match", {"session_id": session_id, "candidate_action": candidate_action})
        return None

    @staticmethod
    def _stable_text_hash(text: str) -> str:
        import hashlib

        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]

    def _handle_telegram_imperative(
        self,
        intent: TelegramImperativeIntent,
        text: str,
        *,
        session_id: str,
    ) -> str | None:
        state = self.brain.memory.get_session_state(session_id)
        mission = self._active_mission_context(state)
        target = self._resolve_telegram_target(intent, mission)
        artifact = self._resolve_telegram_artifact(intent, state, mission)
        artifact_text = self._resolve_telegram_artifact_text(intent, state, mission)
        # SOUL routing policy: when this layer cannot resolve target/artifact
        # from the literal text + active mission, the message belongs to the
        # brain. Returning None falls through silently; the forbidden
        # "¿en qué app o target?" clarification must never come from here.
        if intent.needs_context and not target and intent.intent in {
            "ui.paste_text",
            "ui.set_instructions",
            "ui.submit_prompt",
            "ui.inspect_app",
            "task.continue_active_mission",
        }:
            self._emit_safe(
                "active_mission_resolution_failed",
                {"session_id": session_id, "intent": intent.intent, "reason": "missing_target"},
            )
            return None
        if intent.artifact_hint and not artifact:
            self._emit_safe(
                "active_mission_resolution_failed",
                {"session_id": session_id, "intent": intent.intent, "reason": "missing_artifact"},
            )
            return None
        if intent.intent in {"ui.paste_text", "ui.set_instructions"} and not artifact_text:
            self._emit_safe(
                "active_mission_resolution_failed",
                {"session_id": session_id, "intent": intent.intent, "reason": "missing_artifact_text"},
            )
            return None
        if mission:
            self._emit_safe(
                "active_mission_resolution_success",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "target": target,
                    "artifact": artifact,
                    "mission_id": str(mission.get("mission_id") or ""),
                },
            )
        if intent.intent == "approvals.cleanup_stale_duplicates":
            return self._handle_approval_cleanup_imperative(intent, text, session_id=session_id)
        if target:
            self._remember_telegram_mission(
                session_id,
                target=target,
                artifact=artifact,
                last_user_goal=text,
                pending_action=self._objective_for_imperative(intent, target=target, artifact=artifact),
            )
        if intent.intent == "task.continue_active_mission":
            objective = self._objective_for_imperative(intent, target=target, artifact=artifact)
            if objective:
                task_id = self._record_telegram_imperative_task(
                    session_id=session_id,
                    text=text,
                    intent=intent,
                    target=target,
                    artifact=artifact,
                    result_status="partial_success",
                    capability=None,
                    summary="Active mission continuation resolved but not auto-executed by UI router.",
                )
                self._emit_safe("telegram_imperative_routed", {"session_id": session_id, "intent": intent.intent, "task_id": task_id})
                return self._public_telegram_imperative_result(
                    session_id=session_id,
                    intent=intent,
                    target=target,
                    result_status="partial_success",
                    handler_result=(
                        "Tengo ubicada la misión activa, pero no ejecuté nada: "
                        "falta una acción concreta que pueda hacer en la app."
                    ),
                )
        missing_capability = self._missing_ui_capability(intent)
        if missing_capability:
            task_id = self._record_telegram_imperative_task(
                session_id=session_id,
                text=text,
                intent=intent,
                target=target,
                artifact=artifact,
                result_status="blocked_by_capability",
                capability=missing_capability,
                summary=f"{intent.intent} blocked because {missing_capability} is unavailable.",
            )
            self._emit_safe(
                "telegram_imperative_blocked",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "task_id": task_id,
                    "blocked_reason": "blocked_by_capability",
                    "capability": missing_capability,
                },
            )
            fallback = ""
            if intent.intent in {"ui.paste_text", "ui.set_instructions"}:
                artifact_label = "instrucciones" if intent.intent == "ui.set_instructions" else "prompt"
                fallback = (
                    f"\nFallback seguro: puedo preparar/copiar el {artifact_label}, "
                    "pero eso no equivale a pegarlo en la app."
                )
            return self._public_telegram_imperative_result(
                session_id=session_id,
                intent=intent,
                target=target,
                result_status="blocked_by_capability",
                capability=missing_capability,
                handler_result=fallback.strip() or None,
            )
        execution_response = self._execute_telegram_imperative_via_computer(
            intent,
            text,
            session_id=session_id,
            target=target,
            artifact=artifact,
            artifact_text=artifact_text,
        )
        if execution_response is not None:
            return execution_response
        task_id = self._record_telegram_imperative_task(
            session_id=session_id,
            text=text,
            intent=intent,
            target=target,
            artifact=artifact,
            result_status="pending_approval" if intent.requires_submit else "partial_success",
            capability=None,
            summary=f"{intent.intent} routed for execution.",
        )
        self._emit_safe(
            "telegram_imperative_routed",
            {"session_id": session_id, "intent": intent.intent, "task_id": task_id},
        )
        return self._public_telegram_imperative_result(
            session_id=session_id,
            intent=intent,
            target=target,
            result_status="partial_success",
            handler_result="Tengo el objetivo ubicado, pero no ejecuté una acción local concreta.",
        )

    def _active_mission_context(self, state: dict[str, Any]) -> dict[str, Any] | None:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return None
        mission = active_object.get("active_mission") or {}
        if not isinstance(mission, dict):
            return None
        expires_at = mission.get("expires_at")
        try:
            if expires_at is not None and time.time() > float(expires_at):
                return None
        except (TypeError, ValueError):
            return None
        return mission

    def _mission_target(self, mission: dict[str, Any] | None) -> str | None:
        if not mission:
            return None
        target = str(mission.get("active_target") or "").strip()
        return target or None

    def _resolve_telegram_target(
        self,
        intent: TelegramImperativeIntent,
        mission: dict[str, Any] | None,
    ) -> str | None:
        if intent.target_hint:
            return intent.target_hint
        return self._mission_target(mission)

    def _resolve_telegram_artifact(
        self,
        intent: TelegramImperativeIntent,
        state: dict[str, Any],
        mission: dict[str, Any] | None,
    ) -> str | None:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            active_object = {}
        if intent.artifact_hint == "prompt":
            prompt = active_object.get("active_prompt") or {}
            if isinstance(prompt, dict):
                return str(prompt.get("summary") or prompt.get("kind") or "prompt").strip() or None
            if isinstance(prompt, str) and prompt.strip():
                return "prompt"
            if self._prompt_text_from_reply_context(state):
                return "prompt from reply context"
        if intent.artifact_hint == "instructions":
            prompt = active_object.get("active_prompt") or active_object.get("active_instructions") or {}
            if isinstance(prompt, dict):
                return str(prompt.get("summary") or "instructions").strip() or None
            if isinstance(prompt, str) and prompt.strip():
                return "instructions"
        if mission:
            artifact = str(mission.get("active_artifact") or "").strip()
            if artifact:
                return artifact
        pending = str(state.get("pending_action") or "").strip()
        if pending and intent.artifact_hint:
            return pending[:120]
        return None

    def _resolve_telegram_artifact_text(
        self,
        intent: TelegramImperativeIntent,
        state: dict[str, Any],
        mission: dict[str, Any] | None,
    ) -> str | None:
        if intent.artifact_hint not in {"prompt", "instructions"}:
            return None
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            active_object = {}
        candidates: list[Any] = []
        if intent.artifact_hint == "prompt":
            candidates.extend(
                [
                    active_object.get("active_prompt"),
                    active_object.get("latest_prompt"),
                    active_object.get("pending_prompt"),
                ]
            )
        else:
            candidates.extend(
                [
                    active_object.get("active_instructions"),
                    active_object.get("active_prompt"),
                    active_object.get("latest_instructions"),
                ]
            )
        if mission:
            candidates.extend(
                [
                    mission.get("active_artifact_text"),
                    mission.get("active_prompt_text"),
                    mission.get("instructions_text"),
                ]
            )
        for candidate in candidates:
            text = self._artifact_text_from_candidate(candidate)
            if text:
                return text
        if intent.artifact_hint == "prompt":
            text = self._prompt_text_from_reply_context(state)
            if text:
                return text
        return None

    @staticmethod
    def _artifact_text_from_candidate(candidate: Any) -> str | None:
        if isinstance(candidate, str):
            value = candidate.strip()
            return value or None
        if not isinstance(candidate, dict):
            return None
        for key in ("text", "content", "prompt", "instructions", "body"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _prompt_text_from_reply_context(state: dict[str, Any]) -> str | None:
        active_object = state.get("active_object") or {}
        if not isinstance(active_object, dict):
            return None
        reply_context = active_object.get("reply_context") or {}
        if not isinstance(reply_context, dict) or not reply_context_fresh(reply_context):
            return None
        text = str(reply_context.get("text") or "")
        if not text.strip():
            return None
        lines = text.splitlines()
        marker_index: int | None = None
        for index, line in enumerate(lines):
            normalized = _normalize_command_text(line)
            if "prompt que voy a pegar" in normalized or "prompt to paste" in normalized:
                marker_index = index
                break
        search_lines = lines[marker_index + 1 :] if marker_index is not None else lines
        collected: list[str] = []
        started = False
        for line in search_lines:
            stripped = line.strip()
            if stripped.startswith(">"):
                started = True
                collected.append(stripped[1:].lstrip())
                continue
            if started and stripped == "":
                collected.append("")
                continue
            if started:
                break
        prompt = "\n".join(collected).strip()
        return prompt or None

    def _remember_telegram_mission(
        self,
        session_id: str,
        *,
        target: str,
        artifact: str | None,
        last_user_goal: str,
        pending_action: str | None,
    ) -> None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        mission = dict(active_object.get("active_mission") or {})
        mission.update(
            {
                "channel": "telegram",
                "chat_id": session_id,
                "mission_id": mission.get("mission_id") or f"mission:{session_id}:{int(time.time())}",
                "active_target": target,
                "active_artifact": artifact or mission.get("active_artifact") or "",
                "last_user_goal": last_user_goal[:240],
                "pending_action": pending_action or "",
                "updated_at": time.time(),
                "expires_at": time.time() + 30 * 60,
            }
        )
        active_object["active_mission"] = mission
        last_result = active_object.get("last_action_result")
        current_goal_candidate = (pending_action or last_user_goal).strip()
        self.brain.memory.update_session_state(
            session_id,
            mode="ops",
            current_goal=(
                current_goal_candidate[:280]
                if self._looks_like_persistable_current_goal(current_goal_candidate)
                else ""
            ),
            pending_action=pending_action or state.get("pending_action"),
            active_object=active_object,
            last_checkpoint={
                "summary": f"Telegram imperative routed: {pending_action or last_user_goal[:160]}",
                "verification_status": "pending",
                "active_target": target,
                "last_action_result": last_result,
            },
        )

    def _objective_for_imperative(
        self,
        intent: TelegramImperativeIntent,
        *,
        target: str | None,
        artifact: str | None,
    ) -> str:
        target_text = target or "active target"
        if intent.intent == "ui.open_app":
            return f"Open/focus {target_text}"
        if intent.intent == "ui.inspect_app":
            return f"Inspect {target_text}"
        if intent.intent == "ui.paste_text":
            return f"Paste {artifact or 'active prompt'} into {target_text}"
        if intent.intent == "ui.set_instructions":
            return f"Give {artifact or 'instructions'} to {target_text}"
        if intent.intent == "ui.submit_prompt":
            return f"Submit prompt in {target_text}"
        if intent.intent == "approvals.cleanup_stale_duplicates":
            return "Archive stale or duplicate pending approvals"
        return f"Continue active mission for {target_text}"

    def _handle_approval_cleanup_imperative(
        self,
        intent: TelegramImperativeIntent,
        text: str,
        *,
        session_id: str,
    ) -> str:
        if self.approvals is None:
            task_id = self._record_telegram_imperative_task(
                session_id=session_id,
                text=text,
                intent=intent,
                target=None,
                artifact="approval backlog",
                result_status="blocked_by_capability",
                capability="approval_manager",
                summary="Approval cleanup blocked because ApprovalManager is unavailable.",
            )
            self._emit_safe(
                "telegram_imperative_blocked",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "task_id": task_id,
                    "blocked_reason": "blocked_by_capability",
                    "capability": "approval_manager",
                },
            )
            return "No puedo limpiar aprobaciones ahora porque el gestor de aprobaciones no está disponible."
        try:
            approvals = self.approvals.list_pending()
        except Exception as exc:
            task_id = self._record_telegram_imperative_task(
                session_id=session_id,
                text=text,
                intent=intent,
                target=None,
                artifact="approval backlog",
                result_status="failed",
                capability=None,
                summary=f"Approval cleanup failed while listing approvals: {exc}",
            )
            self._emit_safe(
                "telegram_imperative_execution_failed",
                {"session_id": session_id, "intent": intent.intent, "task_id": task_id, "error": str(exc)[:240]},
            )
            return f"No pude leer las aprobaciones pendientes: {str(exc)[:240]}"
        candidates = self._approval_cleanup_candidates(approvals)
        archived: list[tuple[str, str]] = []
        failed: list[tuple[str, str]] = []
        for item, classification in candidates:
            approval_id = str(item.get("approval_id") or "")
            if not approval_id:
                failed.append(("", "missing_approval_id"))
                continue
            try:
                ok = self.approvals.archive(approval_id, reason=f"telegram_cleanup:{classification}")
            except Exception as exc:
                failed.append((approval_id, str(exc)[:160]))
                continue
            if ok:
                archived.append((approval_id, classification))
            else:
                failed.append((approval_id, "archive_returned_false"))
        kept_count = max(0, len(approvals) - len(archived) - len(failed))
        if failed and archived:
            result_status = "partial_success"
        elif failed:
            result_status = "failed"
        else:
            result_status = "succeeded"
        summary = (
            f"Approval cleanup archived {len(archived)} stale/duplicate approvals; "
            f"kept {kept_count}; failed {len(failed)}."
        )
        handler_lines = [
            summary,
            "Archived IDs: " + (", ".join(f"{approval_id}:{reason}" for approval_id, reason in archived) or "none"),
        ]
        if failed:
            handler_lines.append("Failed IDs: " + ", ".join(f"{approval_id or 'unknown'}:{reason}" for approval_id, reason in failed))
        task_id = self._record_telegram_imperative_task(
            session_id=session_id,
            text=text,
            intent=intent,
            target=None,
            artifact="approval backlog",
            result_status=result_status,
            capability=None,
            summary=summary,
            handler_result="\n".join(handler_lines),
            execution_backend="approval_manager",
        )
        self._remember_approval_cleanup_result(
            session_id,
            task_id=task_id,
            status=result_status,
            archived_count=len(archived),
            kept_count=kept_count,
            failed_count=len(failed),
        )
        event_payload = {
            "session_id": session_id,
            "intent": intent.intent,
            "task_id": task_id,
            "status": result_status,
            "archived_count": len(archived),
            "kept_count": kept_count,
            "failed_count": len(failed),
        }
        if result_status == "failed":
            self._emit_safe("telegram_imperative_execution_failed", event_payload)
        else:
            self._emit_safe("telegram_imperative_executed", event_payload)
            self._emit_safe("telegram_imperative_routed", event_payload)
            self._emit_safe("approval_cleanup_executed", event_payload)
        response_lines = [
            f"Listo. Archivé {len(archived)} aprobaciones stale/duplicadas.",
            f"Conservadas: {kept_count}. Fallidas: {len(failed)}.",
        ]
        if failed:
            response_lines.append("Algunas no se pudieron archivar; dejé el detalle en el registro interno.")
        return "\n".join(response_lines)

    def _approval_cleanup_candidates(self, approvals: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
        candidates: list[tuple[dict[str, Any], str]] = []
        seen_actions: set[str] = set()
        for item in self._sorted_approvals_for_audit(approvals):
            classification = self._classify_approval(item, seen_actions=seen_actions)
            if classification in {"stale", "duplicate", "expired"}:
                candidates.append((item, classification))
        return candidates

    def _remember_approval_cleanup_result(
        self,
        session_id: str,
        *,
        task_id: str,
        status: str,
        archived_count: int,
        kept_count: int,
        failed_count: int,
    ) -> None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        last_result = dict(active_object.get("last_action_result") or {})
        last_result.update(
            {
                "task_id": task_id,
                "status": status,
                "intent": "approvals.cleanup_stale_duplicates",
                "archived_count": archived_count,
                "kept_count": kept_count,
                "failed_count": failed_count,
                "updated_at": time.time(),
            }
        )
        active_object["last_action_result"] = last_result
        self.brain.memory.update_session_state(
            session_id,
            mode="ops",
            verification_status=status,
            active_object=active_object,
            last_checkpoint={
                "summary": (
                    f"Approval cleanup {status}: archived={archived_count}, "
                    f"kept={kept_count}, failed={failed_count}"
                ),
                "verification_status": status,
                "task_id": task_id,
                "intent": "approvals.cleanup_stale_duplicates",
            },
        )

    def _public_telegram_imperative_result(
        self,
        *,
        session_id: str,
        intent: TelegramImperativeIntent,
        target: str | None,
        result_status: str,
        handler_result: str | None = None,
        capability: str | None = None,
    ) -> str:
        detail = self._final_render(session_id, str(handler_result or "").strip()) if handler_result else ""
        normalized_detail = _normalize_command_text(detail)
        if result_status == "succeeded":
            return detail or "Listo. Acción completada."
        if result_status == "pending_approval":
            if "necesito tu autorizacion" in normalized_detail:
                return detail
            return (
                "Necesito tu autorización para continuar con esa acción de escritorio.\n"
                "Responde `te autorizo` para ejecutarla o `aborta` para cancelarla."
            )
        if result_status == "blocked_by_capability":
            label = self._public_ui_capability_label(capability)
            target_text = target or "esa app"
            suffix = f"\n{detail}" if detail else ""
            return f"No puedo completar esa acción en {target_text} desde este runtime porque falta {label}.{suffix}"
        if result_status == "failed":
            return detail or "No pude completar esa acción."
        return detail or "Tengo el objetivo ubicado, pero no ejecuté una acción local concreta."

    @staticmethod
    def _public_ui_capability_label(capability: str | None) -> str:
        labels = {
            "local_ui_write": "control local de apps",
            "local_ui_read": "lectura local de pantalla",
            "computer_control": "control de escritorio",
            "approval_manager": "gestión de aprobaciones",
        }
        return labels.get(str(capability or ""), "una capacidad local requerida")

    def _execute_telegram_imperative_via_computer(
        self,
        intent: TelegramImperativeIntent,
        text: str,
        *,
        session_id: str,
        target: str | None,
        artifact: str | None,
        artifact_text: str | None,
    ) -> str | None:
        if intent.intent not in {
            "ui.open_app",
            "ui.inspect_app",
            "ui.paste_text",
            "ui.set_instructions",
            "ui.submit_prompt",
        }:
            return None
        instruction = self._computer_instruction_for_telegram_imperative(
            intent,
            target=target,
            artifact_text=artifact_text,
        )
        if instruction is None:
            return None
        if intent.intent == "ui.open_app":
            return self._execute_local_open_app_imperative(
                intent,
                text,
                session_id=session_id,
                target=target,
                artifact=artifact,
            )
        if intent.intent == "ui.paste_text":
            return self._execute_local_paste_text_imperative(
                intent,
                text,
                session_id=session_id,
                target=target,
                artifact=artifact,
                artifact_text=artifact_text,
            )
        self._emit_safe(
            "telegram_imperative_execution_started",
            {
                "session_id": session_id,
                "intent": intent.intent,
                "target": target,
                "instruction_hash": self._stable_text_hash(instruction),
            },
        )
        if intent.intent == "ui.inspect_app":
            handler_result = self._computer_handler.computer_response(instruction, session_id)
            execution_backend = "computer_read"
        else:
            handler_result = self._computer_handler.action_response(instruction, session_id)
            execution_backend = "computer_control"
        result_status = self._classify_computer_handler_result(handler_result)
        approval_id = None
        if result_status == "pending_approval":
            pending = self._latest_pending_computer_approval(session_id)
            if pending is not None:
                approval_id = str(pending.get("approval_id") or "") or None
        task_id = self._record_telegram_imperative_task(
            session_id=session_id,
            text=text,
            intent=intent,
            target=target,
            artifact=artifact,
            result_status=result_status,
            capability=None if result_status != "blocked_by_capability" else "computer_control",
            summary=f"{intent.intent} executed through {execution_backend}: {result_status}.",
            handler_result=handler_result,
            approval_id=approval_id,
            execution_backend=execution_backend,
        )
        event_payload = {
            "session_id": session_id,
            "intent": intent.intent,
            "task_id": task_id,
            "target": target,
            "status": result_status,
            "backend": execution_backend,
            "approval_id": approval_id,
        }
        if result_status == "pending_approval":
            self._emit_safe("telegram_imperative_pending_approval", event_payload)
        elif result_status == "blocked_by_capability":
            self._emit_safe(
                "telegram_imperative_blocked",
                {**event_payload, "blocked_reason": "blocked_by_capability", "capability": "computer_control"},
            )
        elif result_status == "failed":
            self._emit_safe("telegram_imperative_execution_failed", event_payload)
        else:
            self._emit_safe("telegram_imperative_executed", event_payload)
            self._emit_safe("telegram_imperative_routed", event_payload)
        return self._public_telegram_imperative_result(
            session_id=session_id,
            intent=intent,
            target=target,
            result_status=result_status,
            handler_result=handler_result,
            capability="computer_control" if result_status == "blocked_by_capability" else None,
        )

    def _execute_local_paste_text_imperative(
        self,
        intent: TelegramImperativeIntent,
        text: str,
        *,
        session_id: str,
        target: str | None,
        artifact: str | None,
        artifact_text: str | None,
    ) -> str:
        app_name = self._local_app_name_for_target(target)
        execution_backend = "local_clipboard_paste"
        if app_name is None:
            result_status = "failed"
            handler_result = "No pude resolver un nombre de app seguro para pegar."
        elif not artifact_text:
            result_status = "failed"
            handler_result = "No encontré texto preparado para pegar."
        else:
            self._emit_safe(
                "telegram_imperative_execution_started",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "target": target,
                    "backend": execution_backend,
                },
            )
            try:
                open_result = subprocess.run(
                    ["open", "-a", app_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if open_result.returncode != 0:
                    detail = (open_result.stderr or open_result.stdout or "open returned non-zero").strip()
                    raise RuntimeError(f"open failed: {redact_sensitive(detail[:240])}")
                time.sleep(0.4)
                copy_result = subprocess.run(
                    ["pbcopy"],
                    input=artifact_text,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if copy_result.returncode != 0:
                    detail = (copy_result.stderr or copy_result.stdout or "pbcopy returned non-zero").strip()
                    raise RuntimeError(f"pbcopy failed: {redact_sensitive(detail[:240])}")
                paste_result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to keystroke "v" using command down',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if paste_result.returncode != 0:
                    detail = (paste_result.stderr or paste_result.stdout or "osascript returned non-zero").strip()
                    raise RuntimeError(f"paste failed: {redact_sensitive(detail[:240])}")
            except Exception as exc:
                result_status = "failed"
                handler_result = f"No pude pegar en `{app_name}`: {str(exc)[:240]}"
            else:
                result_status = "succeeded"
                handler_result = f"Texto pegado en `{app_name}` sin enviar."
        task_id = self._record_telegram_imperative_task(
            session_id=session_id,
            text=text,
            intent=intent,
            target=target,
            artifact=artifact,
            result_status=result_status,
            capability=None if result_status == "succeeded" else execution_backend,
            summary=f"{intent.intent} executed through {execution_backend}: {result_status}.",
            handler_result=handler_result,
            execution_backend=execution_backend,
            notify_policy="none",
        )
        event_payload = {
            "session_id": session_id,
            "intent": intent.intent,
            "task_id": task_id,
            "target": target,
            "status": result_status,
            "backend": execution_backend,
            "approval_id": None,
        }
        if result_status == "failed":
            self._emit_safe("telegram_imperative_execution_failed", event_payload)
        else:
            self._emit_safe("telegram_imperative_executed", event_payload)
            self._emit_safe("telegram_imperative_routed", event_payload)
        return self._public_telegram_imperative_result(
            session_id=session_id,
            intent=intent,
            target=target,
            result_status=result_status,
            handler_result=handler_result,
            capability=execution_backend if result_status != "succeeded" else None,
        )

    def _execute_local_open_app_imperative(
        self,
        intent: TelegramImperativeIntent,
        text: str,
        *,
        session_id: str,
        target: str | None,
        artifact: str | None,
    ) -> str:
        app_name = self._local_app_name_for_target(target)
        execution_backend = "local_app_open"
        if app_name is None:
            result_status = "failed"
            handler_result = "No pude resolver un nombre de app seguro para abrir."
        else:
            self._emit_safe(
                "telegram_imperative_execution_started",
                {
                    "session_id": session_id,
                    "intent": intent.intent,
                    "target": target,
                    "backend": execution_backend,
                },
            )
            try:
                completed = subprocess.run(
                    ["open", "-a", app_name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception as exc:
                result_status = "failed"
                handler_result = f"No pude abrir `{app_name}`: {str(exc)[:240]}"
            else:
                if completed.returncode == 0:
                    result_status = "succeeded"
                    handler_result = f"`{app_name}` abierto/enfocado."
                else:
                    result_status = "failed"
                    detail = (completed.stderr or completed.stdout or "open returned non-zero").strip()
                    handler_result = f"No pude abrir `{app_name}`: {redact_sensitive(detail[:240])}"
        task_id = self._record_telegram_imperative_task(
            session_id=session_id,
            text=text,
            intent=intent,
            target=target,
            artifact=artifact,
            result_status=result_status,
            capability=None if result_status == "succeeded" else execution_backend,
            summary=f"{intent.intent} executed through {execution_backend}: {result_status}.",
            handler_result=handler_result,
            execution_backend=execution_backend,
            notify_policy="none",
        )
        event_payload = {
            "session_id": session_id,
            "intent": intent.intent,
            "task_id": task_id,
            "target": target,
            "status": result_status,
            "backend": execution_backend,
            "approval_id": None,
        }
        if result_status == "failed":
            self._emit_safe("telegram_imperative_execution_failed", event_payload)
        else:
            self._emit_safe("telegram_imperative_executed", event_payload)
            self._emit_safe("telegram_imperative_routed", event_payload)
        return self._public_telegram_imperative_result(
            session_id=session_id,
            intent=intent,
            target=target,
            result_status=result_status,
            handler_result=handler_result,
            capability=execution_backend if result_status != "succeeded" else None,
        )

    @staticmethod
    def _local_app_name_for_target(target: str | None) -> str | None:
        if not target:
            return None
        normalized = _normalize_command_text(target)
        if "codex" in normalized:
            return "Codex"
        if "chatgpt" in normalized or "chat gpt" in normalized:
            return "ChatGPT"
        if "claude" in normalized:
            return "Claude"
        if "chrome" in normalized:
            return "Google Chrome"
        cleaned = re.sub(r"\bapp\b", "", str(target), flags=re.IGNORECASE).strip(" ._-")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,79}", cleaned):
            return None
        return cleaned

    def _computer_instruction_for_telegram_imperative(
        self,
        intent: TelegramImperativeIntent,
        *,
        target: str | None,
        artifact_text: str | None,
    ) -> str | None:
        target_text = target or "the active app"
        if intent.intent == "ui.open_app":
            return (
                f"Open or focus {target_text}. "
                "Stop once the app is visible and focused. "
                "Do not paste, type, submit, send, run, or change content."
            )
        if intent.intent == "ui.inspect_app":
            return (
                f"Inspect {target_text} from the current screen and report what is visible, "
                "whether it is ready for the active mission, and any blocker. "
                "Do not click, type, paste, submit, send, run, or change content."
            )
        if intent.intent == "ui.paste_text":
            if not artifact_text:
                return None
            return (
                f"Focus the appropriate input in {target_text} and paste the following text exactly. "
                "Do not press Enter. Do not click Send. Do not submit, run, or execute it.\n\n"
                "<text_to_paste>\n"
                f"{artifact_text}\n"
                "</text_to_paste>"
            )
        if intent.intent == "ui.set_instructions":
            if not artifact_text:
                return None
            return (
                f"Focus the instruction or prompt input in {target_text} and paste these instructions exactly. "
                "Do not press Enter. Do not click Send. Do not submit, run, or execute it.\n\n"
                "<instructions_to_paste>\n"
                f"{artifact_text}\n"
                "</instructions_to_paste>"
            )
        if intent.intent == "ui.submit_prompt":
            return (
                f"In {target_text}, submit or run the already prepared prompt by pressing Enter "
                "or clicking the app's submit/run/send control. Do not edit or replace the prompt text."
            )
        return None

    @staticmethod
    def _classify_computer_handler_result(result: str | None) -> str:
        text = str(result or "").strip()
        normalized = _normalize_command_text(text)
        if not text:
            return "failed"
        if (
            "necesito tu autorizacion" in normalized
            or "needs approval" in normalized
            or "requires approval" in normalized
            or "awaiting approval" in normalized
        ):
            return "pending_approval"
        if "unavailable" in normalized or "desactivado" in normalized:
            return "blocked_by_capability"
        if (
            normalized.startswith("computer use error")
            or normalized.startswith("computer screenshot error")
            or normalized.startswith("screenshot error")
            or "sin un resultado verificable" in normalized
            or " timed out" in normalized
        ):
            return "failed"
        return "succeeded"

    def _local_ui_read_available(self) -> bool:
        return self.computer is not None and self._capability_available("computer_use")

    def _local_ui_write_available(self) -> bool:
        return self.computer is not None and self._capability_available("computer_control")

    def _missing_ui_capability(self, intent: TelegramImperativeIntent) -> str | None:
        if intent.requires_ui_write and not self._local_ui_write_available():
            return "local_ui_write"
        if intent.requires_ui_read and not self._local_ui_read_available():
            return "local_ui_read"
        return None

    def _record_telegram_imperative_task(
        self,
        *,
        session_id: str,
        text: str,
        intent: TelegramImperativeIntent,
        target: str | None,
        artifact: str | None,
        result_status: str,
        capability: str | None,
        summary: str,
        handler_result: str | None = None,
        approval_id: str | None = None,
        execution_backend: str | None = None,
        notify_policy: str = "done_only",
    ) -> str:
        task_id = f"{session_id}:telegram-imperative:{time.time_ns()}"
        action_result = {
            "status": result_status,
            "intent": intent.intent,
            "target": target,
            "artifact": artifact,
            "capability": capability,
            "approval_id": approval_id,
            "execution_backend": execution_backend,
        }
        artifacts = {
            "action_result": action_result,
            "substeps": [
                {
                    "action": intent.intent,
                    "status": result_status,
                    "reason": capability or result_status,
                }
            ],
            "evidence": {"router_result": action_result},
        }
        if handler_result:
            artifacts["handler_result"] = str(handler_result)[:4000]
        metadata = {
            "origin": "telegram_imperative_router",
            "intent": intent.intent,
            "target": target,
            "artifact": artifact,
            "source_message": text[:500],
            "result_status": result_status,
            "blocked_reason": result_status if result_status.startswith("blocked") else "",
            "approval_id": approval_id,
            "execution_backend": execution_backend,
        }
        if self.task_ledger is not None:
            self.task_ledger.create(
                task_id=task_id,
                session_id=session_id,
                objective=self._objective_for_imperative(intent, target=target, artifact=artifact),
                runtime="telegram_imperative",
                mode="ops",
                status="running",
                notify_policy=notify_policy,
                metadata=metadata,
                artifacts=artifacts,
            )
            if result_status != "pending_approval":
                terminal_status = "failed" if result_status.startswith("blocked") or result_status == "failed" else "succeeded"
                if result_status == "succeeded":
                    verification = "passed"
                elif result_status == "partial_success":
                    verification = "pending"
                else:
                    verification = result_status
                self.task_ledger.mark_terminal(
                    task_id,
                    status=terminal_status,
                    summary=summary,
                    error=capability or "",
                    verification_status=verification,
                    artifacts=artifacts,
                )
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["last_action_result"] = {
            "task_id": task_id,
            **action_result,
            "updated_at": time.time(),
        }
        self.brain.memory.update_session_state(
            session_id,
            verification_status=result_status,
            active_object=active_object,
            last_checkpoint={
                "summary": summary,
                "verification_status": result_status,
                "task_id": task_id,
                "intent": intent.intent,
                "target": target,
                "artifact": artifact,
            },
        )
        return task_id

    def _maybe_handle_owner_delegation_request(
        self,
        text: str,
        *,
        session_id: str,
        runtime_channel: str | None,
    ) -> str | None:
        """PR 0C — owner-delegation kernel.

        Detects "córrelo tú", "decide tú", "te toca a ti", etc. and turns
        them into durable, task-scoped delegated work. Runs BEFORE
        actionable_task_request / task_intent / capability_route / brain
        fallback so it cannot be silenced by CLAW_DISABLE_* flags or by an
        assisted session.

        Resolution and safety policy live in StateHandler.resolve_delegated_objective
        and bot_helpers.is_destructive_or_external_objective. This method
        is just the dispatcher: classify → resolve → branch (safe → start
        delegated task; risky → approval question; unresolved → clarify).
        """
        intent = detect_owner_delegation(text)
        if intent is None:
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "owner_delegation_no_match",
                        payload={"session_id": session_id},
                    )
                except Exception:
                    logger.debug("owner_delegation_no_match emit failed", exc_info=True)
            return None
        if self.observe is not None:
            try:
                self.observe.emit(
                    "owner_delegation_match",
                    payload={
                        "session_id": session_id,
                        "kind": intent.kind,
                        "confidence": intent.confidence,
                        "normalized_text": intent.normalized_text,
                        "requires_resolution": intent.requires_resolution,
                        "explicit_action_hint": intent.explicit_action_hint,
                        "runtime_channel": (runtime_channel or "").lower() or None,
                    },
                )
            except Exception:
                logger.debug("owner_delegation_match emit failed", exc_info=True)
        self._emit_dispatch_decision(
            handler="owner_delegation",
            route="intercepted",
            reason=f"owner_delegation:{intent.kind}",
            session_id=session_id,
            text=text,
            captured=True,
            matched_pattern=f"owner_delegation:{intent.kind}",
        )
        resolution = self._state_handler.resolve_delegated_objective(
            session_id=session_id, text=text, intent=intent
        )
        # Unresolved → one concrete clarifying question. We do NOT say
        # "decide tú" / "elige tú" — the resolver enforces that.
        if resolution.objective is None:
            event_name = (
                "owner_delegation_approval_required"
                if resolution.is_risky
                else "owner_delegation_unresolved"
            )
            if self.observe is not None:
                try:
                    self.observe.emit(
                        event_name,
                        payload={
                            "session_id": session_id,
                            "kind": intent.kind,
                            "resolution_source": resolution.resolution_source,
                        },
                    )
                except Exception:
                    logger.debug("%s emit failed", event_name, exc_info=True)
            return resolution.clarifying_question or (
                "Lo tomo como tuyo, pero dime en una frase imperativa que "
                "accion concreta ejecuto."
            )
        # Risky / external / destructive → ask one approval question.
        if resolution.is_risky:
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "owner_delegation_approval_required",
                        payload={
                            "session_id": session_id,
                            "kind": intent.kind,
                            "resolution_source": resolution.resolution_source,
                            "mode": resolution.mode,
                        },
                    )
                except Exception:
                    logger.debug(
                        "owner_delegation_approval_required emit failed", exc_info=True
                    )
            objective_preview = (resolution.objective or "")[:200]
            return (
                "Lo que pides toca algo externo, destructivo o con costo "
                "(deploy / merge / publish / send / payment / secret / "
                "delete / production). No lo ejecuto sin tu aprobacion "
                "explicita.\n\n"
                f"Objetivo resuelto: {objective_preview}\n\n"
                "Responde \"aprobado\" para que proceda, o aclara el alcance."
            )
        # Safe + resolved → start a durable task-scoped delegated coordinator
        # run. We intentionally do NOT flip the session autonomy_mode; the
        # autonomy is scoped to this task via delegation_metadata.
        delegation_metadata = {
            "owner_delegation": True,
            "delegated_by_owner": True,
            "source_message": text[:500],
            "resolution_source": resolution.resolution_source,
            "verification_required": True,
            "autonomy_scope": "task",
            "manual_handoff_forbidden": True,
            "delegation_kind": intent.kind,
            "runtime_channel": (runtime_channel or "").lower() or None,
        }
        try:
            response = self._task_handler.start_autonomous_task(
                session_id,
                resolution.objective,
                mode=resolution.mode,
                delegation_metadata=delegation_metadata,
            )
        except Exception:
            logger.exception("owner_delegation start_autonomous_task failed")
            if self.observe is not None:
                try:
                    self.observe.emit(
                        "owner_delegation_blocked",
                        payload={
                            "session_id": session_id,
                            "kind": intent.kind,
                            "resolution_source": resolution.resolution_source,
                            "reason": "start_autonomous_task_exception",
                        },
                    )
                except Exception:
                    logger.debug("owner_delegation_blocked emit failed", exc_info=True)
            return (
                "Intente arrancar la tarea delegada pero el coordinador "
                "fallo. Reporto sin disimulo: revisa los logs y dime si "
                "reintento."
            )
        if self.observe is not None:
            try:
                self.observe.emit(
                    "owner_delegation_started_task",
                    payload={
                        "session_id": session_id,
                        "kind": intent.kind,
                        "resolution_source": resolution.resolution_source,
                        "mode": resolution.mode,
                    },
                )
            except Exception:
                logger.debug("owner_delegation_started_task emit failed", exc_info=True)
        prefix = "Lo tomo como tarea delegada.\n\n"
        return prefix + (response or "")

    def _maybe_handle_actionable_task_request(
        self,
        text: str,
        *,
        session_id: str,
        runtime_channel: str | None,
        semantic_turn: SemanticTurn | None = None,
    ) -> str | None:
        if (runtime_channel or "").strip().lower() != "telegram":
            return None
        if not text or text.startswith("/"):
            return None
        if (
            semantic_turn is not None
            and semantic_turn.intent in {"continue_active_mission", "approval_response"}
            and not self._looks_like_direct_actionable_task(text)
        ):
            self._emit_safe(
                "actionable_task_router_skipped_semantic_continuation",
                {
                    "session_id": session_id,
                    "semantic_intent": semantic_turn.intent,
                    "text_preview": text[:80],
                },
            )
            return None
        state = self.brain.memory.get_session_state(session_id)
        objective, source = self._resolve_actionable_task_objective(text, state=state)
        if objective is None:
            if self._looks_like_actionable_followup(text):
                continuation, continuation_source = self._maybe_resolve_telegram_continuation(
                    text,
                    session_id=session_id,
                )
                if isinstance(continuation, _BrainShortcut):
                    self._emit_safe(
                        "telegram_continuation_stateful_resolved",
                        {
                            "session_id": session_id,
                            "intent": "task.continue_active_mission",
                            "resolution_source": continuation_source,
                            "resolution_kind": "brain_shortcut",
                        },
                    )
                    state = self.brain.memory.get_session_state(session_id)
                    objective, source = self._resolve_actionable_task_objective(text, state=state)
            if objective is None and self._looks_like_actionable_followup(text):
                # SOUL routing policy: unresolved follow-ups fall through to
                # the brain instead of clarifying pre-brain.
                self._emit_safe(
                    "actionable_task_contextual_fallthrough",
                    {"session_id": session_id, "reason": "actionable_task_context_not_found"},
                )
                return None
            if objective is None:
                return None
        if os.getenv("CLAW_DISABLE_TELEGRAM_ACTIONABLE_TASK_ROUTER", "0") == "1":
            self._emit_safe(
                "actionable_task_router_disabled",
                {"session_id": session_id, "source": source, "objective_preview": objective[:160]},
            )
            return (
                "El router autonomo de Telegram esta desactivado por configuracion. "
                "No voy a tratar esto como chat: usa `/task_run <objetivo>` o habilita la ruta controlada."
            )
        preflight = self._run_capability_preflight(objective, session_id=session_id)
        mode = _infer_session_mode(objective)
        if mode not in {"coding", "research"}:
            mode = "coding"
        if preflight.blockers:
            self._task_handler.record_blocked_task(
                session_id,
                objective,
                source_text=text,
                mode=mode,
                task_kind=preflight.task_kind,
                risk_tier=preflight.risk_tier,
                plan=preflight.plan,
                verification_requirement=preflight.verification_requirement,
                blockers=preflight.blockers,
                preflight=preflight.to_dict(),
            )
            return self._format_preflight_blocked_response(preflight)
        if getattr(self._task_handler, "coordinator", None) is None:
            blocker_result = preflight.to_dict()
            blocker_result["blockers"] = ["coordinator_unavailable"]
            self._task_handler.record_blocked_task(
                session_id,
                objective,
                source_text=text,
                mode=mode,
                task_kind=preflight.task_kind,
                risk_tier=preflight.risk_tier,
                plan=preflight.plan,
                verification_requirement=preflight.verification_requirement,
                blockers=["coordinator_unavailable"],
                preflight=blocker_result,
            )
            return "Creé la tarea, pero quedó bloqueada porque el coordinador autónomo no está disponible."
        self._task_handler.start_autonomous_task(
            session_id,
            objective,
            mode=mode,
            source_text=text,
            task_kind=preflight.task_kind,
            risk_tier=preflight.risk_tier,
            preflight=preflight.to_dict(),
            plan=preflight.plan,
            verification_requirement=preflight.verification_requirement,
        )
        return "Tomado. Creé una tarea autónoma; voy a ejecutar lo permitido y registrar blockers con evidencia."

    def _resolve_actionable_task_objective(
        self,
        text: str,
        *,
        state: dict[str, Any],
    ) -> tuple[str | None, str]:
        if self._looks_like_direct_actionable_task(text):
            return text.strip(), "direct_message"
        if not self._looks_like_actionable_followup(text):
            return None, "no_match"
        pending_action = str(state.get("pending_action") or "").strip()
        if pending_action:
            return pending_action, "pending_action"
        checkpoint = state.get("last_checkpoint") or {}
        if isinstance(checkpoint, dict):
            checkpoint_action = str(checkpoint.get("pending_action") or "").strip()
            if checkpoint_action:
                return checkpoint_action, "last_checkpoint"
        current_goal = str(state.get("current_goal") or "").strip()
        if (
            current_goal
            and self._looks_like_persistable_current_goal(current_goal)
            and not self._looks_like_actionable_followup(current_goal)
        ):
            return current_goal, "current_goal"
        active_object = state.get("active_object") or {}
        if isinstance(active_object, dict):
            active_task = active_object.get("active_task") or {}
            if isinstance(active_task, dict):
                objective = str(active_task.get("objective") or "").strip()
                status = str(active_task.get("status") or "").strip()
                if objective and status not in {"completed", "succeeded"}:
                    return objective, "active_task"
        return None, "missing_context"

    @staticmethod
    def _looks_like_persistable_current_goal(text: str) -> bool:
        normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
        if len(normalized) < 8 or _looks_like_proceed_request(text):
            return False
        complaint_markers = (
            "no has ",
            "no haz ",
            "no habias ",
            "no abriste",
            "no has abierto",
            "mentiste",
            "deja de mentir",
            "porque perdiste",
            "perdiste el contexto",
            "eso no es",
        )
        if any(marker in normalized for marker in complaint_markers):
            return False
        goal_markers = (
            "abre",
            "abrir",
            "open",
            "focus",
            "inspect",
            "revisa",
            "revisar",
            "lee ",
            "leer ",
            "busca",
            "buscar",
            "crea",
            "crear",
            "implementa",
            "implementar",
            "arregla",
            "corrige",
            "actualiza",
            "ejecuta",
            "corre",
            "verifica",
            "valida",
            "termina",
            "completa",
            "regenera",
            "haz ",
            "hacer ",
        )
        return any(marker in normalized for marker in goal_markers)

    @staticmethod
    def _looks_like_direct_actionable_task(text: str) -> bool:
        normalized = _normalize_command_text(text)
        # "pr" must be a standalone token, not a substring of "pregunta",
        # "preocupa", "preferir", etc. Same for the action verbs: require a
        # word-boundary start so "completas" (2nd-person present) does not
        # trip the PR-completion branch when paired with "pregunta".
        pr_completion = (
            re.search(r"\bpr\b", normalized) is not None
            and re.search(r"\b(termina|completa|finaliza)", normalized) is not None
        )
        return (
            ("actualiza" in normalized and any(token in normalized for token in ("codex", "claude", "codex app")))
            or ("regenera" in normalized and "lock" in normalized)
            or ("poetry.lock" in normalized)
            or ("pyproject" in normalized and "lock" in normalized)
            or pr_completion
        )

    @staticmethod
    def _looks_like_actionable_followup(text: str) -> bool:
        normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
        if _looks_like_proceed_request(text):
            return True
        return any(
            phrase in normalized
            for phrase in (
                "debes hacerlo tu",
                "debes hacerla tu",
                "debes hacerlas tu",
                "debes actualizarlas tu",
                "debes actualizarlos tu",
                "actualizalas tu",
                "actualizalos tu",
                "hazlo tu",
                "ejecutalo tu",
                "ejecutala tu",
            )
        )

    def _run_capability_preflight(self, objective: str, *, session_id: str) -> CapabilityPreflightResult:
        self._emit_safe(
            "capability_preflight_started",
            {"session_id": session_id, "objective_preview": objective[:160]},
        )
        workspace_root = Path(getattr(self.config, "workspace_root", None) or Path.cwd())
        profile = str(getattr(self.config, "sandbox_capability_profile", "engineer") or "engineer")
        result = preflight_objective(
            objective,
            workspace_root=workspace_root,
            capability_profile=profile,
        )
        payload = result.to_dict()
        payload["session_id"] = session_id
        payload["objective_preview"] = objective[:160]
        self._emit_safe("capability_preflight_result", payload)
        for blocker in result.blockers:
            self._emit_safe(
                "tool_blocker_detected",
                {
                    "session_id": session_id,
                    "task_kind": result.task_kind,
                    "blocker": blocker[:300],
                },
            )
        return result

    def _format_preflight_blocked_response(self, preflight: CapabilityPreflightResult) -> str:
        details: list[str] = []
        for check in preflight.checks:
            if not check.blocker:
                continue
            if check.status == "command_not_found":
                details.append(f"{check.binary}: no disponible")
            elif check.status == "policy_blocked":
                details.append(f"{check.binary}: bloqueado por política")
            else:
                details.append(f"{check.binary}: {check.status}")
        if not details:
            details = ["capacidad requerida no disponible"]
        return (
            "Creé la tarea, pero quedó bloqueada en preflight antes de ejecutar.\n"
            + "\n".join(f"- {item}" for item in details[:5])
            + "\nPara continuar necesito que habilites el comando específico o me des una ruta segura alternativa."
        )

    def _emit_safe(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(event_type, payload=payload)
        except Exception:
            logger.debug("failed to emit %s", event_type, exc_info=True)

    def _maybe_handle_operational_status(self, text: str, *, session_id: str) -> str | None:
        normalized = _normalize_command_text(text).strip()
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        status_phrases = {
            "status",
            "estado",
            "estatus",
            "estas",
            "estas?",
            "estas ?",
            "estas vivo",
            "estas viva",
            "estas ahi",
            "estas ahi?",
            "estas ahi ?",
            "ping",
            "como vamos",
            "cómo vamos",
            "que hay pendiente",
            "qué hay pendiente",
            "daily status",
        }
        greeting_status = (
            any(greeting in normalized for greeting in ("buen dia", "buenos dias", "good morning", "hola"))
            and any(token in normalized for token in ("status", "estado", "estatus"))
        )
        contains_status_request = normalized in status_phrases or compact in {
            "estas",
            "estasvivo",
            "estasviva",
            "estasahi",
            "buendiastatus",
            "buenosdiasstatus",
            "dailystatus",
            "comovamos",
            "quehaypendiente",
        }
        if not (contains_status_request or greeting_status):
            return None
        active_count = 0
        latest_line = "sin tareas recientes"
        if self.task_ledger is not None:
            records = self.task_ledger.list(session_id=session_id, limit=5)
            active = [record for record in records if getattr(record, "status", "") in {"queued", "running"}]
            active_count = len(active)
            if records:
                latest = active[0] if active else records[0]
                latest_line = (
                    f"{getattr(latest, 'status', 'unknown')} / "
                    f"{getattr(latest, 'verification_status', 'unknown') or 'unknown'} — "
                    f"`{getattr(latest, 'task_id', 'unknown')}`"
                )
        web_port = int(getattr(self.config, "web_chat_port", 8765)) if self.config is not None else 8765
        runtime = "vivo" if self._runtime_alive() else "sin respuesta local"
        try:
            approvals = self.approvals.list_pending() if self.approvals is not None else []
        except Exception:
            approvals = []
        lines = [
            "Estoy vivo.",
            f"Runtime local: {runtime} en :{web_port}",
            f"Tareas activas en esta sesión: {active_count}",
            f"Última tarea: {latest_line}",
        ]
        if approvals:
            lines.extend(self._approval_summary_lines(approvals))
        else:
            lines.append("Aprobaciones pendientes: 0")
        lines.append("Comandos útiles: `/jobs`, `/tasks`, `/quality`, `/restart`.")
        return "\n".join(lines)

    def _maybe_handle_operational_failure_summary(self, text: str, *, session_id: str) -> str | None:
        normalized = _normalize_command_text(text).strip()
        if not self._matches_operational_failure_summary_request(normalized):
            return None
        return self._format_operational_failure_summary(session_id)

    @staticmethod
    def _matches_operational_failure_summary_request(normalized: str) -> bool:
        if not normalized:
            return False

        def _contains_report_term(term: str) -> bool:
            if " " in term:
                return term in normalized
            return re.search(
                rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])",
                normalized,
            ) is not None

        stop_or_scope_markers = (
            "no continuemos",
            "no sigamos",
            "no avancemos",
            "paremos",
            "detengamos",
            "dejemos esto",
        )
        if any(marker in normalized for marker in stop_or_scope_markers):
            return False
        specific_task_failure = (
            "por que fallo la tarea",
            "porque fallo la tarea",
            "por que fallo el task",
            "porque fallo el task",
            "por que fallaste la tarea",
            "porque fallaste la tarea",
        )
        broad_report_markers = (
            "resumen",
            "recuento",
            "auditoria",
            "auditoría",
            "fallos",
            "errores",
            "hoy",
            "sesion",
            "sesión",
            "ninguna tarea",
            "tareas",
        )
        if any(phrase in normalized for phrase in specific_task_failure) and not any(
            marker in normalized for marker in broad_report_markers
        ):
            return False
        direct_complaints = (
            "no puede completar ninguna tarea",
            "no puedes completar ninguna tarea",
            "no estas completando ninguna tarea",
            "no estás completando ninguna tarea",
            "no completa ninguna tarea",
            "no estas completando tareas",
            "no estás completando tareas",
        )
        if any(phrase in normalized for phrase in direct_complaints):
            return True
        if (
            "ninguna tarea" not in normalized
            and (
                "por que no completas" in normalized
                or "por qué no completas" in normalized
                or "porque no completas" in normalized
            )
        ):
            return False
        failure_terms = (
            "fallo",
            "fallos",
            "fallaste",
            "fallado",
            "error",
            "errores",
            "problema",
            "problemas",
            "perdido",
            "perdida",
            "bloqueo",
            "bloqueos",
            "no complet",
        )
        explicit_report_terms = (
            "resumen",
            "recuento",
            "auditoria",
            "auditoría",
            "reporte",
            "explica",
            "hoy",
            "sesion",
            "sesión",
            "today",
            "summary",
        )
        causal_terms = ("porque", "por que", "por qué")
        task_context_terms = (
            "agente",
            "bot",
            "daemon",
            "tarea",
            "tareas",
            "task",
            "complet",
            "fallaste",
            "fallo",
            "fallos",
            "error",
            "errores",
            "bloqueo",
            "bloqueos",
        )
        has_failure_term = any(term in normalized for term in failure_terms)
        if not has_failure_term:
            return False
        if any(_contains_report_term(term) for term in explicit_report_terms):
            return True
        return any(term in normalized for term in causal_terms) and any(
            term in normalized for term in task_context_terms
        )

    def _format_operational_failure_summary(self, session_id: str) -> str:
        today_prefix = time.strftime("%Y-%m-%d", time.gmtime())
        events = self._recent_today_observe_events(limit=700, today_prefix=today_prefix)
        event_counts: dict[str, int] = {}
        for event in events:
            event_type = str(event.get("event_type") or "")
            event_counts[event_type] = event_counts.get(event_type, 0) + 1

        recent_messages = self._recent_session_messages(session_id, limit=80)
        defensive_template_count = sum(
            1
            for msg in recent_messages
            if msg.get("role") == "assistant"
            and "no digo `arrancando`" in _normalize_command_text(str(msg.get("content") or ""))
        )
        clarification_count = sum(
            1
            for msg in recent_messages
            if msg.get("role") == "assistant"
            and (
                "que accion concreta quieres que ejecute" in _normalize_command_text(str(msg.get("content") or ""))
                or "necesito una aclaracion minima" in _normalize_command_text(str(msg.get("content") or ""))
            )
        )

        lines = [
            f"Resumen operativo de fallos de hoy ({today_prefix}), con evidencia local:",
        ]
        findings: list[str] = []
        imperative_bounces = (
            event_counts.get("telegram_imperative_clarification", 0)
            + event_counts.get("active_mission_resolution_failed", 0)
        )
        if imperative_bounces:
            findings.append(
                f"Continuidad: {imperative_bounces} evento(s) de continuación terminaron en aclaración/bounce en vez de usar contexto."
            )
        blocked_start = event_counts.get("evidence_gate_blocked_start_claim", 0)
        if blocked_start or defensive_template_count:
            findings.append(
                "Gate de evidencia: "
                f"{blocked_start} bloqueo(s) observados y {defensive_template_count} respuesta(s) visibles con plantilla defensiva."
            )
        if clarification_count:
            findings.append(
                f"Contexto conversacional: {clarification_count} respuesta(s) pidieron una acción concreta aunque había contexto previo."
            )
        routed_continuations = event_counts.get("stateful_continuation_routed_to_actionable_task", 0)
        if routed_continuations:
            findings.append(
                f"Continuación stateful: {routed_continuations} continuación(es) ya fueron convertidas en tarea durable."
            )
        worker_retry = event_counts.get("coordinator_worker_retry", 0)
        if worker_retry:
            latest_retry = self._latest_event_payload(events, "coordinator_worker_retry")
            retry_error = _compact_summary(
                str(redact_sensitive(latest_retry.get("error") or "", limit=240)),
                limit=180,
            )
            findings.append(
                f"Coordinador: {worker_retry} retry(s) de worker; último error: {retry_error or 'sin detalle'}."
            )
        autostash = event_counts.get("worktree_autostash", 0)
        if autostash:
            findings.append(
                f"Worktree: {autostash} autostash registrado; eso puede ocultar cambios dirty durante una tarea autónoma."
            )
        if not findings:
            findings.append(
                "No encontré fallos operativos recientes en la telemetría local; "
                "revisé el historial de tareas y mensajes recientes."
            )
        lines.extend(f"- {finding}" for finding in findings[:6])

        task_lines = self._operational_failure_task_lines(session_id)
        if task_lines:
            lines.append("")
            lines.append("Tareas recientes:")
            lines.extend(task_lines)
        lines.append("")
        lines.append(
            "Diagnóstico: los reportes de fallos deben salir del resumen operativo verificado, "
            "no de una respuesta genérica. No debo reportar tareas como cerradas sin verificación final."
        )
        return "\n".join(lines)

    def _recent_today_observe_events(self, *, limit: int, today_prefix: str) -> list[dict[str, Any]]:
        if self.observe is None:
            return []
        try:
            events = self.observe.recent_events(limit=limit)
        except Exception:
            logger.debug("failed to read observe_stream for failure summary", exc_info=True)
            return []
        today = [
            event
            for event in events
            if str(event.get("timestamp") or "").startswith(today_prefix)
        ]
        return today or events

    def _recent_session_messages(self, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        try:
            return list(self.brain.memory.get_recent_messages(session_id, limit=limit))
        except Exception:
            logger.debug("failed to read recent messages for failure summary", exc_info=True)
            return []

    @staticmethod
    def _latest_event_payload(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
        for event in events:
            if event.get("event_type") == event_type:
                payload = event.get("payload") or {}
                return payload if isinstance(payload, dict) else {}
        return {}

    def _operational_failure_task_lines(self, session_id: str) -> list[str]:
        if self.task_ledger is None:
            return []
        try:
            records = self.task_ledger.list(session_id=session_id, limit=6)
        except Exception:
            logger.debug("failed to read task ledger for failure summary", exc_info=True)
            return []
        if not records:
            return ["- Sin tareas recientes en esta sesión."]
        lines: list[str] = []
        for record in records[:4]:
            status = self._public_operational_task_state(
                str(getattr(record, "status", "unknown") or "unknown")
            )
            verification = self._public_operational_task_state(
                str(getattr(record, "verification_status", "unknown") or "unknown")
            )
            objective = _compact_summary(
                str(redact_sensitive(getattr(record, "objective", "") or "", limit=300)),
                limit=180,
            )
            error = _compact_summary(
                str(redact_sensitive(getattr(record, "error", "") or "", limit=240)),
                limit=140,
            )
            detail = f" - {error}" if error else ""
            lines.append(
                f"- {status} / {verification}: {objective or 'sin objetivo'}{detail}"
            )
        return lines

    @staticmethod
    def _public_operational_task_state(value: str) -> str:
        normalized = str(value or "").strip().lower()
        return {
            "completed_unverified": "completada sin verificación final",
            "needs_verification": "pendiente de verificación",
            "running_needs_verification": "en verificación",
            "succeeded": "completada",
            "passed": "verificada",
            "failed": "fallida",
            "blocked": "bloqueada",
            "running": "en curso",
            "queued": "en cola",
            "timed_out": "agotada por tiempo",
            "cancelled": "cancelada",
            "lost": "sin estado ejecutable",
            "unknown": "desconocida",
        }.get(normalized, normalized.replace("_", " ") or "desconocida")

    def _maybe_handle_boot_context_status(
        self,
        text: str,
        *,
        session_id: str,
        runtime_channel: str | None = None,
    ) -> str | None:
        normalized = _normalize_command_text(text).strip()
        asks_boot = any(term in normalized for term in ("arranque", "arrancar", "boot", "startup"))
        asks_context = "contexto" in normalized or "fuentes" in normalized or "cargaste" in normalized or "cargado" in normalized
        if not asks_boot or not asks_context:
            return None
        if self.observe is None:
            return "No encontré observe_stream disponible para verificar el boot actual."
        try:
            events = self.observe.recent_events(limit=300)
        except Exception as exc:
            return f"No pude leer observe_stream para verificar el boot actual: {type(exc).__name__}."
        event = next((item for item in events if item.get("event_type") == "agent_startup_context"), None)
        if event is None:
            return (
                "No hay evento `agent_startup_context` en `observe_stream` para este proceso. "
                "No voy a afirmar que el boot nuevo está cargado sin esa evidencia."
            )
        payload = event.get("payload") or {}
        loaded_files = list(payload.get("loaded_files") or [])
        daily_files = list(payload.get("daily_memory_files") or [])
        missing_files = list(payload.get("missing_files") or [])
        missing_line = ", ".join(missing_files) if missing_files else "none"
        loaded_line = ", ".join(loaded_files[:12]) if loaded_files else "none"
        daily_line = ", ".join(daily_files[:8]) if daily_files else "none"
        channel = runtime_channel or payload.get("channel") or "unknown"
        return (
            "Boot observable verificado desde `agent_startup_context`.\n"
            f"- boot_context_version: `{payload.get('boot_context_version', 'unknown')}`\n"
            f"- boot_protocol_version: `{payload.get('boot_protocol_version', 'unknown')}`\n"
            f"- startup_context_used: `{str(payload.get('startup_context_used', False)).lower()}`\n"
            f"- stable_context_used: `{str(payload.get('stable_context_used', False)).lower()}`\n"
            f"- boot_protocol_loaded: `{str(payload.get('boot_protocol_loaded', False)).lower()}`\n"
            f"- task_ledger_loaded: `{str(payload.get('task_ledger_loaded', False)).lower()}`\n"
            f"- session_state_loaded: `{str(payload.get('session_state_loaded', False)).lower()}`\n"
            f"- daily_memory_loaded: `{str(payload.get('daily_memory_loaded', False)).lower()}` ({daily_line})\n"
            f"- context_truncated: `{str(payload.get('context_truncated', False)).lower()}`\n"
            f"- workspace_root: `{payload.get('workspace_root') or payload.get('root')}`\n"
            f"- cwd: `{payload.get('cwd', 'unknown')}`\n"
            f"- pid: `{payload.get('pid', 'unknown')}`\n"
            f"- code_version: `{payload.get('code_version', 'unknown')}`\n"
            f"- current_channel: `{channel}`\n"
            f"- loaded_sources: {loaded_line}\n"
            f"- missing_sources: {missing_line}\n"
            "No imprimí contenido privado ni secretos; solo nombres de fuentes y estado de carga."
        )

    def _task_status_question_response(self, session_id: str) -> str:
        latest = None
        if self.task_ledger is not None:
            records = self.task_ledger.list(session_id=session_id, limit=5)
            active = [record for record in records if getattr(record, "status", "") in {"queued", "running"}]
            latest = active[0] if active else (records[0] if records else None)
        if latest is not None:
            status = str(getattr(latest, "status", "unknown"))
            verification = str(getattr(latest, "verification_status", "unknown") or "unknown")
            objective = str(getattr(latest, "objective", "") or "").strip()
            summary = str(getattr(latest, "summary", "") or "").strip()
            error = str(getattr(latest, "error", "") or "").strip()
            task_id = str(getattr(latest, "task_id", "") or "")
            detail = summary or objective
            if status in {"queued", "running"}:
                return (
                    "Todavía no. La tarea más reciente sigue activa.\n"
                    f"Task: `{task_id}`\n"
                    f"Estado: {status} / verificación: {verification}\n"
                    f"Detalle: {detail[:500] or 'sin resumen'}"
                )
            if status == "succeeded":
                return (
                    "Sí. La tarea más reciente cerró correctamente.\n"
                    f"Task: `{task_id}`\n"
                    f"Verificación: {verification}\n"
                    f"Resultado: {detail[:500] or 'sin resumen'}"
                )
            if verification == "blocked":
                return (
                    "No quedó ejecutada; quedó bloqueada.\n"
                    f"Task: `{task_id}`\n"
                    f"Motivo: {(error or detail)[:500] or 'faltó información ejecutable'}"
                )
            return (
                "No. La tarea más reciente ya no está activa, pero no cerró como completada.\n"
                f"Task: `{task_id}`\n"
                f"Estado: {status} / verificación: {verification}\n"
                f"Detalle: {(error or detail)[:500] or 'sin detalle'}"
            )
        state = self.brain.memory.get_session_state(session_id)
        active_task = dict((state.get("active_object") or {}).get("active_task") or {})
        if active_task:
            return (
                "Tengo una tarea en estado de sesión, pero no aparece en el ledger.\n"
                f"Task: `{active_task.get('task_id', 'unknown')}`\n"
                f"Estado: {active_task.get('status', 'unknown')}\n"
                f"Objetivo: {active_task.get('objective', 'sin objetivo')}"
            )
        return "No tengo tareas registradas para esta conversación."

    def _change_status_question_response(self, session_id: str) -> str:
        records = self.task_ledger.list(session_id=session_id, limit=20) if self.task_ledger is not None else []
        relevant = [record for record in records if not _looks_like_change_status_question(str(getattr(record, "objective", "") or ""))]
        terminal = [
            record for record in relevant
            if getattr(record, "status", "") in {"succeeded", "failed", "timed_out", "cancelled", "lost"}
            and self._is_change_status_relevant_record(record)
        ][:5]
        active = [
            record for record in relevant
            if getattr(record, "status", "") in {"queued", "running"}
            and self._is_change_status_relevant_record(record)
        ][:3]
        ignored_status_queries = len(records) - len(relevant)
        commits = self._recent_workspace_commits(limit=5)

        lines = ["Estatus de cambios:"]
        if commits:
            lines.append("Commits recientes en HEAD:")
            lines.extend(f"- `{sha}` — {subject}" for sha, subject in commits)
        if terminal:
            lines.append("Tareas cerradas relevantes:")
            lines.extend(self._format_task_status_line(record) for record in terminal)
        if active:
            lines.append("Tareas abiertas relevantes:")
            lines.extend(self._format_task_status_line(record) for record in active)
        if ignored_status_queries:
            plural = "s" if ignored_status_queries != 1 else ""
            lines.append(f"Ignoré {ignored_status_queries} consulta{plural} de estatus abierta{plural}; eso no cuenta como cambio pendiente.")
        if len(lines) == 1:
            return "No encontré commits ni tareas cerradas recientes para los fixes/cambios de esta sesión."
        return "\n".join(lines)

    def _is_change_status_relevant_record(self, record: Any) -> bool:
        text = " ".join(
            str(value or "")
            for value in (
                getattr(record, "objective", ""),
                getattr(record, "summary", ""),
                getattr(record, "error", ""),
                getattr(record, "mode", ""),
            )
        )
        normalized = _normalize_command_text(text)
        return any(token in normalized for token in ("fix", "fixes", "cambio", "cambios", "bug", "codigo", "código", "handler"))

    def _format_task_status_line(self, record: Any) -> str:
        status = str(getattr(record, "status", "unknown") or "unknown")
        verification = str(getattr(record, "verification_status", "unknown") or "unknown")
        task_id = str(getattr(record, "task_id", "unknown") or "unknown")
        detail = str(getattr(record, "summary", "") or getattr(record, "objective", "") or "sin resumen").strip()
        return f"- `{task_id}` — {status} / {verification}: {detail[:220]}"

    def _recent_workspace_commits(self, *, limit: int = 5) -> list[tuple[str, str]]:
        workspace_root = Path(getattr(self.config, "workspace_root", None) or Path.cwd())
        try:
            result = subprocess.run(
                ["git", "-C", str(workspace_root), "log", f"-n{max(1, min(int(limit), 20))}", "--pretty=format:%h%x00%s"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        commits: list[tuple[str, str]] = []
        for line in result.stdout.splitlines():
            if "\x00" not in line:
                continue
            sha, subject = line.split("\x00", 1)
            sha = sha.strip()
            subject = subject.strip()
            if sha and subject:
                commits.append((sha, subject))
        return commits

    def _latest_relevant_task(self, session_id: str) -> Any | None:
        if self.task_ledger is None:
            return None
        records = self.task_ledger.list(session_id=session_id, limit=10)
        if not records:
            return None
        return records[0]

    def _task_failure_diagnostic_response(self, session_id: str) -> str:
        latest = self._latest_relevant_task(session_id)
        if latest is None:
            return "No encontré una tarea reciente para diagnosticar en esta conversación."
        status = str(getattr(latest, "status", "unknown"))
        verification = str(getattr(latest, "verification_status", "unknown") or "unknown")
        error = str(getattr(latest, "error", "") or "").strip()
        summary = str(getattr(latest, "summary", "") or "").strip()
        task_id = str(getattr(latest, "task_id", "") or "")
        reason = error or summary or "no hay detalle suficiente registrado"
        can_resume = (
            status in {"failed", "running", "lost", "timed_out"}
            or verification in {"blocked", "pending", "missing_evidence"}
        )
        lines = [
            "No voy a crear otra tarea para esto; revisé la tarea más reciente.",
            f"Task: `{task_id}`",
            f"Cierre: {status} / verificación: {verification}",
            f"Motivo: {reason[:700]}",
        ]
        if can_resume:
            lines.append(f"Para reanudarla: `/task_resume {task_id}`")
        return "\n".join(lines)

    def _task_resume_previous_response(self, session_id: str) -> str:
        latest = self._latest_relevant_task(session_id)
        if latest is None:
            return "No encontré una tarea anterior para reanudar."
        status = str(getattr(latest, "status", "unknown"))
        verification = str(getattr(latest, "verification_status", "unknown") or "unknown")
        task_id = str(getattr(latest, "task_id", "") or "")
        if status in {"succeeded", "completed", "done", "closed"}:
            # Brain-bypass refactor commit #6: a terminal status alone is not
            # proof of completion. Only claim "completada" when the verifier
            # actually marked the evidence as passed; otherwise surface the
            # missing-evidence state so the user can reopen the task.
            if verification == "passed":
                return (
                    "La tarea más reciente ya cerró como completada y verificada; no necesita reanudarse.\n"
                    f"Task: `{task_id}`"
                )
            return (
                f"La tarea más reciente cerró como `{status}` pero su verificación quedó en `{verification}`; falta evidencia para considerarla completada.\n"
                f"Task: `{task_id}`\n"
                f"Para reabrirla: `/task_resume {task_id}`"
            )
        if status == "cancelled":
            return (
                "La tarea más reciente fue cancelada; necesito que me confirmes si quieres reabrirla.\n"
                f"Task: `{task_id}`"
            )
        return (
            "Antes de crear otra tarea, te confirmo la anterior.\n"
            f"Task: `{task_id}`\n"
            f"Estado actual: {status}\n"
            f"Para reanudarla: `/task_resume {task_id}`"
        )
