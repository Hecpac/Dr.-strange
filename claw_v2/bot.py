from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

logger = logging.getLogger(__name__)

from claw_v2.agents import AutoResearchAgentService
from claw_v2.agent_handler import AgentHandler
from claw_v2.browse_handler import BrowseHandler
from claw_v2.task_handler import TaskHandler
from claw_v2.approval import ApprovalManager
from claw_v2.approval_gate import ApprovalPending
from claw_v2.brain import BrainService
from claw_v2.bot_commands import BotCommand, CommandContext, dispatch_commands
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
from claw_v2.github import GitHubPullRequestService
from claw_v2.heartbeat import HeartbeatService
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


def _format_approval_pending(exc: ApprovalPending) -> str:
    """Convert a Tier 3 soft-block into Telegram-ready instructions for Hector."""
    return (
        "⚠️ Acción de Tier 3 detectada. Requiere aprobación de Hector.\n\n"
        f"Tool: `{exc.tool}`\n"
        f"Resumen: {exc.summary}\n\n"
        f"Comando: `/approve {exc.approval_id} {exc.token}`"
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


def _looks_like_operational_alert(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    return normalized.startswith("alerta operacional:") or normalized.startswith("operational alert:")


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
    ) -> None:
        self.brain = brain
        self.auto_research = auto_research
        self.heartbeat = heartbeat
        self.approvals = approvals
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
        self.learning: Any | None = None
        self._wiki_handler = WikiHandler(memory=brain.memory)
        self._nlm_handler = NlmHandler(update_session_state=brain.memory.update_session_state)
        self._capability_status: dict[str, dict[str, Any]] = {}
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
            get_session_state=brain.memory.get_session_state,
            update_session_state=brain.memory.update_session_state,
            store_message=self._store_message_from_handler,
            workspace_root=getattr(config, "workspace_root", None),
        )
        self._state_handler = StateHandler(
            brain_memory=brain.memory,
            task_handler=self._task_handler,
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
        )
        self._agent_handler = AgentHandler(
            auto_research=auto_research,
            pull_requests=pull_requests,
        )
        self._pre_state_commands = self._build_pre_state_commands()
        self._post_shortcut_commands = self._build_post_shortcut_commands()

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

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        self._ensure_default_autonomy(session_id)
        stripped = text.strip()
        context = CommandContext(user_id=user_id, session_id=session_id, text=text, stripped=stripped)
        command_response = dispatch_commands(self._pre_state_commands, context)
        if isinstance(command_response, _BrainShortcut):
            return self._brain_text_response(
                session_id, command_response.text, memory_text=command_response.memory_text,
            )
        if command_response is not None:
            return command_response
        operational_alert_response = self._maybe_handle_operational_alert(stripped, session_id=session_id)
        if operational_alert_response is not None:
            self._store_memory_turn(session_id, stripped, operational_alert_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, operational_alert_response)
            return operational_alert_response
        task_status_response = self._maybe_handle_task_status_question(stripped, session_id=session_id)
        if task_status_response is not None:
            self._store_memory_turn(session_id, stripped, task_status_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, task_status_response)
            return task_status_response
        self._remember_user_turn_state(session_id, stripped)
        if _looks_like_autonomy_grant(stripped):
            return self._handle_autonomy_grant_response(session_id, stripped)
        stateful_followup = self._maybe_resolve_stateful_followup(stripped, session_id=session_id)
        if isinstance(stateful_followup, _BrainShortcut):
            return self._brain_text_response(
                session_id,
                stateful_followup.text,
                memory_text=stateful_followup.memory_text,
            )
        if stateful_followup is not None:
            self._store_memory_turn(session_id, stripped, stateful_followup, assistant_limit=2000)
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
            self._store_memory_turn(session_id, stripped, shortcut_response, assistant_limit=2000)
            self._remember_assistant_turn_state(session_id, stripped, shortcut_response)
            return shortcut_response
        nlm_response = self._nlm_handler.natural_language_response(session_id, stripped)
        if nlm_response is not None:
            self._store_memory_turn(session_id, stripped, nlm_response, assistant_limit=4000)
            self._remember_assistant_turn_state(session_id, stripped, nlm_response)
            return nlm_response
        coordinated_response = self._task_handler.maybe_run_coordinated_task(session_id, stripped)
        if coordinated_response is not None:
            self._store_memory_turn(session_id, stripped, coordinated_response, assistant_limit=4000)
            if not coordinated_response.startswith("Tarea autónoma iniciada:"):
                self._remember_assistant_turn_state(session_id, stripped, coordinated_response)
            return coordinated_response
        command_response = dispatch_commands(self._post_shortcut_commands, context)
        if command_response is not None:
            return command_response

        return self._brain_text_response(session_id, stripped)

    def _build_pre_state_commands(self) -> list[BotCommand]:
        return [
            BotCommand("help", self._handle_help_command, exact=("/help",), prefixes=("/help ",)),
            BotCommand("status", self._handle_status_command, exact=("/status",)),
            BotCommand("config", self._handle_config_command, exact=("/config",)),
            BotCommand("model", self._handle_model_command, exact=("/models", "/model", "/model status"), prefixes=("/model ",)),
            BotCommand("tokens", self._handle_tokens_command, exact=("/tokens",)),
            BotCommand("spending", self._handle_spending_command, exact=("/spending",)),
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
            BotCommand("pipeline", self._handle_pipeline_command, exact=("/pipeline", "/pipeline_approve", "/pipeline_merge", "/pipeline_status"), prefixes=("/pipeline_approve ", "/pipeline_merge ", "/pipeline ")),
            BotCommand("social", self._handle_social_command, exact=("/social_preview", "/social_publish", "/social_status"), prefixes=("/social_preview ", "/social_publish ")),
            *self._nlm_handler.commands(),
        ]

    def _handle_help_command(self, context: CommandContext) -> str:
        if context.stripped == "/help":
            return self._help_response()
        parts = context.stripped.split(maxsplit=1)
        if len(parts) != 2:
            return self._help_response()
        return self._help_response(parts[1])

    def _handle_status_command(self, context: CommandContext) -> str:
        return json.dumps(asdict(self.heartbeat.collect()), indent=2, sort_keys=True)

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
            parts = stripped.split(maxsplit=1)
            account = parts[1]
            try:
                drafts = self.content_engine.generate_batch(account)
                results = [self.social_publisher.publish(d) for d in drafts]
                return json.dumps([{"platform": r.platform, "post_id": r.post_id, "url": r.url} for r in results], indent=2)
            except Exception:
                logger.exception("social_publish error for %s", account)
                return "error publishing — check logs"
        return f"usage: {stripped} <argument>"

    def _help_response(self, topic: str | None = None) -> str:
        return _help_response(topic)

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
        try:
            response = self.brain.handle_message(
                session_id,
                prompt_text,
                memory_text=source_text,
                task_type="telegram_message",
            )
        except ApprovalPending as exc:
            return _format_approval_pending(exc)

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
            self._browse_handler._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Bot returned fallback for message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome="failure",
                error_snippet=(raw_content or "empty_response")[:500],
                lesson="When the brain returns empty output, ask a clarifying question and inspect prompt/context assembly.",
                predicted_confidence=self.brain._last_confidence or None,
            )
        else:
            self._browse_handler._record_learning_outcome(
                task_type="telegram_message",
                session_id=session_id,
                description=f"Handled message: {source_text[:200]}",
                approach="brain.handle_message",
                outcome="success",
                lesson="The brain produced a usable reply for this conversational request.",
                predicted_confidence=self.brain._last_confidence or None,
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
        try:
            return self.brain.handle_message(
                session_id,
                content_blocks,
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
        return json.dumps(result, indent=2, sort_keys=True)

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
        return json.dumps({"job_id": job_id, "event_count": len(replay), "events": replay}, indent=2, sort_keys=True)



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
        if current.get("autonomy_mode") == "autonomous":
            return
        active_object["autonomy_configured"] = {
            "mode": "autonomous",
            "source": "telegram_default",
        }
        self.brain.memory.update_session_state(
            session_id,
            autonomy_mode="autonomous",
            step_budget=_default_step_budget("autonomous"),
            active_object=active_object,
        )

    def _remember_user_turn_state(self, session_id: str, text: str) -> None:
        self._state_handler.remember_user_turn_state(session_id, text)

    def _remember_assistant_turn_state(self, session_id: str, user_text: str, reply_text: str) -> None:
        self._state_handler.remember_assistant_turn_state(session_id, user_text, reply_text)

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

    def _maybe_handle_task_status_question(self, text: str, *, session_id: str) -> str | None:
        if not _looks_like_task_completion_question(text):
            return None
        return self._task_status_question_response(session_id)

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
