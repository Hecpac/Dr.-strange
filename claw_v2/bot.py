from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

from claw_v2.agents import AutoResearchAgentService
from claw_v2.agent_handler import AgentHandler
from claw_v2.browse_handler import BrowseHandler
from claw_v2.task_handler import TaskHandler
from claw_v2.approval import ApprovalManager
from claw_v2.brain import BrainService
from claw_v2.bot_commands import CommandContext, HandlerRegistry
from claw_v2.bot_core_commands import CoreCommandPlugin
from claw_v2.bot_post_commands import PostCommandPlugin
from claw_v2.bot_skill_commands import SkillCommandPlugin
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
from claw_v2.pipeline import PipelineService
from claw_v2.social import SocialPublisher
from claw_v2.bot_helpers import *  # noqa: F403


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
        self.allowed_user_id = allowed_user_id
        self.pipeline = pipeline
        self.content_engine = content_engine
        self.social_publisher = social_publisher
        self.config = config
        self._terminal_handler = TerminalHandler(terminal_bridge=terminal_bridge)
        self.observe = observe
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
            get_session_state=brain.memory.get_session_state,
            update_session_state=brain.memory.update_session_state,
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
            brain_handle_message=brain.handle_message,
        )
        self._agent_handler = AgentHandler(
            auto_research=auto_research,
            pull_requests=pull_requests,
        )
        self._core_commands = CoreCommandPlugin(self)
        self._post_commands = PostCommandPlugin(self)
        self._skill_commands = SkillCommandPlugin(self)
        self._pre_state_registry = self._build_pre_state_registry()
        self._post_shortcut_registry = self._build_post_shortcut_registry()
        self._pre_state_commands = self._pre_state_registry.commands
        self._post_shortcut_commands = self._post_shortcut_registry.commands

    @property
    def terminal_bridge(self) -> object | None:
        return self._terminal_handler.terminal_bridge

    @terminal_bridge.setter
    def terminal_bridge(self, value: object | None) -> None:
        self._terminal_handler.terminal_bridge = value

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

    def set_capability_status(self, name: str, *, available: bool, reason: str | None = None) -> None:
        self._capability_status[name] = {"available": available, "reason": reason or ""}

    def _capability_unavailable_message(self, name: str, fallback: str) -> str | None:
        status = self._capability_status.get(name)
        if status is None or status.get("available", True):
            return None
        base = _CAPABILITY_MESSAGES.get(name, fallback)
        reason = str(status.get("reason", "")).strip()
        return f"{base} {reason}".strip()

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        if self.allowed_user_id is None:
            raise PermissionError("TELEGRAM_ALLOWED_USER_ID must be configured")
        if user_id != self.allowed_user_id:
            raise PermissionError("user is not allowed to access this bot")
        stripped = text.strip()
        context = CommandContext(user_id=user_id, session_id=session_id, text=text, stripped=stripped)
        command_response = self._pre_state_registry.execute(context)
        if isinstance(command_response, _BrainShortcut):
            return self._brain_text_response(
                session_id, command_response.text, memory_text=command_response.memory_text,
            )
        if command_response is not None:
            return command_response
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
        coordinated_response = self._task_handler.maybe_run_coordinated_task(session_id, stripped)
        if coordinated_response is not None:
            self.brain.memory.store_message(session_id, "user", stripped)
            self.brain.memory.store_message(session_id, "assistant", coordinated_response[:4000])
            self._remember_assistant_turn_state(session_id, stripped, coordinated_response)
            return coordinated_response
        command_response = self._post_shortcut_registry.execute(context)
        if command_response is not None:
            return command_response

        nlm_response = self._nlm_handler.natural_language_response(session_id, stripped)
        if nlm_response is not None:
            return nlm_response

        return self._brain_text_response(session_id, stripped)

    def _build_pre_state_registry(self) -> HandlerRegistry:
        registry = HandlerRegistry("pre_state")
        registry.extend(self._core_commands.commands())
        registry.extend(self._terminal_handler.commands())
        registry.extend(self._chrome_handler.commands())
        registry.extend(self._computer_handler.commands())
        registry.extend(self._skill_commands.commands())
        registry.extend(self._wiki_handler.commands())
        registry.extend(self._design_handler.commands())
        if self._checkpoint_handler is not None:
            registry.extend(self._checkpoint_handler.commands())
        return registry

    def _build_post_shortcut_registry(self) -> HandlerRegistry:
        registry = HandlerRegistry("post_shortcut")
        registry.extend(self._agent_handler.commands())
        registry.extend(self._post_commands.commands())
        registry.extend(self._nlm_handler.commands())
        return registry

    def is_voice_mode(self, session_id: str) -> str | None:
        if not hasattr(self, "_voice_sessions"):
            return None
        return self._voice_sessions.get(session_id)

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
        return self.brain.handle_message(
            session_id,
            content_blocks,
            memory_text=memory_text,
            task_type="telegram_message",
        ).content


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
        state = self.brain.memory.update_session_state(
            session_id,
            autonomy_mode=mode,
            step_budget=_default_step_budget(mode),
        )
        return json.dumps(state, indent=2, sort_keys=True)

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
