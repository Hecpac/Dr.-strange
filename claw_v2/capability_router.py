"""Capability Router — decide automáticamente la ruta de ejecución para
intenciones en lenguaje natural.

Reglas clave (no negociables):
- Sólo procesa lenguaje natural; comandos slash (`/...`) NO se interceptan.
- Routing por `task_kind`, NO orden global único.
- Acciones críticas (publish/merge/deploy) → `approval_required` antes que
  cualquier otra ruta.
- Si hay una sola ruta segura, `ask_user=False`.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Literal


RouteKind = Literal[
    "local",
    "runtime",
    "skill",
    "cdp",
    "bridge",
    "blocked",
    "approval_required",
    "chat",
    "runtime_handoff",
]


# Capabilities that genuinely need to execute bash/python/browser/CDP.
# When the current environment cannot run them, we MUST handoff.
_EXECUTION_REQUIRING_TASKS: frozenset[str] = frozenset(
    {"ai_news_brief", "x_trends"}
)


CRITICAL_TASK_KINDS: frozenset[str] = frozenset(
    {"social_publish", "pipeline_merge", "deploy"}
)


@dataclass(slots=True)
class AutonomyIntent:
    task_kind: str
    required_capabilities: list[str] = field(default_factory=list)
    raw_text: str = ""


@dataclass(slots=True)
class CapabilityRoute:
    route: RouteKind
    reason: str
    task_kind: str
    required_capabilities: list[str] = field(default_factory=list)
    available_capabilities: list[str] = field(default_factory=list)
    missing_capabilities: list[str] = field(default_factory=list)
    skill: str | None = None
    agent: str | None = None
    requires_approval: bool = False
    ask_user: bool = False
    next_action: str = ""


# --- Intent classification ---


_AI_NEWS_PATTERNS = [
    r"\bnoticias?\s+(?:de\s+)?(?:ai|ia|inteligencia\s+artificial)\s+(?:de\s+)?(?:hoy|recientes?|del\s+d[ií]a)?\b",
    r"\b(?:ai|ia)\s+news\b",
    r"\bdame\s+(?:el\s+)?ai\s+brief\b",
    r"\btitulares?\s+(?:de\s+)?(?:ai|ia)\b",
    r"\bqu[eé]\s+pas[oó]\s+en\s+(?:ai|ia)\b",
    r"\btrend\s+(?:de\s+)?(?:ai|ia)\b",
]

_X_TRENDS_PATTERNS = [
    r"\b(?:trends?|tendencias)\s+(?:en\s+)?x\b",
    r"\btweets?\s+(?:de\s+)?(?:hoy|trending)\b",
    r"\btimeline\s+de\s+x\b",
    r"\bque\s+est[aá]\s+pasando\s+en\s+x\b",
    r"\bx\s+trends?\b",
]

_NOTEBOOKLM_PATTERNS = [
    r"\b(?:cuaderno|notebook|notebooklm|nlm)\b",
    r"\b(?:revisa|abre|usa)\s+(?:el\s+)?(?:[uú]ltimo\s+)?cuaderno\b",
]

_SOCIAL_PUBLISH_PATTERNS = [
    r"\bpublica\s+(?:esto|este|en\s+)\b",
    r"\bpostea(?:r|lo|ar)?\b",
    r"\btwittea(?:r|lo|ar)?\b",
    r"\btweet\s+esto\b",
    r"\bsubir?\s+a\s+(?:linkedin|twitter|x)\b",
]

_PIPELINE_MERGE_PATTERNS = [
    r"\bmerge\s+(?:el\s+|este\s+)?(?:pr|issue|branch)\b",
    r"\bhaz\s+merge\b",
    r"\bcierra\s+(?:el\s+)?(?:pr|issue)\b",
]

_DEPLOY_PATTERNS = [
    r"\bdesplie?ga\b",
    r"\bdeploy(?:ear|alo)?\b",
    r"\bsube\s+(?:a|al)\s+prod(?:ucci[oó]n)?\b",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def classify_autonomy_intent(text: str) -> AutonomyIntent:
    """Clasifica un texto en lenguaje natural a un AutonomyIntent.

    Retorna AutonomyIntent con task_kind="unknown" si no hay match claro.
    """
    raw = text.strip()
    if _matches_any(raw, _SOCIAL_PUBLISH_PATTERNS):
        return AutonomyIntent(
            task_kind="social_publish",
            required_capabilities=["social_publish"],
            raw_text=raw,
        )
    if _matches_any(raw, _PIPELINE_MERGE_PATTERNS):
        return AutonomyIntent(
            task_kind="pipeline_merge",
            required_capabilities=["pipeline_merge"],
            raw_text=raw,
        )
    if _matches_any(raw, _DEPLOY_PATTERNS):
        return AutonomyIntent(
            task_kind="deploy",
            required_capabilities=["deploy"],
            raw_text=raw,
        )
    if _matches_any(raw, _AI_NEWS_PATTERNS):
        return AutonomyIntent(
            task_kind="ai_news_brief",
            required_capabilities=["web_search"],
            raw_text=raw,
        )
    if _matches_any(raw, _X_TRENDS_PATTERNS):
        return AutonomyIntent(
            task_kind="x_trends",
            required_capabilities=["x_authenticated", "chrome_cdp"],
            raw_text=raw,
        )
    if _matches_any(raw, _NOTEBOOKLM_PATTERNS):
        return AutonomyIntent(
            task_kind="notebooklm_review",
            required_capabilities=["notebooklm"],
            raw_text=raw,
        )
    return AutonomyIntent(task_kind="unknown", required_capabilities=[], raw_text=raw)


# --- Route decisions per task_kind ---


def _route_critical(intent: AutonomyIntent) -> CapabilityRoute:
    return CapabilityRoute(
        route="approval_required",
        reason="critical_action_requires_approval",
        task_kind=intent.task_kind,
        required_capabilities=list(intent.required_capabilities),
        requires_approval=True,
        ask_user=False,
        next_action=f"Acción crítica `{intent.task_kind}`: requiere aprobación explícita.",
    )


def _route_ai_news(
    intent: AutonomyIntent,
    *,
    skill_available: bool,
    runtime_alive: bool,
    web_available: bool,
) -> CapabilityRoute:
    available: list[str] = []
    if skill_available:
        available.append("skill")
    if runtime_alive:
        available.append("runtime")
    if web_available:
        available.append("web_search")
    if skill_available:
        return CapabilityRoute(
            route="skill",
            reason="ai_news_skill_available",
            task_kind="ai_news_brief",
            required_capabilities=list(intent.required_capabilities),
            available_capabilities=available,
            skill="ai-news-daily",
            agent="alma",
            ask_user=False,
            next_action="Despachando a skill ai-news-daily.",
        )
    if runtime_alive:
        return CapabilityRoute(
            route="runtime",
            reason="ai_news_via_runtime",
            task_kind="ai_news_brief",
            required_capabilities=list(intent.required_capabilities),
            available_capabilities=available,
            ask_user=False,
            next_action="Ejecutando AI news vía runtime.",
        )
    if web_available:
        return CapabilityRoute(
            route="local",
            reason="ai_news_via_local_web",
            task_kind="ai_news_brief",
            required_capabilities=list(intent.required_capabilities),
            available_capabilities=available,
            ask_user=False,
            next_action="Ejecutando AI news con web tools locales.",
        )
    return CapabilityRoute(
        route="blocked",
        reason="ai_news_no_available_route",
        task_kind="ai_news_brief",
        required_capabilities=list(intent.required_capabilities),
        available_capabilities=available,
        missing_capabilities=["skill", "runtime", "web_search"],
        ask_user=False,
        next_action=(
            "No puedo obtener noticias AI ahora: no hay skill registrada, runtime "
            "está caído y no tengo acceso web. Configura runtime o skill."
        ),
    )


def _route_x_trends(
    intent: AutonomyIntent,
    *,
    chrome_cdp: bool,
    runtime_alive: bool,
) -> CapabilityRoute:
    available: list[str] = []
    if chrome_cdp:
        available.append("chrome_cdp")
    if runtime_alive:
        available.append("runtime")
    if chrome_cdp:
        return CapabilityRoute(
            route="cdp",
            reason="x_trends_via_cdp",
            task_kind="x_trends",
            required_capabilities=list(intent.required_capabilities),
            available_capabilities=available,
            ask_user=False,
            next_action="Usando Chrome CDP para X trends.",
        )
    if runtime_alive:
        return CapabilityRoute(
            route="runtime",
            reason="x_trends_via_runtime",
            task_kind="x_trends",
            required_capabilities=list(intent.required_capabilities),
            available_capabilities=available,
            ask_user=False,
            next_action="Ejecutando X trends vía runtime.",
        )
    return CapabilityRoute(
        route="blocked",
        reason="x_trends_no_available_route",
        task_kind="x_trends",
        required_capabilities=list(intent.required_capabilities),
        available_capabilities=available,
        missing_capabilities=["chrome_cdp", "runtime"],
        ask_user=False,
        next_action=(
            "No puedo leer X trends ahora: Chrome CDP no está activo y runtime "
            "está caído."
        ),
    )


def _route_notebooklm(intent: AutonomyIntent) -> CapabilityRoute:
    return CapabilityRoute(
        route="local",
        reason="notebooklm_handled_by_nlm_handler",
        task_kind="notebooklm_review",
        required_capabilities=list(intent.required_capabilities),
        available_capabilities=["notebooklm"],
        ask_user=False,
        next_action="Delegando a NlmHandler con resolver de contexto.",
    )


def _route_runtime_handoff(intent: AutonomyIntent, reason: str) -> CapabilityRoute:
    return CapabilityRoute(
        route="runtime_handoff",
        reason=reason,
        task_kind=intent.task_kind,
        required_capabilities=list(intent.required_capabilities),
        ask_user=False,
        next_action="Despachando a Claw producción.",
    )


def route_request(
    intent: AutonomyIntent,
    *,
    skill_available: Callable[[str], bool] | None = None,
    runtime_alive: bool = False,
    chrome_cdp: bool = False,
    web_available: bool = True,
    current_environment: str | None = None,
) -> CapabilityRoute:
    """Decide la ruta para una intención, sin orden global único.

    Cada `task_kind` tiene su propia política de rutas. El caller wire los
    estados de capabilities desde el bot.

    Si ``current_environment="claude_code_sandbox"`` y la tarea necesita
    ejecución real (browser/bash/python), forzamos ``runtime_handoff``: no
    se ejecuta localmente porque el sandbox bloquea las tools.
    """
    if intent.task_kind in CRITICAL_TASK_KINDS:
        return _route_critical(intent)
    if (
        current_environment == "claude_code_sandbox"
        and intent.task_kind in _EXECUTION_REQUIRING_TASKS
    ):
        return _route_runtime_handoff(
            intent, reason="claude_code_sandbox_cannot_execute"
        )
    if intent.task_kind == "ai_news_brief":
        skill_ok = bool(skill_available and skill_available("ai-news-daily"))
        return _route_ai_news(
            intent,
            skill_available=skill_ok,
            runtime_alive=runtime_alive,
            web_available=web_available,
        )
    if intent.task_kind == "x_trends":
        return _route_x_trends(
            intent,
            chrome_cdp=chrome_cdp,
            runtime_alive=runtime_alive,
        )
    if intent.task_kind.startswith("notebooklm"):
        return _route_notebooklm(intent)
    return CapabilityRoute(
        route="chat",
        reason="unknown_intent_falls_through_to_chat",
        task_kind=intent.task_kind,
        required_capabilities=list(intent.required_capabilities),
        ask_user=False,
        next_action="",
    )


# --- Runtime alive (cached, ≤250ms timeout) ---


@dataclass(slots=True)
class _RuntimeAliveCache:
    last_check_ts: float = 0.0
    last_result: bool = False


_RUNTIME_CACHE_TTL_SECONDS = 30.0
_RUNTIME_PROBE_TIMEOUT_SECONDS = 0.25


class RuntimeAliveProbe:
    """Probes runtime liveness with caching.

    Strategy: try fast checks (port listener) and cache result for ~30s.
    Probe timeout ≤250ms total to never block handle_text.
    """

    def __init__(
        self,
        *,
        port: int = 8765,
        probe_fn: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._port = port
        self._probe_fn = probe_fn or self._default_probe
        self._clock = clock
        self._cache = _RuntimeAliveCache()

    def is_alive(self) -> bool:
        now = self._clock()
        if now - self._cache.last_check_ts < _RUNTIME_CACHE_TTL_SECONDS:
            return self._cache.last_result
        try:
            result = self._probe_fn()
        except Exception:
            result = False
        self._cache.last_result = bool(result)
        self._cache.last_check_ts = now
        return self._cache.last_result

    def _default_probe(self) -> bool:
        # Fast TCP connect with 250ms timeout
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(_RUNTIME_PROBE_TIMEOUT_SECONDS)
        try:
            return sock.connect_ex(("127.0.0.1", self._port)) == 0
        finally:
            sock.close()
