from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict
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
from claw_v2.state_handler import StateHandler, _BrainShortcut
from claw_v2.terminal_handler import TerminalHandler
from claw_v2.wiki_handler import WikiHandler
from claw_v2.coordinator import CoordinatorService
from claw_v2.content import ContentEngine
from claw_v2.redaction import redact_sensitive
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
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
from claw_v2.bot_helpers import _is_secret_shaped_token  # explicit: private helper

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
    return any(token in normalized for token in _CHATGPT_OPEN_TOKENS + _CHATGPT_INTERACTIVE_TOKENS)


def _looks_like_chatgpt_interactive_request(normalized: str) -> bool:
    if not any(token in normalized for token in _CHATGPT_TARGET_TOKENS):
        return False
    return any(token in normalized for token in _CHATGPT_INTERACTIVE_TOKENS)


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
_STARTING_ACTION_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:voy\s+a|procedo\s+a|empiezo|arranco|arrancando|iniciando)\b"),
    re.compile(r"\b(?:i\s+will|i'll|i\s+am\s+going\s+to|i'm\s+going\s+to|starting|started)\b"),
)
_STARTING_ACTION_OBJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:depur|limpi|archiv|ejecut|corr|aplic|actualiz|arregl)\w*\b"),
    re.compile(r"\b(?:clean|cleanup|archive|execute|run|apply|update|fix)\w*\b"),
    re.compile(r"\b(?:approval|approvals|aprobaciones|ledger|cola|archivo|file|comando|command|task|tarea)\b"),
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
    return any(term in normalized for term in _OPERATOR_ACTION_REQUEST_TERMS)


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
PRE_HOOK_BLOCK_REPEATED_THRESHOLD = 5
PRE_HOOK_BLOCK_REPEATED_WINDOW_MINUTES = 10


def _looks_like_pre_hook_block(content: str) -> bool:
    return content.strip().startswith(_PRE_HOOK_BLOCK_PREFIX)


