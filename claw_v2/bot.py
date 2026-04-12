from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

from claw_v2.agents import AgentDefinition, AutoResearchAgentService
from claw_v2.approval import ApprovalManager
from claw_v2.brain import BrainService
from claw_v2.coordinator import CoordinatorResult, CoordinatorService, WorkerTask
from claw_v2.content import ContentEngine
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
from claw_v2.pipeline import PipelineService
from claw_v2.social import SocialPublisher


@dataclass(slots=True)
class _BrainShortcut:
    text: str
    memory_text: str | None = None

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
    r"^\s*(?:por favor\s+)?(?:(?:cr[eé]a(?:me)?)|(?:genera(?:me)?)|(?:haz(?:me)?)|quiero|necesito)\s+"
    r"(?:un\s+)?(?:cuaderno|notebook)(?:\s+(?:en\s+notebooklm))?"
    r"(?:\s+(?:sobre|de(?:l)?))?\s+(.+?)\s*$",
    re.IGNORECASE,
)
_NLM_ARTIFACT_KINDS = {
    "podcast": "podcast",
    "infografia": "infographic",
    "infographic": "infographic",
    "video": "video",
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
)
_SCHEME_URL_RE = re.compile(r"(?P<url>https?://[^\s<>()]+)", re.IGNORECASE)
_HOST_URL_RE = re.compile(
    r"(?P<url>(?<!@)(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})(?::\d+)?(?:[/?#][^\s<>()]*)?)",
    re.IGNORECASE,
)
_DEFAULT_COMPUTER_MODEL = "gpt-5.4"
_COMPUTER_SYSTEM_PROMPT = (
    "You control the user's Mac via the computer-use tool. "
    "Be careful, explicit, and incremental. "
    "Prefer reading the current screen before acting. "
    "When searching for a visible UI element, move/scroll as needed, then click only when confident. "
    "Stop and explain what you see when the task is complete."
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
    "continue",
    "continua",
    "continuar",
    "sigue",
    "seguir",
    "dale",
    "hazlo",
    "asi",
)
_OPTION_ORDINALS = {
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
    "push": (r"\bgit\s+push\b",),
    "deploy": (r"\bdeploy\b", r"\bdespliega\b", r"\bproduction\b", r"\bprod\b"),
    "publish": (r"\bpublica\b", r"\bpublish\b", r"\btweet\b", r"\bpost\b"),
    "destructive": (r"\bdelete\b", r"\bborra\b", r"\belimina\b", r"\brm\s+-", r"\bdrop\s+table\b", r"\btruncate\b"),
}
_AUTONOMY_TASK_ACTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "inspect": (r"\brevisa\b", r"\binspect\b", r"\banaliza\b", r"\bdebug\b", r"\bcheck\b"),
    "edit": (r"\bcorrige\b", r"\barregla\b", r"\bfix\b", r"\bimplementa\b", r"\bedit\b", r"\bpatch\b"),
    "test": (r"\btest\b", r"\bpytest\b", r"\bverifica\b", r"\bverify\b"),
    "commit": (r"\bcommit\b", r"\bcommitea\b"),
    "research": (r"\binvestiga\b", r"\bresearch\b", r"\bfind\b", r"\bgather\b"),
    "summarize": (r"\bresume\b", r"\bsummariza\b", r"\bsummary\b", r"\bsintetiza\b"),
}
_AUTONOMY_POLICY_MATRIX: dict[str, dict[str, Any]] = {
    "manual": {
        "automatic_coordinator_modes": [],
        "blocked_actions": ("push", "deploy", "publish", "destructive"),
        "approval_required_actions": ("commit",),
        "allowed_task_actions": (),
        "notes": [
            "No coordinated execution runs automatically in manual mode.",
            "Use this mode when you want explicit confirmation before non-trivial work.",
        ],
    },
    "assisted": {
        "automatic_coordinator_modes": [],
        "blocked_actions": ("push", "deploy", "publish", "destructive"),
        "approval_required_actions": ("commit",),
        "allowed_task_actions": ("inspect", "edit", "test", "research", "summarize"),
        "notes": [
            "Coordinated runs require explicit `/task_run` in assisted mode.",
            "Commit requires confirmation. Publish, deploy, push, and destructive actions remain blocked.",
        ],
    },
    "autonomous": {
        "automatic_coordinator_modes": ["coding", "research"],
        "blocked_actions": ("push", "deploy", "publish", "destructive"),
        "approval_required_actions": ("commit",),
        "allowed_task_actions": ("inspect", "edit", "test", "research", "summarize"),
        "notes": [
            "Autonomous coordinator runs are limited to coding and research.",
            "Operational/browser/authenticated flows stay outside the autonomous coordinator path.",
            "Commit requires confirmation. Publish, deploy, push, and destructive actions remain blocked.",
        ],
    },
}


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
    ) -> None:
        self.brain = brain
        self.auto_research = auto_research
        self.heartbeat = heartbeat
        self.approvals = approvals
        self.pull_requests = pull_requests
        self.allowed_user_id = allowed_user_id
        self.pipeline = pipeline
        self.content_engine = content_engine
        self.social_publisher = social_publisher
        self.config = config
        self.coordinator = coordinator
        self.browser = browser
        self.terminal_bridge = terminal_bridge
        self.computer = computer
        self.browser_use = browser_use
        self.computer_gate = computer_gate
        self.computer_client_factory = computer_client_factory
        self.computer_model = computer_model
        self.computer_system_prompt = computer_system_prompt or _COMPUTER_SYSTEM_PROMPT
        self.observe = observe
        self.learning: Any | None = None
        self.wiki: Any | None = None
        self._state_lock = threading.Lock()
        self._computer_sessions: dict[str, Any] = {}
        self._computer_client: Any | None = None
        self.notebooklm: Any | None = None
        self._active_notebooks: dict[str, dict[str, str]] = {}
        self.managed_chrome: Any | None = None
        self._recent_browse_urls: dict[str, str] = {}

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        stripped = text.strip()
        if stripped == "/help":
            return self._help_response()
        if stripped.startswith("/help "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return self._help_response()
            return self._help_response(parts[1])
        if stripped == "/status":
            return json.dumps(asdict(self.heartbeat.collect()), indent=2, sort_keys=True)
        if stripped == "/config":
            if self.config is None:
                return "config not available"
            c = self.config
            lanes = {}
            for lane in ("brain", "worker", "verifier", "research", "judge"):
                lanes[lane] = {
                    "provider": c.provider_for_lane(lane),
                    "model": c.model_for_lane(lane),
                    "effort": c.effort_for_lane(lane),
                    "context_window": c.context_window_for_lane(lane),
                    "max_output": c.max_output_for_lane(lane),
                }
            return json.dumps({"lanes": lanes, "max_budget_usd": c.max_budget_usd, "daily_token_budget": c.daily_token_budget}, indent=2)
        if stripped == "/tokens":
            return self._tokens_info_response(session_id)
        if stripped == "/task_run":
            return "usage: /task_run <objective>"
        if stripped.startswith("/task_run "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /task_run <objective>"
            return self._coordinated_task_response(session_id, parts[1], forced=True)
        if stripped == "/autonomy":
            return json.dumps(self.brain.memory.get_session_state(session_id), indent=2, sort_keys=True)
        if stripped == "/autonomy_policy":
            return json.dumps(_autonomy_policy_payload(self.brain.memory.get_session_state(session_id)), indent=2, sort_keys=True)
        if stripped.startswith("/autonomy "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /autonomy <manual|assisted|autonomous>"
            try:
                return self._set_autonomy_mode_response(session_id, parts[1])
            except ValueError as exc:
                return str(exc)
        if stripped == "/task_loop":
            return json.dumps(self.brain.memory.get_session_state(session_id), indent=2, sort_keys=True)
        if stripped == "/task_queue":
            state = self.brain.memory.get_session_state(session_id)
            return json.dumps(state.get("task_queue") or [], indent=2, sort_keys=True)
        if stripped.startswith("/task_queue "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /task_queue [mode]"
            state = self.brain.memory.get_session_state(session_id)
            return json.dumps(_filter_task_queue_by_mode(state.get("task_queue") or [], parts[1]), indent=2, sort_keys=True)
        if stripped == "/task_done":
            return "usage: /task_done <task_id>"
        if stripped.startswith("/task_done "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /task_done <task_id>"
            return self._task_queue_transition_response(session_id, parts[1], to_status="done")
        if stripped == "/task_defer":
            return "usage: /task_defer <task_id>"
        if stripped.startswith("/task_defer "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /task_defer <task_id>"
            return self._task_queue_transition_response(session_id, parts[1], to_status="deferred")
        if stripped == "/task_pending":
            state = self.brain.memory.get_session_state(session_id)
            return json.dumps(state.get("pending_approvals") or [], indent=2, sort_keys=True)
        if stripped == "/session_state":
            return json.dumps(self.brain.memory.get_session_state(session_id), indent=2, sort_keys=True)
        if stripped.startswith("/browse "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /browse <url>"
            return self._browse_response(parts[1], session_id=session_id)
        if stripped == "/terminal_list":
            return self._terminal_list_response()
        if stripped == "/terminal_open":
            return "usage: /terminal_open <claude|codex> [cwd]"
        if stripped.startswith("/terminal_open "):
            parts = stripped.split(maxsplit=2)
            if len(parts) not in {2, 3}:
                return "usage: /terminal_open <claude|codex> [cwd]"
            cwd = parts[2] if len(parts) == 3 else None
            return self._terminal_open_response(parts[1], cwd=cwd)
        if stripped == "/terminal_status":
            return "usage: /terminal_status <session_id>"
        if stripped.startswith("/terminal_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /terminal_status <session_id>"
            return self._terminal_status_response(parts[1])
        if stripped == "/terminal_read":
            return "usage: /terminal_read <session_id> [offset]"
        if stripped.startswith("/terminal_read "):
            parts = stripped.split(maxsplit=2)
            if len(parts) == 2:
                offset = 0
            elif len(parts) == 3:
                try:
                    offset = _parse_non_negative_int(parts[2], field_name="offset")
                except ValueError as exc:
                    return str(exc)
            else:
                return "usage: /terminal_read <session_id> [offset]"
            return self._terminal_read_response(parts[1], offset=offset)
        if stripped == "/terminal_send":
            return "usage: /terminal_send <session_id> <text>"
        if stripped.startswith("/terminal_send "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /terminal_send <session_id> <text>"
            return self._terminal_send_response(parts[1], parts[2])
        if stripped == "/terminal_close":
            return "usage: /terminal_close <session_id>"
        if stripped.startswith("/terminal_close "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /terminal_close <session_id>"
            return self._terminal_close_response(parts[1])
        if stripped == "/chrome_pages":
            return self._chrome_pages_response()
        if stripped == "/chrome_browse":
            return "usage: /chrome_browse <url>"
        if stripped.startswith("/chrome_browse "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /chrome_browse <url>"
            return self._chrome_browse_response(parts[1], session_id=session_id)
        if stripped.startswith("/chrome_shot"):
            return self._chrome_shot_response(stripped)
        if stripped == "/chrome_login":
            return self._chrome_login_response()
        if stripped == "/chrome_headless":
            return self._chrome_headless_response()
        if stripped == "/screen":
            return self._screen_response()
        if stripped == "/computer":
            return "usage: /computer <instruction>"
        if stripped.startswith("/computer_abort"):
            return self._computer_abort_response(session_id)
        if stripped.startswith("/computer "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /computer <instruction>"
            return self._computer_response(parts[1], session_id)
        if stripped.startswith("/action_approve "):
            parts = stripped.split()
            if len(parts) != 3:
                return "usage: /action_approve <approval_id> <token>"
            return self._action_approve_response(parts[1], parts[2])
        if stripped.startswith("/action_abort "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /action_abort <approval_id>"
            return self._action_abort_response(parts[1])
        if stripped == "/buddy hatch":
            return self._buddy_hatch_response(user_id)
        if stripped == "/buddy stats":
            return self._buddy_stats_response(user_id)
        if stripped.startswith("/buddy rename "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /buddy rename <name>"
            return self._buddy_rename_response(user_id, parts[2])
        if stripped == "/buddy" or stripped == "/buddy card":
            return self._buddy_card_response(user_id)
        if stripped == "/wiki":
            return self._wiki_stats_response()
        if stripped == "/wiki lint":
            return self._wiki_lint_response()
        if stripped.startswith("/wiki ingest "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /wiki ingest <title>"
            return self._wiki_ingest_response(parts[2], session_id)
        if stripped.startswith("/wiki query "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /wiki query <question>"
            return self._wiki_query_response(parts[2])
        self._remember_user_turn_state(session_id, stripped)
        stateful_followup = self._maybe_resolve_stateful_followup(stripped, session_id=session_id)
        if isinstance(stateful_followup, _BrainShortcut):
            return self._brain_text_response(
                session_id,
                stateful_followup.text,
                memory_text=stateful_followup.memory_text,
            )
        if stateful_followup is not None:
            self.brain.memory.store_message(session_id, "user", stripped)
            self.brain.memory.store_message(session_id, "assistant", stateful_followup[:2000])
            self._remember_assistant_turn_state(session_id, stripped, stateful_followup)
            return stateful_followup
        shortcut_response = self._maybe_handle_shortcut(stripped, session_id=session_id)
        if isinstance(shortcut_response, _BrainShortcut):
            return self._brain_text_response(
                session_id,
                shortcut_response.text,
                memory_text=shortcut_response.memory_text,
            )
        if shortcut_response is not None:
            # Store the exchange so the brain has context on subsequent messages.
            self.brain.memory.store_message(session_id, "user", stripped)
            self.brain.memory.store_message(session_id, "assistant", shortcut_response[:2000])
            self._remember_assistant_turn_state(session_id, stripped, shortcut_response)
            return shortcut_response
        coordinated_response = self._maybe_run_coordinated_task(session_id, stripped)
        if coordinated_response is not None:
            self.brain.memory.store_message(session_id, "user", stripped)
            self.brain.memory.store_message(session_id, "assistant", coordinated_response[:4000])
            self._remember_assistant_turn_state(session_id, stripped, coordinated_response)
            return coordinated_response
        if stripped == "/agents":
            return json.dumps(self._list_agents_payload(), indent=2, sort_keys=True)
        if stripped.startswith("/agent_create "):
            parts = stripped.split(maxsplit=3)
            if len(parts) != 4:
                return "usage: /agent_create <agent_name> <researcher|operator|deployer> <instruction>"
            return self._create_agent_response(parts[1], parts[2], parts[3])
        if stripped.startswith("/agent_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_status <agent_name>"
            return self._agent_state_response(parts[1])
        if stripped.startswith("/agent_pause "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_pause <agent_name>"
            return self._pause_agent_response(parts[1])
        if stripped.startswith("/agent_resume "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /agent_resume <agent_name>"
            return self._resume_agent_response(parts[1])
        if stripped.startswith("/agent_history "):
            parts = stripped.split(maxsplit=2)
            if len(parts) == 2:
                limit = 10
            elif len(parts) == 3:
                try:
                    limit = _parse_positive_int(parts[2], field_name="limit")
                except ValueError as exc:
                    return str(exc)
            else:
                return "usage: /agent_history <agent_name> [limit]"
            return self._agent_history_response(parts[1], limit=limit)
        if stripped.startswith("/agent_promote "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_promote <agent_name> <on|off>"
            try:
                enabled = _parse_toggle(parts[2])
            except ValueError as exc:
                return str(exc)
            return self._update_agent_response(parts[1], promote_on_improvement=enabled)
        if stripped.startswith("/agent_branch "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_branch <agent_name> <on|off>"
            try:
                enabled = _parse_toggle(parts[2])
            except ValueError as exc:
                return str(exc)
            return self._update_agent_response(parts[1], branch_on_promotion=enabled)
        if stripped.startswith("/agent_branch_name "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_branch_name <agent_name> <name|-|clear>"
            branch_name = "" if parts[2].strip().lower() in {"-", "clear", "default"} else parts[2]
            return self._update_agent_response(parts[1], promotion_branch_name=branch_name)
        if stripped.startswith("/agent_commit "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_commit <agent_name> <on|off>"
            try:
                enabled = _parse_toggle(parts[2])
            except ValueError as exc:
                return str(exc)
            return self._update_agent_response(parts[1], commit_on_promotion=enabled)
        if stripped.startswith("/agent_commit_message "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_commit_message <agent_name> <message|-|clear>"
            message = "" if parts[2].strip().lower() in {"-", "clear", "default"} else parts[2]
            return self._update_agent_response(parts[1], promotion_commit_message=message)
        if stripped.startswith("/agent_run_until "):
            parts = stripped.split(maxsplit=3)
            if len(parts) != 4:
                return "usage: /agent_run_until <agent_name> <target_metric> <max_experiments>"
            try:
                target_metric = _parse_float(parts[2], field_name="target_metric")
                max_experiments = _parse_positive_int(parts[3], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._run_agent_until_response(parts[1], target_metric=target_metric, max_experiments=max_experiments)
        if stripped.startswith("/agent_publish "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_publish <agent_name> <max_experiments>"
            try:
                max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._publish_agent_response(parts[1], max_experiments=max_experiments)
        if stripped.startswith("/agent_pr "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_pr <agent_name> <max_experiments>"
            try:
                max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._pull_request_response(parts[1], max_experiments=max_experiments)
        if stripped.startswith("/agent_run "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /agent_run <agent_name> <max_experiments>"
            try:
                max_experiments = _parse_positive_int(parts[2], field_name="max_experiments")
            except ValueError as exc:
                return str(exc)
            return self._run_agent_response(parts[1], max_experiments=max_experiments)
        if stripped == "/approvals":
            return json.dumps(self.approvals.list_pending(), indent=2, sort_keys=True)
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
        if stripped.startswith("/trace "):
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
            return self._task_approve_response(parts[1], parts[2])
        if stripped.startswith("/task_abort "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /task_abort <approval_id>"
            return self._task_abort_response(parts[1])
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
        if stripped.startswith("/feedback"):
            if self.learning is None:
                return "learning loop not available"
            parts = stripped.split(maxsplit=2)
            if len(parts) < 2:
                return "usage: /feedback <positive|negative|note> [outcome_id]"
            rating = parts[1]
            oid = int(parts[2]) if len(parts) == 3 else None
            return self.learning.feedback(oid, rating)
        if stripped.startswith("/pipeline_merge "):
            if self.pipeline is None:
                return "pipeline service unavailable"
            parts = stripped.split(maxsplit=1)
            issue_id = parts[1].strip()
            try:
                run = self.pipeline.merge_and_close(issue_id)
                return json.dumps({"issue": run.issue_id, "status": run.status, "pr_url": run.pr_url}, indent=2)
            except Exception:
                logger.exception("pipeline merge error for %s", issue_id)
                return "merge error — check logs for details"
        if stripped == "/pipeline_status":
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
                return json.dumps({"issue": run.issue_id, "status": run.status, "branch": run.branch_name, "approval_id": run.approval_id, "approval_token": run.approval_token}, indent=2)
            except Exception:
                logger.exception("pipeline error for %s", issue_id)
                return "pipeline error — check logs for details"
        if stripped in ("/pipeline", "/pipeline_approve", "/pipeline_merge", "/social_preview", "/social_publish", "/feedback"):
            return f"usage: {stripped} <argument>"
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
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                results = [self.social_publisher.publish(d) for d in drafts]
                return json.dumps([{"platform": r.platform, "post_id": r.post_id, "url": r.url} for r in results], indent=2)
            except Exception:
                logger.exception("social_publish error for %s", account)
                return "error publishing — check logs"
        # --- NotebookLM commands ---
        if stripped.startswith("/nlm_"):
            return self._nlm_dispatch(session_id, stripped)

        nlm_response = self._nlm_natural_language_response(session_id, stripped)
        if nlm_response is not None:
            return nlm_response

        return self._brain_text_response(session_id, stripped)

    def _help_response(self, topic: str | None = None) -> str:
        if topic is None:
            return (
                "Comandos principales:\n"
                "/status - salud general del bot\n"
                "/approvals - aprobaciones pendientes\n"
                "/traces [limit] - traces recientes\n"
                "/trace <trace_id> [limit] - replay de una traza\n"
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
                "/task_run <objetivo> - ejecutar ciclo coordinado\n"
                "/task_loop - inspeccionar presupuesto y checkpoint actual\n"
                "/task_queue [mode] - listar siguientes pasos pendientes de la sesión\n"
                "/task_done <task_id> - marcar paso de la cola como completado\n"
                "/task_defer <task_id> - posponer paso de la cola\n"
                "/task_pending - listar aprobaciones/tareas pendientes de la sesión\n"
                "/session_state - inspeccionar estado persistente de la sesión\n"
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
                "/help autonomy"
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
            "Prueba con: approvals, pipeline, agents, terminal, browser, social, notebooklm."
        )

    def _brain_text_response(self, session_id: str, text: str, *, memory_text: str | None = None) -> str:
        prompt_text = text
        source_text = memory_text or text
        runtime_capability_question = _looks_like_runtime_capability_question(text)
        link_analysis_context = _extract_link_analysis_context(text)
        enriched = _enrich_tweet_urls(text)
        if enriched != text:
            prompt_text = _format_tweet_analysis_prompt(text, enriched)
        if runtime_capability_question:
            prompt_text = _format_runtime_capability_prompt(prompt_text)
        response = self.brain.handle_message(
            session_id,
            prompt_text,
            memory_text=source_text,
            task_type="telegram_message",
        )

        raw_content = response.content or ""
        content = raw_content.strip()
        if not content or content == "(no result)":
            content = "Recibido. ¿Qué quieres que haga con esto?"
        elif runtime_capability_question:
            content = _enforce_runtime_capability_sections(content)
        elif link_analysis_context is not None:
            content = _enforce_link_analysis_sections(
                content,
                url=link_analysis_context["url"],
                fetched_content=link_analysis_context["fetched_content"],
            )
        if content != raw_content:
            self.brain.memory.replace_latest_assistant_message(session_id, raw_content, content)
        if content == "Recibido. ¿Qué quieres que haga con esto?":
            self._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Bot returned fallback for message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome="failure",
                error_snippet=(raw_content or "empty_response")[:500],
                lesson="When the brain returns empty output, ask a clarifying question and inspect prompt/context assembly.",
            )
        else:
            self._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Handled message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome="success",
                lesson="The brain produced a usable reply for this conversational request.",
            )
        self._remember_assistant_turn_state(session_id, source_text, content)
        return content

    def handle_multimodal(
        self,
        *,
        user_id: str,
        session_id: str,
        content_blocks: list[dict[str, Any]],
        memory_text: str,
    ) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        return self.brain.handle_message(
            session_id,
            content_blocks,
            memory_text=memory_text,
            task_type="telegram_message",
        ).content

    def _list_agents_payload(self) -> list[dict]:
        payload: list[dict] = []
        for agent_name in self.auto_research.list_agents():
            state = self.auto_research.inspect(agent_name)
            payload.append(_agent_summary(agent_name, state))
        return payload

    def _agent_state_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _create_agent_response(self, agent_name: str, agent_class: str, instruction: str) -> str:
        try:
            state = self.auto_research.create_agent(
                AgentDefinition(
                    name=agent_name,
                    agent_class=agent_class,
                    instruction=instruction,
                )
            )
        except FileExistsError:
            return f"agent already exists: {agent_name}"
        except ValueError as exc:
            return str(exc)
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _update_agent_response(self, agent_name: str, **changes: object) -> str:
        try:
            state = self.auto_research.update_controls(agent_name, **changes)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        except ValueError as exc:
            return str(exc)
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _pause_agent_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.pause(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _resume_agent_response(self, agent_name: str) -> str:
        try:
            state = self.auto_research.resume(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_agent_summary(agent_name, state, include_instruction=True), indent=2, sort_keys=True)

    def _agent_history_response(self, agent_name: str, *, limit: int) -> str:
        try:
            state = self.auto_research.inspect(agent_name)
            history = self.auto_research.history(agent_name, limit=limit)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(
            {
                **_agent_summary(agent_name, state, include_instruction=True),
                "history_limit": limit,
                "history_count": len(history),
                "history": [_record_summary(item) for item in history],
            },
            indent=2,
            sort_keys=True,
        )

    def _run_agent_response(self, agent_name: str, *, max_experiments: int) -> str:
        try:
            result = self.auto_research.run_loop(agent_name, max_experiments=max_experiments)
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_run_summary(agent_name, state, result), indent=2, sort_keys=True)

    def _run_agent_until_response(self, agent_name: str, *, target_metric: float, max_experiments: int) -> str:
        try:
            result = self.auto_research.run_until(
                agent_name,
                target_metric=target_metric,
                max_experiments=max_experiments,
            )
            state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"
        return json.dumps(_run_summary(agent_name, state, result), indent=2, sort_keys=True)

    def _publish_agent_response(self, agent_name: str, *, max_experiments: int) -> str:
        payload = self._publish_agent_payload(agent_name, max_experiments=max_experiments)
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, indent=2, sort_keys=True)

    def _publish_agent_payload(self, agent_name: str, *, max_experiments: int) -> dict | str:
        try:
            previous_state = self.auto_research.inspect(agent_name)
        except FileNotFoundError:
            return f"agent not found: {agent_name}"

        update_payload: dict[str, object] = {}
        if not previous_state.get("promote_on_improvement", False):
            update_payload["promote_on_improvement"] = True
        if not previous_state.get("commit_on_promotion", False):
            update_payload["commit_on_promotion"] = True
        if not previous_state.get("branch_on_promotion", False):
            update_payload["branch_on_promotion"] = True
        if update_payload:
            self.auto_research.update_controls(agent_name, **update_payload)

        result = self.auto_research.run_loop(agent_name, max_experiments=max_experiments)
        state = self.auto_research.inspect(agent_name)
        latest = self.auto_research.latest_result(agent_name)
        payload = _run_summary(agent_name, state, result)
        payload["publish_mode_updated"] = bool(update_payload)
        if latest is not None:
            payload["published_commit_sha"] = latest.promotion_commit_sha
            payload["published_branch_name"] = latest.promotion_branch_name
            payload["published"] = bool(latest.promotion_commit_sha or latest.promotion_branch_name)
        else:
            payload["published_commit_sha"] = None
            payload["published_branch_name"] = None
            payload["published"] = False
        return payload

    def _pull_request_response(self, agent_name: str, *, max_experiments: int) -> str:
        payload = self._publish_agent_payload(agent_name, max_experiments=max_experiments)
        if isinstance(payload, str):
            return payload
        if self.pull_requests is None:
            payload["pull_request_created"] = False
            payload["pull_request_error"] = "pull request service unavailable"
            return json.dumps(payload, indent=2, sort_keys=True)
        branch_name = payload.get("published_branch_name")
        if not isinstance(branch_name, str) or not branch_name:
            payload["pull_request_created"] = False
            payload["pull_request_error"] = "no published branch available for pull request creation"
            return json.dumps(payload, indent=2, sort_keys=True)

        title = _default_pull_request_title(agent_name, payload)
        body = _default_pull_request_body(agent_name, payload)
        try:
            pr = self.pull_requests.create_pull_request(
                branch_name=branch_name,
                title=title,
                body=body,
                draft=True,
            )
        except Exception as exc:  # pragma: no cover - depends on local gh/git runtime
            payload["pull_request_created"] = False
            payload["pull_request_error"] = str(exc)
            return json.dumps(payload, indent=2, sort_keys=True)

        payload["pull_request_created"] = True
        payload["pull_request_url"] = pr.url
        payload["pull_request_number"] = pr.number
        payload["pull_request_title"] = pr.title
        payload["pull_request_draft"] = pr.draft
        return json.dumps(payload, indent=2, sort_keys=True)

    def _browse_response(self, url: str, *, session_id: str | None = None) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        self._remember_recent_browse_url(session_id, normalized_url)

        backend = self._browse_backend()
        playwright_available = backend in {"auto", "playwright_local"} and self.browser is not None
        browserbase_available = (
            backend == "browserbase_cdp"
            and self.browser is not None
            and self.config is not None
            and bool(getattr(self.config, "browserbase_api_key", None))
            and bool(getattr(self.config, "browserbase_project_id", None))
        )
        cdp_available = (
            backend in {"auto", "chrome_cdp"}
            and self.managed_chrome is not None
            and self.browser is not None
        )
        tweet_fallback = _tweet_fxtwitter_read(normalized_url) if _is_tweet_url(normalized_url) else ""
        auth_required = _needs_real_browser(normalized_url)
        started_at = time.perf_counter()

        if auth_required:
            response, outcome = self._browse_authenticated_response(
                normalized_url,
                tweet_fallback=tweet_fallback,
                configured_backend=backend,
                cdp_available=cdp_available,
                browserbase_available=browserbase_available,
                playwright_available=playwright_available,
            )
        else:
            response, outcome = self._browse_public_response(
                normalized_url,
                configured_backend=backend,
                cdp_available=cdp_available,
                browserbase_available=browserbase_available,
                playwright_available=playwright_available,
            )

        self._emit_browse_event(
            url=normalized_url,
            configured_backend=backend,
            strategy=outcome["strategy"],
            selected_backend=outcome["selected_backend"],
            status=outcome["status"],
            auth_required=auth_required,
            duration_ms=round((time.perf_counter() - started_at) * 1000, 1),
            note=outcome.get("note"),
        )
        if outcome["status"] != "success":
            self._record_learning_outcome(
                task_type="browse",
                session_id=session_id,
                description=f"Browse {outcome['status']} for {normalized_url}",
                approach=f"strategy={outcome['strategy']} backend={outcome['selected_backend']}",
                outcome="failure" if outcome["status"] == "error" else "partial",
                error_snippet=response[:500],
                lesson=(
                    "Authenticated or JS-heavy pages need a better backend selection or clearer fallback messaging."
                    if outcome["strategy"] == "authenticated"
                    else "When all browse backends fail, capture the failing backend path and retry strategy explicitly."
                ),
            )
        else:
            # Auto-ingest successful browse into wiki
            browse_title = _extract_title_from_url(normalized_url)
            self._maybe_wiki_ingest(browse_title, response, source_type="browse")
        return response

    def _browse_backend(self) -> str:
        if self.config is None:
            return "auto"
        backend = getattr(self.config, "browse_backend", "auto")
        if not isinstance(backend, str) or not backend.strip():
            return "auto"
        return backend.strip().lower()

    def _browse_public_response(
        self,
        url: str,
        *,
        configured_backend: str,
        cdp_available: bool,
        browserbase_available: bool,
        playwright_available: bool,
    ) -> tuple[str, dict[str, str]]:
        content = _jina_read(url)
        if content:
            return content[:6000], {
                "strategy": "public",
                "selected_backend": "jina",
                "status": "success",
            }

        if playwright_available:
            content = self._playwright_browse_response(url)
            if content:
                return content, {
                    "strategy": "public",
                    "selected_backend": "playwright_local",
                    "status": "success",
                }

        if browserbase_available:
            content = self._browserbase_browse_response(url)
            if content:
                return content, {
                    "strategy": "public",
                    "selected_backend": "browserbase_cdp",
                    "status": "success",
                }

        if configured_backend == "chrome_cdp" and cdp_available:
            try:
                result = self.browser.chrome_navigate(
                    url,
                    cdp_url=self.managed_chrome.cdp_url,
                )
                if _is_usable_browse_content(url, result.content):
                    return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}", {
                        "strategy": "public",
                        "selected_backend": "chrome_cdp",
                        "status": "success",
                    }
            except Exception as exc:
                return _format_chrome_cdp_error(exc, prefix="browse error"), {
                    "strategy": "public",
                    "selected_backend": "chrome_cdp",
                    "status": "error",
                    "note": "cdp_failed",
                }

        return f"browse error: no se pudo leer {url}", {
            "strategy": "public",
            "selected_backend": "none",
            "status": "error",
            "note": "all_backends_failed",
        }

    def _browse_authenticated_response(
        self,
        url: str,
        *,
        tweet_fallback: str,
        configured_backend: str,
        cdp_available: bool,
        browserbase_available: bool,
        playwright_available: bool,
    ) -> tuple[str, dict[str, str]]:
        if cdp_available:
            try:
                from urllib.parse import urlparse
                host = urlparse(url).netloc.lower()
                result = self.browser.chrome_navigate(
                    url,
                    cdp_url=self.managed_chrome.cdp_url,
                    page_url_pattern=host,
                )
                if _is_usable_browse_content(url, result.content):
                    return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}", {
                        "strategy": "authenticated",
                        "selected_backend": "chrome_cdp",
                        "status": "success",
                    }
            except Exception as exc:
                if configured_backend == "chrome_cdp":
                    return _format_chrome_cdp_error(exc, prefix="browse error"), {
                        "strategy": "authenticated",
                        "selected_backend": "chrome_cdp",
                        "status": "error",
                        "note": "cdp_failed",
                    }

        if browserbase_available:
            content = self._browserbase_browse_response(url)
            if content:
                return f"Contenido parcial (sesión remota sin cookies locales):\n\n{content}", {
                    "strategy": "authenticated",
                    "selected_backend": "browserbase_cdp",
                    "status": "partial",
                    "note": "remote_session_no_local_cookies",
                }

        fallback_content, fallback_backend, note = self._browse_textual_fallback(
            url,
            tweet_fallback=tweet_fallback,
            playwright_available=playwright_available,
        )
        if fallback_content:
            return fallback_content, {
                "strategy": "authenticated",
                "selected_backend": fallback_backend,
                "status": "partial",
                "note": note,
            }

        return f"browse error: {url} requiere navegador autenticado y Chrome CDP no está disponible.", {
            "strategy": "authenticated",
            "selected_backend": "none",
            "status": "error",
            "note": "no_authenticated_backend",
        }

    def _browse_textual_fallback(
        self,
        url: str,
        *,
        tweet_fallback: str,
        playwright_available: bool,
    ) -> tuple[str, str, str]:
        if tweet_fallback:
            return tweet_fallback[:6000], "tweet_fallback", "tweet_reader"

        if playwright_available:
            content = self._playwright_browse_response(url)
            if content:
                return (
                    f"Contenido parcial (sin sesión autenticada):\n\n{content}",
                    "playwright_local",
                    "no_authenticated_session",
                )

        content = _jina_read(url)
        if content:
            return (
                f"Contenido parcial (CDP no disponible):\n\n{content[:6000]}",
                "jina",
                "best_effort_textual",
            )
        return "", "none", "all_textual_fallbacks_failed"

    def _playwright_browse_response(self, url: str) -> str:
        if self.browser is None or not hasattr(self.browser, "browse"):
            return ""
        try:
            result = self.browser.browse(url)
        except Exception:
            logger.debug("Playwright local browse failed for %s", url, exc_info=True)
            return ""
        if not _is_usable_browse_content(url, result.content):
            return ""
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def _browserbase_browse_response(self, url: str) -> str:
        if self.browser is None or self.config is None or not hasattr(self.browser, "browserbase_browse"):
            return ""
        api_key = getattr(self.config, "browserbase_api_key", None)
        project_id = getattr(self.config, "browserbase_project_id", None)
        if not api_key or not project_id:
            return ""
        try:
            result = self.browser.browserbase_browse(
                url,
                api_key=api_key,
                project_id=project_id,
                api_url=getattr(self.config, "browserbase_api_url", "https://api.browserbase.com"),
                region=getattr(self.config, "browserbase_region", None),
                keep_alive=bool(getattr(self.config, "browserbase_keep_alive", False)),
            )
        except Exception:
            logger.debug("Browserbase browse failed for %s", url, exc_info=True)
            return ""
        if not _is_usable_browse_content(url, result.content):
            return ""
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def _emit_browse_event(
        self,
        *,
        url: str,
        configured_backend: str,
        strategy: str,
        selected_backend: str,
        status: str,
        auth_required: bool,
        duration_ms: float,
        note: str | None = None,
    ) -> None:
        if self.observe is None:
            return
        payload = {
            "url": url,
            "configured_backend": configured_backend,
            "strategy": strategy,
            "selected_backend": selected_backend,
            "status": status,
            "auth_required": auth_required,
            "duration_ms": duration_ms,
        }
        if note:
            payload["note"] = note
        self.observe.emit("browse_result", payload=payload)

    def _record_learning_outcome(
        self,
        *,
        task_type: str,
        session_id: str | None,
        description: str,
        approach: str,
        outcome: str,
        error_snippet: str | None = None,
        lesson: str | None = None,
    ) -> None:
        if self.learning is None:
            return
        task_id = f"{session_id or 'global'}:{time.time_ns()}"
        try:
            self.learning.record(
                task_type=task_type,
                task_id=task_id,
                description=description,
                approach=approach,
                outcome=outcome,
                error_snippet=error_snippet,
                lesson=lesson,
            )
        except Exception:
            logger.debug("learning record failed for %s", task_type, exc_info=True)

    def _maybe_wiki_ingest(self, title: str, content: str, *, source_type: str = "article") -> None:
        """Auto-ingest content into the wiki if available. Runs in a background thread."""
        if self.wiki is None or not content or len(content) < 100:
            return
        def _do_ingest():
            try:
                result = self.wiki.ingest(title, content, source_type=source_type)
                logger.info(
                    "Wiki auto-ingest '%s': %d pages written", title, result["pages_written"]
                )
            except Exception:
                logger.debug("Wiki auto-ingest failed for '%s'", title, exc_info=True)
        threading.Thread(target=_do_ingest, daemon=True).start()

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
            "model": "Claude Opus 4.6 / Sonnet 4.6",
            "context_window": context_window,
            "max_output": max_output,
            "messages_count": message_count,
            "estimated_tokens": estimated_tokens,
            "estimated_percentage": round(estimated_pct, 1),
            "status": status,
            "status_display": f"{status_emoji} {status.title()}",
            "recommendation": recommendation,
        }, indent=2, sort_keys=True)

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
        return json.dumps(
            {"trace_id": trace_id, "event_count": len(replay), "events": replay},
            indent=2,
            sort_keys=True,
        )

    def _terminal_list_response(self) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            sessions = self.terminal_bridge.list_sessions()
        except Exception as exc:
            return f"terminal list error: {exc}"
        return json.dumps({"sessions": sessions}, indent=2, sort_keys=True)

    def _terminal_open_response(self, tool: str, *, cwd: str | None) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.open(tool, cwd=cwd)
        except Exception as exc:
            return f"terminal open error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_status_response(self, session_id: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.status(session_id)
        except Exception as exc:
            return f"terminal status error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_read_response(self, session_id: str, *, offset: int) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.read(session_id, offset=offset, limit=3000)
        except Exception as exc:
            return f"terminal read error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_send_response(self, session_id: str, text: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.send(session_id, text)
        except Exception as exc:
            return f"terminal send error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _terminal_close_response(self, session_id: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.close(session_id)
        except Exception as exc:
            return f"terminal close error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _chrome_pages_response(self) -> str:
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            pages = self.browser.connect_to_chrome(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome CDP error")
        return json.dumps({"pages": pages}, indent=2, sort_keys=True)

    def _chrome_browse_response(self, url: str, *, session_id: str | None = None) -> str:
        try:
            normalized_url = _normalize_url(url)
        except ValueError as exc:
            return str(exc)
        self._remember_recent_browse_url(session_id, normalized_url)
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        tweet_fallback = _tweet_fxtwitter_read(normalized_url) if _is_tweet_url(normalized_url) else ""
        try:
            from urllib.parse import urlparse
            host = urlparse(normalized_url).netloc.lower()
            result = self.browser.chrome_navigate(
                normalized_url,
                cdp_url=self.managed_chrome.cdp_url,
                page_url_pattern=host,
            )
        except Exception as exc:
            if tweet_fallback:
                return tweet_fallback[:6000]
            return _format_chrome_cdp_error(exc, prefix="chrome browse error")
        if not _is_usable_browse_content(normalized_url, result.content) and tweet_fallback:
            return tweet_fallback[:6000]
        return f"**{result.title}** ({result.url})\n\n{result.content[:6000]}"

    def _chrome_shot_response(self, command: str) -> str:
        if self.browser is None or self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            result = self.browser.chrome_screenshot(cdp_url=self.managed_chrome.cdp_url)
        except Exception as exc:
            return _format_chrome_cdp_error(exc, prefix="chrome screenshot error")
        return json.dumps({
            "url": result.url,
            "title": result.title,
            "screenshot_path": result.screenshot_path,
        }, indent=2)

    def _chrome_login_response(self) -> str:
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=False)
            return "Chrome reiniciado en modo visible. Haz login en los sitios que necesites. Cuando termines: /chrome_headless"
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"

    def _chrome_headless_response(self) -> str:
        if self.managed_chrome is None:
            return "Chrome no disponible."
        try:
            self.managed_chrome.stop()
            self.managed_chrome.start(headless=True)
            return "Chrome reiniciado en modo headless."
        except Exception as exc:
            return f"Error reiniciando Chrome: {exc}"

    def _screen_response(self) -> str:
        if self.computer is None:
            return "computer use unavailable"
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"screenshot error: {exc}"
        return json.dumps({"screenshot_data": screenshot["data"][:100] + "...", "media_type": screenshot["media_type"]})

    def _computer_response(self, instruction: str, session_id: str) -> str:
        if self.computer is None:
            return "computer use unavailable"
        if _computer_instruction_requires_actions(instruction):
            return self._computer_action_response(instruction, session_id)
        try:
            screenshot = self.computer.capture_screenshot()
        except Exception as exc:
            return f"computer screenshot error: {exc}"

        content_blocks = [
            {"type": "text", "text": instruction},
            {"type": "image", "source": {"type": "base64", **screenshot}},
        ]
        memory_text = f"[Screenshot de escritorio]\n{instruction.strip()}"
        return self.brain.handle_message(
            session_id,
            content_blocks,
            memory_text=memory_text,
        ).content

    def _computer_action_response(self, instruction: str, session_id: str) -> str:
        # Try browser_use (OpenAI) first — no Anthropic API credits needed
        if self.browser_use is not None:
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(asyncio.run, self.browser_use.run_task(instruction)).result(timeout=120)
                else:
                    result = asyncio.run(self.browser_use.run_task(instruction))
                return result
            except Exception as exc:
                logger.warning("browser_use fallback failed: %s", exc)

        from claw_v2.computer import ComputerSession

        with self._state_lock:
            active = self._computer_sessions.get(session_id)
            if active is not None:
                active.status = "aborted"
            session = ComputerSession(task=instruction)
            self._computer_sessions[session_id] = session
        return self._run_computer_session(session_id)

    def _run_computer_session(self, session_id: str) -> str:
        session = self._computer_sessions.get(session_id)
        if session is None:
            return "no active computer session"
        try:
            gate = self._get_computer_gate()
            client = None if self.computer.codex_backend is not None else self._get_computer_client()
            result = self.computer.run_agent_loop(
                session=session,
                client=client,
                gate=gate,
                model=self.computer_model,
                system_prompt=self.computer_system_prompt,
            )
        except Exception as exc:
            self._computer_sessions.pop(session_id, None)
            logger.exception("computer use failed for %s", session_id)
            if self.observe is not None:
                self.observe.emit("error", payload={"source": "computer_use", "error": str(exc)[:200]})
            return f"computer use error: {exc}"

        if session.status == "awaiting_approval":
            pending = dict(session.pending_action or {})
            pending_approval = self.approvals.create(
                action=pending.get("action", "computer_action"),
                summary=_format_computer_pending_summary(session.task, pending),
                metadata={
                    "kind": "computer_use",
                    "session_id": session_id,
                    "task": session.task,
                    "pending_action": pending,
                },
            )
            session.pending_action = {
                **pending,
                "approval_id": pending_approval.approval_id,
                "approval_token": pending_approval.token,
            }
            return (
                f"{result}\n\n"
                f"Approve via: `/action_approve {pending_approval.approval_id} {pending_approval.token}`\n"
                f"Abort via: `/action_abort {pending_approval.approval_id}`"
            )

        if session.status in {"done", "aborted"}:
            self._computer_sessions.pop(session_id, None)
        return result

    def _get_computer_client(self) -> Any:
        if self._computer_client is not None:
            return self._computer_client
        if self.computer_client_factory is not None:
            self._computer_client = self.computer_client_factory()
            return self._computer_client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency is installed in runtime
            raise RuntimeError("openai SDK is not installed") from exc
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for computer use")
        self._computer_client = OpenAI(api_key=api_key)
        return self._computer_client

    def _get_computer_gate(self) -> Any:
        if self.computer_gate is not None:
            return self.computer_gate
        from claw_v2.computer_gate import ActionGate

        sensitive_urls = getattr(self.config, "sensitive_urls", []) if self.config is not None else []
        self.computer_gate = ActionGate(sensitive_urls=sensitive_urls)
        return self.computer_gate

    def _action_approve_response(self, approval_id: str, token: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            valid = self.approvals.approve(approval_id, token)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        if not valid:
            return "invalid token"
        payload = self.approvals.read(approval_id)
        metadata = payload.get("metadata", {})
        if metadata.get("kind") != "computer_use":
            return "approved"
        session_id = metadata.get("session_id")
        if not isinstance(session_id, str):
            return "approved, but no computer session metadata was found"
        session = self._computer_sessions.get(session_id)
        if session is None:
            return "approved, but the computer session is no longer active"
        if session.pending_action is not None and session.pending_action.get("approval_id") != approval_id:
            return "approved, but no matching pending computer action was found"
        session.status = "running"
        return self._run_computer_session(session_id)

    def _action_abort_response(self, approval_id: str) -> str:
        if self.approvals is None:
            return "approvals unavailable"
        try:
            payload = self.approvals.read(approval_id)
            self.approvals.reject(approval_id)
        except FileNotFoundError:
            return f"approval {approval_id} not found"
        metadata = payload.get("metadata", {})
        if metadata.get("kind") == "computer_use":
            session_id = metadata.get("session_id")
            if isinstance(session_id, str):
                session = self._computer_sessions.pop(session_id, None)
                if session is not None:
                    session.status = "aborted"
            return "computer action rejected"
        return "action rejected"

    def _task_approve_response(self, approval_id: str, token: str) -> str:
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
        return self._coordinated_task_response(
            session_id,
            objective,
            forced=True,
            approved_actions=tuple(str(action) for action in approved_actions),
        )

    def _task_abort_response(self, approval_id: str) -> str:
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
            self.brain.memory.update_session_state(
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

    def _wiki_stats_response(self) -> str:
        if self.wiki is None:
            return "wiki service not available"
        stats = self.wiki.stats()
        return (
            f"Wiki: {stats['wiki_pages']} pages, {stats['raw_sources']} raw sources\n"
            f"Root: {stats['wiki_root']}"
        )

    def _wiki_lint_response(self) -> str:
        if self.wiki is None:
            return "wiki service not available"
        result = self.wiki.lint()
        parts = [f"Pages: {result.get('total_pages', 0)}, Issues: {result['issues']}"]
        if result["orphans"]:
            parts.append(f"Orphans: {', '.join(result['orphans'][:10])}")
        if result["missing"]:
            parts.append(f"Missing: {', '.join(result['missing'][:10])}")
        return "\n".join(parts)

    def _wiki_ingest_response(self, title: str, session_id: str) -> str:
        if self.wiki is None:
            return "wiki service not available"
        # Use recent assistant message as content to ingest
        recent = self.brain.memory.get_recent_messages(session_id, limit=2)
        content = ""
        for msg in reversed(recent):
            if msg.get("role") == "assistant" and msg.get("content"):
                content = msg["content"]
                break
        if not content:
            return "No recent content to ingest. Send content first, then /wiki ingest <title>"
        result = self.wiki.ingest(title, content)
        return (
            f"Ingested: {title}\n"
            f"Pages written: {result['pages_written']}, Updates: {result['updates']}, New: {result['new_pages']}"
        )

    def _wiki_query_response(self, question: str) -> str:
        if self.wiki is None:
            return "wiki service not available"
        answer = self.wiki.query(question, archive=True)
        return answer or "No relevant information found in the wiki."

    def _computer_abort_response(self, session_id: str) -> str:
        session = self._computer_sessions.pop(session_id, None)
        if session is None:
            return "no active computer session"
        session.status = "aborted"
        return "computer session aborted"

    def _set_autonomy_mode_response(self, session_id: str, value: str) -> str:
        mode = _parse_autonomy_mode(value)
        state = self.brain.memory.update_session_state(
            session_id,
            autonomy_mode=mode,
            step_budget=_default_step_budget(mode),
        )
        return json.dumps(state, indent=2, sort_keys=True)

    def _remember_user_turn_state(self, session_id: str, text: str) -> None:
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
        current = self.brain.memory.get_session_state(session_id)
        self.brain.memory.update_session_state(
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

    def _remember_assistant_turn_state(self, session_id: str, user_text: str, reply_text: str) -> None:
        state = self.brain.memory.get_session_state(session_id)
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
            depends_on = self._derive_task_dependencies(task_queue, summary=pending_action)
            task_queue = self._upsert_task_queue_entry(
                task_queue,
                summary=pending_action,
                mode=_infer_session_mode(user_text, reply_text),
                status="pending",
                source="assistant",
                priority=1,
                depends_on=depends_on,
            )
        elif verification_status == "passed":
            task_queue = self._mark_first_task_queue_entry(task_queue, from_status="in_progress", to_status="done")
            task_queue = self._mark_first_task_queue_entry(task_queue, from_status="pending", to_status="done")
        elif verification_status == "failed":
            task_queue = self._mark_first_task_queue_entry(task_queue, from_status="in_progress", to_status="blocked")
        self.brain.memory.update_session_state(
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

    def _maybe_resolve_stateful_followup(self, text: str, *, session_id: str) -> str | _BrainShortcut | None:
        if not text or text.startswith("/"):
            return None
        state = self.brain.memory.get_session_state(session_id)
        option_index = _extract_option_reference(text)
        if option_index is not None:
            options = state.get("last_options") or []
            if 1 <= option_index <= len(options):
                selected = options[option_index - 1]
                self.brain.memory.update_session_state(
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
                task_queue = self._mark_task_queue_in_progress(state.get("task_queue") or [], summary=pending_action)
                self.brain.memory.update_session_state(session_id, task_queue=task_queue)
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
                task_queue = self._mark_task_queue_in_progress(task_queue, task_id=next_task.get("task_id"))
                self.brain.memory.update_session_state(session_id, task_queue=task_queue)
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

    def _maybe_run_coordinated_task(self, session_id: str, text: str) -> str | None:
        if self.coordinator is None or not text or text.startswith("/"):
            return None
        state = self.brain.memory.get_session_state(session_id)
        if state.get("autonomy_mode") != "autonomous":
            return None
        if _extract_option_reference(text) is not None or _looks_like_proceed_request(text):
            return None
        mode = _infer_session_mode(text)
        policy = _evaluate_autonomy_policy(text, mode=mode, forced=False)
        if not policy["allowed"] and policy["reason"] == "sensitive_action":
            self.brain.memory.update_session_state(
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
        return self._coordinated_task_response(session_id, text, forced=False)

    def _coordinated_task_response(
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
                self.brain.memory.update_session_state(
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
            self.brain.memory.update_session_state(
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
        self.brain.memory.update_session_state(
            session_id,
            mode=mode,
            pending_action=checkpoint.get("pending_action"),
            task_queue=self._upsert_task_queue_entry(
                self.brain.memory.get_session_state(session_id).get("task_queue") or [],
                summary=checkpoint.get("pending_action") or checkpoint.get("summary") or objective,
                mode=mode,
                status="pending" if checkpoint.get("pending_action") else checkpoint.get("verification_status", "unknown"),
                source="coordinator",
                priority=0,
                depends_on=self._derive_task_dependencies(
                    self.brain.memory.get_session_state(session_id).get("task_queue") or [],
                    summary=checkpoint.get("pending_action") or checkpoint.get("summary") or objective,
                ),
            ) if checkpoint.get("pending_action") or checkpoint.get("summary") else self.brain.memory.get_session_state(session_id).get("task_queue") or [],
            verification_status=checkpoint.get("verification_status", "unknown"),
            last_checkpoint=checkpoint,
        )
        return _format_coordinator_response(result, checkpoint=checkpoint, forced=forced)

    def _updated_pending_task_approvals(self, session_id: str, entry: dict[str, Any]) -> list[dict[str, Any]]:
        state = self.brain.memory.get_session_state(session_id)
        pending = [item for item in (state.get("pending_approvals") or []) if item.get("approval_id") != entry.get("approval_id")]
        pending.append(entry)
        return pending[-5:]

    def _remove_pending_task_approval(self, session_id: str, approval_id: str) -> None:
        state = self.brain.memory.get_session_state(session_id)
        pending = [item for item in (state.get("pending_approvals") or []) if item.get("approval_id") != approval_id]
        self.brain.memory.update_session_state(session_id, pending_approvals=pending)

    def _task_queue_transition_response(self, session_id: str, task_id: str, *, to_status: str) -> str:
        state = self.brain.memory.get_session_state(session_id)
        task_queue = state.get("task_queue") or []
        updated = self._set_task_queue_status(task_queue, task_id=task_id, to_status=to_status)
        if updated == task_queue:
            return f"task {task_id} not found"
        next_pending = _select_next_task_queue_item(updated, preferred_mode=state.get("mode") or "chat")
        self.brain.memory.update_session_state(
            session_id,
            task_queue=updated,
            pending_action=next_pending.get("summary") if next_pending else "",
        )
        return json.dumps(updated, indent=2, sort_keys=True)

    @staticmethod
    def _upsert_task_queue_entry(
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
    def _mark_task_queue_in_progress(
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
    def _mark_first_task_queue_entry(
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
    def _set_task_queue_status(
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
    def _derive_task_dependencies(queue: list[dict[str, Any]], *, summary: str) -> list[str]:
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
                return self._chrome_browse_response(extracted_url, session_id=session_id)
            if normalized_url is not None and (
                _is_local_url(normalized_url)
                or (_has_url_query(normalized_url) and "://" not in extracted_url)
            ):
                return self._browse_response(extracted_url, session_id=session_id)
            if any(token in normalized for token in _LINK_ANALYSIS_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._link_review_shortcut(text, extracted_url, session_id=session_id)
            if any(token in normalized for token in _BROWSE_SHORTCUT_TOKENS) or _looks_like_standalone_url(text, extracted_url):
                return self._browse_response(extracted_url, session_id=session_id)

        if _looks_like_tweet_followup_request(normalized):
            recent_tweet_url = self._recent_tweet_url(session_id)
            if recent_tweet_url is not None:
                return _BrainShortcut(f"{text}\n\n{recent_tweet_url}")

        if _computer_instruction_requires_actions(text):
            return self._computer_action_response(text, session_id)

        if _looks_like_computer_read_request(normalized):
            return self._computer_response(text, session_id)

        if any(token in normalized for token in ("abre", "abrir", "open", "inicia", "iniciar", "run", "corre")):
            if "terminal" in normalized and "claude" in normalized:
                return self._terminal_open_response("claude", cwd=None)
            if "terminal" in normalized and "codex" in normalized:
                return self._terminal_open_response("codex", cwd=None)

        if "google ads" in normalized or "ads.google.com" in normalized:
            if any(token in normalized for token in ("abre", "abrir", "open", "revisa", "revisa", "revisalo", "review", "check")):
                return self._chrome_browse_response("https://ads.google.com", session_id=session_id)

        return None

    def _link_review_shortcut(self, text: str, url: str, *, session_id: str) -> _BrainShortcut:
        try:
            normalized_url = _normalize_url(url)
        except ValueError:
            normalized_url = url
        fetched_content = self._browse_response(url, session_id=session_id)
        return _BrainShortcut(
            text=_format_link_analysis_prompt(text, normalized_url, fetched_content),
            memory_text=text,
        )

    def _remember_recent_browse_url(self, session_id: str | None, url: str) -> None:
        if session_id:
            self._recent_browse_urls[session_id] = url
            self.brain.memory.update_session_state(
                session_id,
                mode="browse",
                active_object={"kind": "url", "url": url},
            )

    def _recent_tweet_url(self, session_id: str) -> str | None:
        url = self._recent_browse_urls.get(session_id)
        if url and _is_tweet_url(url):
            return url
        return None

    # -- NotebookLM handlers ----------------------------------------------------

    def _nlm_dispatch(self, session_id: str, command: str) -> str:
        if self.notebooklm is None:
            return "NotebookLM no disponible. El servicio no está configurado."
        try:
            if command == "/nlm_list":
                return self._nlm_list_response()
            if command.startswith("/nlm_create "):
                title = command.split(maxsplit=1)[1]
                return self._nlm_create_response(session_id, title)
            if command == "/nlm_create":
                return "usage: /nlm_create <titulo>"
            if command.startswith("/nlm_delete "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_delete_response(nb_id)
            if command == "/nlm_delete":
                return "usage: /nlm_delete <notebook_id>"
            if command.startswith("/nlm_status "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_status_response(nb_id)
            if command == "/nlm_status":
                return "usage: /nlm_status <notebook_id>"
            if command.startswith("/nlm_sources "):
                parts = command.split()
                if len(parts) < 3:
                    return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
                return self._nlm_sources_response(parts[1], parts[2:])
            if command == "/nlm_sources":
                return "usage: /nlm_sources <notebook_id> <url1> [url2] ..."
            if command.startswith("/nlm_text "):
                rest = command.split(maxsplit=2)
                if len(rest) < 3 or "|" not in rest[2]:
                    return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
                nb_id = rest[1]
                title_and_content = rest[2]
                pipe_idx = title_and_content.index("|")
                title = title_and_content[:pipe_idx].strip()
                content = title_and_content[pipe_idx + 1:].strip()
                return self._nlm_text_response(nb_id, title, content)
            if command == "/nlm_text":
                return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
            if command.startswith("/nlm_research "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_research <notebook_id> <query>"
                return self._nlm_research_response(parts[1], parts[2])
            if command == "/nlm_research":
                return "usage: /nlm_research <notebook_id> <query>"
            if command.startswith("/nlm_podcast "):
                nb_id = command.split(maxsplit=1)[1]
                return self._nlm_podcast_response(session_id, nb_id)
            if command == "/nlm_podcast":
                return self._nlm_podcast_response(session_id, None)
            if command.startswith("/nlm_chat "):
                parts = command.split(maxsplit=2)
                if len(parts) < 3:
                    return "usage: /nlm_chat <notebook_id> <pregunta>"
                return self._nlm_chat_response(parts[1], parts[2])
            if command == "/nlm_chat":
                return "usage: /nlm_chat <notebook_id> <pregunta>"
            return "Comando NLM no reconocido. Disponibles: /nlm_list, /nlm_create, /nlm_delete, /nlm_status, /nlm_sources, /nlm_text, /nlm_research, /nlm_podcast, /nlm_chat"
        except ValueError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.exception("NLM command error")
            return f"Error en NotebookLM: {exc}"

    def _nlm_list_response(self) -> str:
        notebooks = self.notebooklm.list_notebooks()
        if not notebooks:
            return "No hay notebooks."
        lines = []
        for nb in notebooks:
            short_id = nb["id"][:8]
            date = nb.get("created_at") or "-"
            lines.append(f"{short_id}  {nb['title']}  {date}")
        return "\n".join(lines)

    def _nlm_create_response(self, session_id: str, title: str) -> str:
        result = self.notebooklm.create_notebook(title)
        self._set_active_notebook(session_id, result["id"], result["title"])
        research = self.notebooklm.start_research(result["id"], result["title"])
        return (
            f"Notebook creado: {result['id'][:8]} — {result['title']}\n"
            f"{research}\n"
            "Queda como cuaderno activo para esta conversación."
        )

    def _nlm_delete_response(self, notebook_id: str) -> str:
        self.notebooklm.delete_notebook(notebook_id)
        return "Notebook eliminado."

    def _nlm_status_response(self, notebook_id: str) -> str:
        info = self.notebooklm.status(notebook_id)
        nb = info["notebook"]
        lines = [f"Notebook: {nb['title']} ({nb['id'][:8]})", f"Sources: {nb['sources_count']}"]
        for src in info["sources"]:
            lines.append(f"  - {src['title']} [{src['kind']}]")
        return "\n".join(lines)

    def _nlm_sources_response(self, notebook_id: str, urls: list[str]) -> str:
        results = self.notebooklm.add_sources(notebook_id, urls)
        lines = [f"{len(results)} source(s) agregados:"]
        for src in results:
            lines.append(f"  - {src['title']}")
        return "\n".join(lines)

    def _nlm_text_response(self, notebook_id: str, title: str, content: str) -> str:
        if not title.strip() or not content.strip():
            return "usage: /nlm_text <notebook_id> <titulo> | <contenido>"
        result = self.notebooklm.add_text(notebook_id, title, content)
        return f"Source de texto agregado: {result['title']}"

    def _nlm_research_response(self, notebook_id: str, query: str) -> str:
        return self.notebooklm.start_research(notebook_id, query)

    def _nlm_podcast_response(self, session_id: str, notebook_id: str | None) -> str:
        target = notebook_id or self._active_notebook_id(session_id)
        if target is None:
            return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
        self._set_active_notebook(session_id, target)
        return self.notebooklm.start_podcast(target)

    def _nlm_chat_response(self, notebook_id: str, question: str) -> str:
        return self.notebooklm.chat(notebook_id, question)

    def _nlm_natural_language_response(self, session_id: str, text: str) -> str | None:
        if self.notebooklm is None or not text or text.startswith("/"):
            return None
        if topic := _extract_nlm_create_topic(text):
            return self._nlm_create_response(session_id, topic)
        if kind := _extract_nlm_artifact_kind(text):
            target = self._active_notebook_id(session_id)
            if target is None:
                return "No hay cuaderno activo. Primero dime `creame un cuaderno sobre ...`."
            self._set_active_notebook(session_id, target)
            return self.notebooklm.start_artifact(target, kind)
        return None

    def _set_active_notebook(self, session_id: str, notebook_id: str, title: str | None = None) -> None:
        current = self._active_notebooks.get(session_id, {})
        notebook = {
            "id": notebook_id,
            "title": title or current.get("title", notebook_id[:8]),
        }
        self._active_notebooks[session_id] = notebook
        self.brain.memory.update_session_state(
            session_id,
            mode="research",
            active_object={"kind": "notebook", **notebook},
        )

    def _active_notebook_id(self, session_id: str) -> str | None:
        notebook = self._active_notebooks.get(session_id)
        if notebook is None:
            return None
        return notebook["id"]


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


def _extract_nlm_create_topic(text: str) -> str | None:
    match = _NLM_CREATE_RE.match(text.strip())
    if not match:
        return None
    topic = match.group(1).strip()
    return topic or None


def _extract_nlm_artifact_kind(text: str) -> str | None:
    normalized = _normalize_command_text(text)
    if not any(token in normalized for token in _NLM_ACTION_TOKENS):
        return None
    for token, kind in _NLM_ARTIFACT_KINDS.items():
        if token in normalized:
            return kind
    return None


def _normalize_command_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _stable_task_id(summary: str, *, mode: str, source: str) -> str:
    normalized = _normalize_command_text(summary)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")[:48] or "task"
    return f"{mode}:{source}:{slug}"


def _extract_option_reference(text: str) -> int | None:
    normalized = _normalize_command_text(text).strip()
    match = re.fullmatch(r"(?:opcion|option)\s+(\d+)", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"(\d+)", normalized)
    if match:
        return int(match.group(1))
    match = re.fullmatch(r"la\s+(\w+)", normalized)
    if match:
        return _OPTION_ORDINALS.get(match.group(1))
    return _OPTION_ORDINALS.get(normalized)


def _looks_like_proceed_request(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    if not normalized:
        return False
    if normalized in _PROCEED_TOKENS:
        return True
    return any(normalized.startswith(f"{token} ") for token in _PROCEED_TOKENS)


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
        match = re.match(r"^\s*(?:siguiente paso|next step|pendiente)\s*:\s*(.+?)\s*$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip()
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
        match = re.match(
            r"^\s*(?:verificado|verified|verification status)\s*:\s*(ok|passed|pending|failed|none|unknown)\s*$",
            line,
            re.IGNORECASE,
        )
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
                    "Summarize files touched and what changed. "
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
    pending_action = _extract_pending_action_from_reply(verification_text) or _extract_pending_action_from_reply(implementation_text)
    verification_status = (
        _extract_verification_status(verification_text)
        or ("failed" if result.error else None)
        or ("pending" if verification_results else "unknown")
    )
    summary = _compact_summary(result.synthesis or objective, limit=180) or objective
    checkpoint = {
        "summary": summary,
        "verification_status": verification_status,
    }
    if pending_action:
        checkpoint["pending_action"] = pending_action
    if result.error:
        checkpoint["error"] = result.error
    return checkpoint


def _format_worker_results(results: list[Any]) -> str:
    lines: list[str] = []
    for item in results:
        if item.error:
            lines.append(f"- {item.task_name}: ERROR {item.error}")
        else:
            lines.append(f"- {item.task_name}: {_compact_summary(item.content, limit=240) or '(no content)'}")
    return "\n".join(lines) if lines else "- none"


def _format_coordinator_response(result: CoordinatorResult, *, checkpoint: dict[str, str], forced: bool) -> str:
    lines = [
        f"Coordinator task: {result.task_id}",
        f"Dispatch: {'manual' if forced else 'autonomous'}",
    ]
    if result.error:
        lines.append(f"Error: {result.error}")
    if result.synthesis:
        lines.extend(["", "Plan:", result.synthesis])
    if "implementation" in result.phase_results:
        lines.extend(["", "Implementation:", _format_worker_results(result.phase_results["implementation"])])
    if "verification" in result.phase_results:
        lines.extend(["", "Verification:", _format_worker_results(result.phase_results["verification"])])
    lines.extend(
        [
            "",
            f"Verification Status: {checkpoint.get('verification_status', 'unknown')}",
        ]
    )
    if checkpoint.get("pending_action"):
        lines.append(f"Siguiente paso: {checkpoint['pending_action']}")
    if checkpoint.get("summary"):
        lines.append(f"Checkpoint: {checkpoint['summary']}")
    return "\n".join(lines)


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
    approved_actions: tuple[str, ...] = (),
) -> dict[str, Any]:
    normalized = _normalize_command_text(text)
    autonomy_mode = "autonomous" if not forced else "assisted"
    policy = _policy_for_mode(autonomy_mode)
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
            "summary": f"El modo {mode} no está habilitado para ejecución automática en {autonomy_mode}.",
        }
    requested_actions = _classify_task_actions(normalized, mode=mode)
    effective_allowed_actions = set(policy["allowed_task_actions"]) | set(approved_actions)
    disallowed_actions = sorted(set(requested_actions) - effective_allowed_actions)
    if disallowed_actions:
        labels = ", ".join(disallowed_actions)
        return {
            "allowed": False,
            "reason": "action_not_allowed",
            "summary": f"La tarea pide acciones fuera del scope permitido para {autonomy_mode}: {labels}.",
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
        "Blocked automatic scopes: publish, deploy, push, destructive actions."
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
    if any(token in normalized for token in ("browse", "revisa", "review", "http://", "https://", "www.")):
        return "browse"
    if any(token in normalized for token in ("terminal", "chrome", "screen", "computer", "click", "scroll", "sesion")):
        return "ops"
    if any(token in normalized for token in ("commit", "test", "pytest", "fix", "corrige", "arregla", "bug", "repo", "codigo", "code", "patch")):
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


def _strip_url_punctuation(value: str) -> str:
    candidate = value.strip().strip("<>[]{}\"'")
    while candidate and candidate[-1] in ".,;:!?":
        candidate = candidate[:-1]
    return candidate


def _looks_like_standalone_url(text: str, url: str) -> bool:
    remainder = text.replace(url, " ", 1)
    remainder = re.sub(r"[\s`\"'“”‘’<>()\[\]{}.,;:!?-]+", "", remainder)
    return remainder == ""


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
})


def _needs_real_browser(url: str) -> bool:
    """Check if URL needs Chrome CDP (auth cookies + JS) instead of headless."""
    from urllib.parse import urlparse

    try:
        host = urlparse(url).netloc.lower()
    except (ValueError, AttributeError):
        return False
    return any(host == d or host.endswith(f".{d}") for d in _AUTH_DOMAINS)


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
    urls = _SCHEME_URL_RE.findall(text)
    tweet_urls = [_strip_url_punctuation(u) for u in urls if _is_tweet_url(_strip_url_punctuation(u))]
    if not tweet_urls:
        return text
    enriched = text
    for url in tweet_urls:
        content = _tweet_fxtwitter_read(url)
        if content:
            enriched += f"\n\n---\n[Contenido del tweet pre-cargado]:\n{content}"
    return enriched


def _format_tweet_analysis_prompt(original_text: str, enriched_text: str) -> str:
    return (
        f"{original_text}\n\n"
        "[Instrucción de formato]\n"
        "Si respondes sobre este tweet o sobre enlaces relacionados que el tweet incluye, separa SIEMPRE la respuesta en dos secciones exactas:\n"
        "## Fuente\n"
        "- Resume únicamente lo que está en el tweet y, si aplica, en el contenido enlazado que sí fue leído.\n"
        "- Distingue con claridad qué viene del tweet y qué viene del enlace.\n"
        "- No presentes inferencias o recomendaciones como si fueran parte de la fuente.\n\n"
        "## Aplicación sugerida\n"
        "- Incluye solo inferencias, recomendaciones o ideas prácticas tuyas.\n"
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
        "- Si el contenido vino incompleto, estaba detrás de login o hubo limitaciones de lectura, dilo explícitamente.\n"
        "- No mezcles inferencias, recomendaciones o juicio propio con la fuente.\n\n"
        "## Aplicación sugerida\n"
        "- Incluye solo inferencias, recomendaciones, riesgos o siguientes pasos tuyos.\n"
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
    if not any(token in normalized_text for token in ("tweet", "tweets", "tuit", "tuits", "post")):
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
    action = pending_action.get("action", "unknown")
    if "coordinate" in pending_action:
        return f"{task} — {action} at {pending_action['coordinate']}"
    if "text" in pending_action:
        return f"{task} — {action} {pending_action['text']!r}"
    return f"{task} — {action}"


def _jina_read(url: str, *, timeout: float = 10) -> str:
    """Fetch URL content as markdown via Jina Reader."""
    import httpx
    try:
        response = httpx.get(
            f"https://r.jina.ai/{url}",
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
