from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import _autonomy_policy_payload, _filter_task_queue_by_mode, _help_response


@dataclass(slots=True)
class CoreCommandPlugin:
    bot: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.bot, name)

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand("help", self._handle_help_command, exact=("/help",), prefixes=("/help ",)),
            BotCommand("status", self._handle_status_command, exact=("/status",)),
            BotCommand("restart", self._handle_restart_command, exact=("/restart",)),
            BotCommand("config", self._handle_config_command, exact=("/config",)),
            BotCommand("tokens", self._handle_tokens_command, exact=("/tokens",)),
            BotCommand("spending", self._handle_spending_command, exact=("/spending",)),
            BotCommand("task_run", self._handle_task_run_command, exact=("/task_run",), prefixes=("/task_run ",)),
            BotCommand("autonomy", self._handle_autonomy_command, exact=("/autonomy", "/autonomy_policy"), prefixes=("/autonomy ",)),
            BotCommand("task_state", self._handle_task_state_command, exact=("/task_loop", "/task_queue", "/task_pending", "/session_state"), prefixes=("/task_queue ",)),
            BotCommand("task_transition", self._handle_task_transition_command, exact=("/task_done", "/task_defer"), prefixes=("/task_done ", "/task_defer ")),
            BotCommand("browse", self._handle_browse_command, prefixes=("/browse ",)),
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

    def _handle_restart_command(self, context: CommandContext) -> str:
        import os
        import signal
        import threading
        from datetime import UTC, datetime

        from claw_v2 import bot as bot_module

        marker_path = bot_module.Path.home() / ".claw" / "restart_requested.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "requested_at": datetime.now(UTC).isoformat(),
                    "requested_by": context.user_id,
                    "reason": "telegram_command",
                }
            ),
            encoding="utf-8",
        )

        def _shutdown_self() -> None:
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                os._exit(0)

        threading.Timer(2.0, _shutdown_self).start()
        return "🔄 Reiniciando… vuelvo en ~5s (launchd KeepAlive)."

    def _handle_config_command(self, context: CommandContext) -> str:
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

    def _handle_tokens_command(self, context: CommandContext) -> str:
        return self._tokens_info_response(context.session_id)

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
        if context.stripped == "/task_loop":
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

    def _help_response(self, topic: str | None = None) -> str:
        return _help_response(topic)