def _parse_pre_hook_block(content: str) -> tuple[str, str] | None:
    match = _PRE_HOOK_BLOCK_RE.match(content.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _format_approval_pending(exc: ApprovalPending) -> str:
    """Convert a Tier 3 soft-block into Telegram-ready instructions for Hector."""
    return (
        "⚠️ Acción de Tier 3 detectada. Requiere aprobación de Hector.\n\n"
        f"Tool: `{exc.tool}`\n"
        f"Resumen: {exc.summary}\n\n"
        f"Comando: `/approve {exc.approval_id} {exc.token}`"
    )


def _format_approval_pending_for_memory(exc: ApprovalPending) -> str:
    return (
        "Acción de Tier 3 pendiente de aprobación.\n"
        f"Tool: {exc.tool}\n"
        f"Resumen: {exc.summary}\n"
        f"Approval ID: {exc.approval_id}\n"
        "Token omitido en memoria."
    )


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


def _looks_like_computer_approval_grant(text: str) -> bool:
    if _looks_like_computer_approval_reject(text):
        return False
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if _looks_like_pending_tool_approval_grant(normalized):
        return True
    return any(
        phrase in normalized
        for phrase in (
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
        )
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


def _looks_like_task_diagnostic_question(text: str) -> bool:
    normalized = _normalize_command_text(text)
    return any(t in normalized for t in _TASK_TERMS) and any(t in normalized for t in _DIAGNOSTIC_TERMS)


def _looks_like_previous_task_followup(text: str) -> bool:
    normalized = _normalize_command_text(text)
    return any(token in normalized for token in _FOLLOWUP_TERMS)


def _looks_like_short_meta_question(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    if len(normalized) >= 120:
        return False
    is_question = "?" in text or normalized.startswith(("porque", "por que", "por qué", "que paso", "qué pasó"))
    if not is_question:
        return False
    return any(token in normalized for token in (
        "tarea", "task", "job", "cuaderno", "notebook",
        "completaste", "fallo", "falló",
    ))


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
            store_message=self._store_message_from_handler,
            workspace_root=getattr(config, "workspace_root", None),
            telemetry_root=getattr(config, "telemetry_root", None),
        )
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
        if not content or content == "(no result)":
            content = "Recibido. ¿Qué quieres que haga con esto?"
        elif _looks_like_pre_hook_block(content):
            content = self._maybe_augment_pre_hook_block(content)
        elif _looks_like_manual_handoff(content) and _looks_like_operator_action_request(source_text):
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
            corrected = self._unexecuted_start_response()
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
        ):
            corrected = self._pending_evidence_response()
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
        elif _looks_like_identity_drift(content):
            corrected = self._identity_binding_response()
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
    ) -> bool:
        if not _looks_like_operator_action_request(source_text):
            return False
        if not _looks_like_completion_side_effect_claim(content):
            return False
        if self._response_has_evidence_signal(response):
            return False
        if self._session_has_fresh_evidence(session_id):
            return False
        return True

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
            fallback = self._sanitize_visible_chat_response(
                session_id,
                self._internal_trace_recovery_fallback(source_text=source_text, pending_action=pending_action),
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
        self._ensure_default_autonomy(session_id)
        self._remember_inbound_context(session_id, context_metadata)
        stripped = text.strip()
        context = CommandContext(user_id=user_id, session_id=session_id, text=text, stripped=stripped)
        command_response = dispatch_commands(self._pre_state_commands, context)
        if isinstance(command_response, _BrainShortcut):
            return self._brain_text_response(
                session_id,
                command_response.text,
                memory_text=command_response.memory_text,
                runtime_channel=runtime_channel,
            )
        if command_response is not None:
            return command_response
        computer_approval_response = self._handle_pending_computer_approval_response(session_id, stripped)
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
        self._emit_dispatch_decision(
            handler="telegram_imperative",
            route="intercepted" if telegram_imperative_response is not None else "fall_through",
            reason=telegram_imperative_reason,
            session_id=session_id,
            text=stripped,
            captured=telegram_imperative_response is not None,
            matched_pattern=telegram_imperative_pattern,
        )
        if telegram_imperative_response is not None:
            telegram_imperative_response = self._quality_guard_response(
                session_id,
                stripped,
                telegram_imperative_response,
                source="telegram_imperative",
            )
            self._store_memory_turn(session_id, stripped, telegram_imperative_response, assistant_limit=3000)
            self._remember_assistant_turn_state(session_id, stripped, telegram_imperative_response)
            return telegram_imperative_response
        actionable_task_response = self._maybe_handle_actionable_task_request(
            stripped,
            session_id=session_id,
            runtime_channel=runtime_channel,
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
        if pending_tool_approval is not None:
            return pending_tool_approval
        if _looks_like_autonomy_grant(stripped):
            return self._handle_autonomy_grant_response(session_id, stripped)
        stateful_followup = self._maybe_resolve_stateful_followup(stripped, session_id=session_id)
        if isinstance(stateful_followup, _BrainShortcut):
            is_pending_execution = (
                stateful_followup.text.startswith("Continúa con esta acción pendiente:")
                or stateful_followup.text.startswith("Continúa con este siguiente paso")
            )
            result = self._brain_text_response(
                session_id,
                stateful_followup.text,
                memory_text=stateful_followup.memory_text,
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
        if stateful_followup is not None:
            self._store_memory_turn(session_id, stripped, stateful_followup, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, stateful_followup)
            return stateful_followup
        shortcut_response = self._maybe_handle_shortcut(stripped, session_id=session_id)
        self._emit_dispatch_decision(
            handler="shortcut",
            route="intercepted" if shortcut_response is not None else "fall_through",
            reason=(
                "shortcut_matched"
                if shortcut_response is not None
                else "shortcut_no_match"
            ),
            session_id=session_id,
            text=stripped,
            captured=shortcut_response is not None,
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
        nlm_response = self._nlm_handler.natural_language_response(session_id, stripped)
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
        command_response = dispatch_commands(self._post_shortcut_commands, context)
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
        return json.dumps(asdict(self.heartbeat.collect()), indent=2, sort_keys=True)

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
        for lane in ("brain", "worker", "verifier", "research", "judge"):
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
        for lane in ("brain", "worker", "research", "verifier", "judge"):
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
                f"  judge: {self.config.judge_effort}\n"
                f"\nUso: /effort <level> [lane]\n"
                f"Niveles: {', '.join(self._VALID_EFFORTS)}\n"
                f"Lanes: brain, worker, judge (omitir = todas)"
            )
        parts = context.stripped.split()
        level = parts[1].lower() if len(parts) >= 2 else ""
        if level not in self._VALID_EFFORTS:
            return f"Nivel inválido: {level}\nVálidos: {', '.join(self._VALID_EFFORTS)}"
        lane = parts[2].lower() if len(parts) >= 3 else None
        if lane and lane not in ("brain", "worker", "judge"):
            return f"Lane inválido: {lane}\nVálidos: brain, worker, judge"
        if lane:
            setattr(self.config, f"{lane}_effort", level)
        else:
            self.config.brain_effort = level
            self.config.worker_effort = level
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
                return "usage: /approve <approval_id> <token>"
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
                return "usage: /pipeline_approve <approval_id> <token>"
            approved = self.approvals.approve(parts[1], parts[2])
            if not approved:
                return "approval rejected"
            if self.pipeline is None:
                return "pipeline service unavailable"
            for run in self.pipeline.list_active():
                if run.approval_id == parts[1]:
                    result = self.pipeline.complete_pipeline(run.issue_id)
                    return json.dumps({"status": result.status, "pr_url": result.pr_url}, indent=2)
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
            )
            return json.dumps(
                {
                    "status": "approval_required",
                    "issue": issue_id,
                    "pr_url": run.pr_url,
                    "approval_id": pending.approval_id,
                    "approval_token": pending.token,
                    "confirm_with": f"/pipeline_merge_confirm {pending.approval_id} {pending.token}",
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
                results = [self.social_publisher.publish(d) for d in drafts]
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
        if content != raw_content:
            self.brain.memory.replace_latest_assistant_message(session_id, raw_content, content)
        if content == "Recibido. ¿Qué quieres que haga con esto?":
            self._browse_handler._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Bot returned fallback for message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome="failure",
                error_snippet=(raw_content or "empty_response")[:500],
                lesson="When the brain returns empty output, ask a clarifying question and inspect prompt/context assembly.",
                predicted_confidence=self.brain._last_confidence.get(session_id) or None,
            )
        else:
            self._browse_handler._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Handled message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome="success",
                lesson="The brain produced a usable reply for this conversational request.",
                predicted_confidence=self.brain._last_confidence.get(session_id) or None,
            )
        self._remember_assistant_turn_state(session_id, source_text, content)
        self._attach_brain_tool_use_ledger(
            session_id=session_id,
            response=response,
            source_text=source_text,
            runtime_channel=runtime_channel,
        )
        return content

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
          - any tool event + no active task→ create synthetic task; mark
                                              terminal with verification
                                              status derived from
                                              failures + evidence
          - approval-gated tool blocked    → recorded as
                                              brain_tooluse_ledger_skipped_sensitive

        Never marks verification_status="passed" on a brain fallback —
        the brain has no evidence pack, so the defense-in-depth
        `task_completion.validate_completion` gate would downgrade
        success-without-evidence anyway.
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
            "outputs_summarized": "",
            "tool_event_count": len(tool_events),
            "tool_failure_count": len(tool_failure_events),
            "approval_event_count": len(approval_events),
            "blockers": [],
            "sensitive_redactions_applied": sensitive_redactions > 0,
            "verification_result": "unknown",
        }
        brain_artifacts = {
            "evidence_manifest": evidence_manifest,
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
        try:
            self.task_ledger.create(
                task_id=task_id,
                session_id=session_id,
                objective=(
                    f"brain fallback tool-use turn ({len(tool_events)} "
                    f"tool calls, trace {trace_id[:8]})"
                ),
                runtime=(runtime_channel or "brain_fallback"),
                mode="brain_fallback",
                status="running",
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
            useful_result = self._brain_response_has_useful_result(response)
            if useful_result:
                evidence_manifest["completed_at"] = time.time()
                evidence_manifest["verification_result"] = "succeeded_with_warnings"
                evidence_manifest["blockers"] = [first_error[:200] or "nonfatal tool failure"]
                self.task_ledger.mark_terminal(
                    task_id,
                    status="succeeded",
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
                            "task_status": "succeeded_with_warnings",
                        },
                    )
                except Exception:
                    logger.debug("brain_tooluse_ledger_completed_with_warnings emit failed", exc_info=True)
                return
            evidence_manifest["completed_at"] = time.time()
            evidence_manifest["verification_result"] = "failed"
            evidence_manifest["blockers"] = [first_error[:200] or "tool execution failed"]
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
        # PR 0F: tools ran without failure AND we have an
        # evidence_manifest → the row closes terminally as succeeded +
        # needs_verification (no out-of-band verifier rerun required).
        # The PR 0F branch in task_completion.validate_completion
        # recognizes the manifest and accepts the close. Without a
        # manifest the existing false-success guard would still
        # downgrade to running+missing_evidence — that's why we attach
        # the manifest unconditionally above.
        evidence_manifest["completed_at"] = time.time()
        evidence_manifest["verification_result"] = "needs_verification"
        self.task_ledger.mark_terminal(
            task_id,
            status="succeeded",
            summary=f"brain tool-use turn: {len(tool_events)} tool calls (unverified)",
            verification_status="needs_verification",
            artifacts=brain_artifacts,
        )
        try:
            self.observe.emit(
                "brain_tooluse_ledger_needs_verification",
                payload={"task_id": task_id, "tools_count": len(tool_events)},
            )
            self.observe.emit(
                "brain_tooluse_ledger_completed",
                payload={
                    "task_id": task_id,
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
        str,
    ]:
        """Pull (tools_run, files_touched, commands_run, files_read,
        files_written, grep_patterns, glob_patterns, first_error) from
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
            first_error,
        )

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

    def _operator_handoff_binding_response(self) -> str:
        return (
            "No cierro esto con handoff manual. "
            "Detecte una accion operativa y la respuesta intento delegarte el ultimo paso. "
            "La ruta correcta es: ejecutar desde el runtime, crear una tarea durable, pedir aprobacion "
            "o reportar un bloqueo de capacidad/politica verificado. "
            "No marco la accion como completada sin ejecucion verificable."
        )

    def _pending_evidence_response(self) -> str:
        return (
            "No marco esto como completado todavía. "
            "La respuesta reclamó una acción hecha, pero no hay evidencia verificable "
            "en el runtime para sostenerlo. Continúo solo con una de estas salidas: "
            "tarea durable, acción ejecutada con evidencia, aprobación pendiente, "
            "bloqueo de capacidad/política o una aclaración mínima."
        )

    def _unexecuted_start_response(self) -> str:
        return (
            "No digo `arrancando` sin haber creado una tarea, ejecutado una herramienta "
            "o registrado evidencia. Esta solicitud debe pasar por un router accionable: "
            "tarea durable, acción ejecutada, aprobación, bloqueo explícito o aclaración mínima."
        )

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
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        active_object["pending_tool_approval"] = {
            "approval_id": exc.approval_id,
            "tool": exc.tool,
            "summary": exc.summary,
            "original_text": user_text,
            "created_at": time.time(),
        }
        self.brain.memory.update_session_state(
            session_id,
            pending_action=user_text,
            verification_status="awaiting_tool_approval",
            active_object=active_object,
        )

    def _clear_pending_tool_approval(self, session_id: str, approval_id: str | None = None) -> None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        pending = active_object.get("pending_tool_approval")
        if approval_id is None or not isinstance(pending, dict) or pending.get("approval_id") == approval_id:
            active_object.pop("pending_tool_approval", None)
            self.brain.memory.update_session_state(
                session_id,
                pending_action="",
                verification_status="unknown",
                active_object=active_object,
            )

    def _handle_pending_tool_approval_grant_response(self, session_id: str, text: str) -> str | None:
        state = self.brain.memory.get_session_state(session_id)
        active_object = dict(state.get("active_object") or {})
        pending = active_object.get("pending_tool_approval")
        if not isinstance(pending, dict):
            return None
        if not _looks_like_pending_tool_approval_grant(text):
            return None
        approval_id = str(pending.get("approval_id") or "")
        tool = str(pending.get("tool") or "")
        original_text = str(pending.get("original_text") or "").strip()
        if not approval_id or not tool or not original_text:
            return "Hay una aprobación pendiente, pero le falta contexto para reintentar. Reenvíame el objetivo concreto."
        try:
            status = self.approvals.status(approval_id)
        except FileNotFoundError:
            self._clear_pending_tool_approval(session_id, approval_id)
            return f"La aprobación pendiente `{approval_id}` ya no existe. Reenvíame el objetivo concreto."
        if status == "pending":
            if not self.approvals.approve_internal(approval_id):
                return "No pude registrar la aprobación pendiente. Usa `/approvals` para revisar el estado."
        elif status != "approved":
            self._clear_pending_tool_approval(session_id, approval_id)
            return f"La aprobación `{approval_id}` está en estado `{status}`. Reenvíame el objetivo si quieres intentarlo de nuevo."
        self._clear_pending_tool_approval(session_id, approval_id)
        with approved_tool_invocation(
            tool=tool,
            approval_id=approval_id,
            reason="telegram_owner_followup",
        ):
            result = self._brain_text_response(session_id, original_text, memory_text=original_text)
        return f"Aprobación registrada. Reintenté la acción original.\n\n{result}"

    def _handle_pending_computer_approval_response(self, session_id: str, text: str) -> str | None:
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
        same_hook_count = 0
        for event in recent:
            if event.get("event_type") != "llm_pre_hook_blocked":
                continue
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("blocked_by") != hook_name:
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
            return _format_approval_pending(exc)


    def _tokens_info_response(self, session_id: str) -> str:
        message_count = self.brain.memory.count_messages(session_id)

        # Estimación aproximada: ~500 tokens por mensaje (muy conservador)
        estimated_tokens = message_count * 500
        context_window = self.config.brain_context_window if self.config else 1_000_000

        estimated_pct = (estimated_tokens / context_window) * 100

        if estimated_pct > 80:
            status = "critical"
            status_emoji = "🔴"
            recommendation = "Compacta ahora para evitar pérdida de contexto"
        elif estimated_pct > 60:
            status = "warning"
            status_emoji = "🟡"
            recommendation = "Considera compactar pronto"
        else:
            status = "healthy"
            status_emoji = "🟢"
            recommendation = "Espacio saludable"

        max_output = self.config.brain_max_output if self.config else 128_000
        return json.dumps({
            "session_id": session_id,
            "model": "Claude Opus 4.7 / Sonnet 4.6",
            "context_window": context_window,
            "max_output": max_output,
            "messages_count": message_count,
            "estimated_tokens": estimated_tokens,
            "estimated_percentage": round(estimated_pct, 1),
            "status": status,
            "status_display": f"{status_emoji} {status.title()}",
            "recommendation": recommendation,
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
            if any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._browse_handler.browse_response(extracted_url, session_id=session_id)

        if _looks_like_tweet_followup_request(normalized):
            recent_tweet_url = self._browse_handler.recent_tweet_url(session_id)
            if recent_tweet_url is not None:
                return _BrainShortcut(f"{text}\n\n{recent_tweet_url}")

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

    def _pending_tasks_query_matches(self, text: str) -> bool:
        normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
        compact = re.sub(r"[^a-z0-9]+", "", normalized)
        return (
            ("tareas" in normalized and "pendient" in normalized)
            or compact in {"pendientes", "tareaspendientes", "pendietes", "tareaspendietes"}
        )

    def _handle_pending_tasks_query(
        self,
        text: str,
        *,
        session_id: str,
        runtime_channel: str | None = None,
    ) -> str:
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
        pending_action = str(state.get("pending_action") or "").strip()
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
            task_id = str(last_result.get("task_id") or "sin_task_id")
            return (
                f"Sí. Última limpieza registrada: `{status}`.\n"
                f"Archivadas: {archived}; conservadas: {kept}; fallidas: {failed}.\n"
                f"Task: `{task_id}`."
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
    ) -> tuple[str | None, str, str | None]:
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
        response = self._handle_telegram_imperative(intent, text, session_id=session_id)
        return response, f"telegram_imperative:{intent.intent}", intent.matched_pattern

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
    ) -> str:
        state = self.brain.memory.get_session_state(session_id)
        mission = self._active_mission_context(state)
        target = self._resolve_telegram_target(intent, mission)
        artifact = self._resolve_telegram_artifact(intent, state, mission)
        artifact_text = self._resolve_telegram_artifact_text(intent, state, mission)
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
            self._emit_safe("telegram_imperative_clarification", {"session_id": session_id, "intent": intent.intent})
            return (
                "Necesito una aclaración mínima: ¿en qué app o target ejecuto esto?\n"
                f"Intent detectado: `{intent.intent}`."
            )
        if intent.artifact_hint and not artifact:
            self._emit_safe(
                "active_mission_resolution_failed",
                {"session_id": session_id, "intent": intent.intent, "reason": "missing_artifact"},
            )
            self._emit_safe("telegram_imperative_clarification", {"session_id": session_id, "intent": intent.intent})
            return (
                "Necesito una aclaración mínima: ¿qué prompt/instrucciones uso?\n"
                f"Intent detectado: `{intent.intent}`; target: `{target or 'desconocido'}`."
            )
        if intent.intent in {"ui.paste_text", "ui.set_instructions"} and not artifact_text:
            self._emit_safe(
                "active_mission_resolution_failed",
                {"session_id": session_id, "intent": intent.intent, "reason": "missing_artifact_text"},
            )
            self._emit_safe("telegram_imperative_clarification", {"session_id": session_id, "intent": intent.intent})
            return (
                "Necesito una aclaración mínima: tengo la referencia del prompt/instrucciones, "
                "pero no encontré el texto exacto para pegar.\n"
                f"Intent detectado: `{intent.intent}`; target: `{target or 'desconocido'}`."
            )
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
                return (
                    "Resolví la continuación de la misión activa.\n"
                    f"Intent: `{intent.intent}`\n"
                    f"Target: `{target}`\n"
                    f"Task: `{task_id}`\n"
                    "Estado: `partial_success` — falta una acción concreta o capacidad de ejecución."
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
                fallback = "\nFallback seguro: puedo preparar/copiar el texto, pero eso no equivale a pegarlo en la app."
            return (
                f"Resultado: `blocked_by_capability`\n"
                f"Intent: `{intent.intent}`\n"
                f"Target: `{target or 'desconocido'}`\n"
                f"Artifact: `{artifact or 'desconocido'}`\n"
                f"Capability faltante: `{missing_capability}`\n"
                f"Task: `{task_id}`"
                f"{fallback}"
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
        return (
            f"Intent routed: `{intent.intent}`\n"
            f"Target: `{target or 'desconocido'}`\n"
            f"Artifact: `{artifact or 'desconocido'}`\n"
            f"Estado: `partial_success`\n"
            f"Task: `{task_id}`"
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
        self.brain.memory.update_session_state(
            session_id,
            mode="ops",
            current_goal=last_user_goal[:280],
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
            return (
                "Resultado: `blocked_by_capability`\n"
                "Intent: `approvals.cleanup_stale_duplicates`\n"
                "Capability faltante: `approval_manager`\n"
                f"Task: `{task_id}`"
            )
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
            return (
                "Resultado: `failed`\n"
                "Intent: `approvals.cleanup_stale_duplicates`\n"
                f"Error: {str(exc)[:240]}\n"
                f"Task: `{task_id}`"
            )
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
            f"Intent: `{intent.intent}`",
            f"Estado: `{result_status}`",
            f"Archivadas: {len(archived)}",
            f"Conservadas: {kept_count}",
            f"Fallidas: {len(failed)}",
            f"Task: `{task_id}`",
        ]
        if archived:
            response_lines.append("IDs archivadas: " + ", ".join(approval_id for approval_id, _ in archived[:8]))
        if failed:
            response_lines.append("Fallos: " + ", ".join(f"{approval_id or 'unknown'}:{reason}" for approval_id, reason in failed[:5]))
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
        lines = [
            f"Intent: `{intent.intent}`",
            f"Target: `{target or 'desconocido'}`",
            f"Estado: `{result_status}`",
            f"Task: `{task_id}`",
        ]
        if approval_id:
            lines.append(f"approval_id: `{approval_id}`")
        if handler_result:
            lines.append("")
            lines.append(str(handler_result))
        return "\n".join(lines)

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
    ) -> str | None:
        if (runtime_channel or "").strip().lower() != "telegram":
            return None
        if not text or text.startswith("/"):
            return None
        state = self.brain.memory.get_session_state(session_id)
        objective, source = self._resolve_actionable_task_objective(text, state=state)
        if objective is None:
            if self._looks_like_actionable_followup(text):
                self._emit_safe(
                    "clarification_requested_after_context_lookup",
                    {"session_id": session_id, "reason": "actionable_task_context_not_found"},
                )
                return "¿Qué acción concreta quieres que ejecute?"
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
        if current_goal and not self._looks_like_actionable_followup(current_goal):
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
                details.append(f"{check.binary}: bloqueado por policy")
            else:
                details.append(f"{check.binary}: {check.status}")
        if not details:
            details = ["capacidad requerida no disponible"]
        return (
            "Creé la tarea, pero quedó bloqueada en preflight antes de ejecutar.\n"
            + "\n".join(f"- {item}" for item in details[:5])
            + "\nSiguiente paso: habilitar el comando específico o darme una ruta segura alternativa."
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
