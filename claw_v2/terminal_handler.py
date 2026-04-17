from __future__ import annotations

import json
from typing import Any

from claw_v2.bot_commands import BotCommand, CommandContext
from claw_v2.bot_helpers import _parse_non_negative_int


class TerminalHandler:
    def __init__(self, terminal_bridge: Any | None = None) -> None:
        self.terminal_bridge = terminal_bridge

    def commands(self) -> list[BotCommand]:
        return [
            BotCommand(
                "terminal",
                self.handle_command,
                exact=(
                    "/terminal_list",
                    "/terminal_open",
                    "/terminal_status",
                    "/terminal_read",
                    "/terminal_send",
                    "/terminal_close",
                ),
                prefixes=(
                    "/terminal_open ",
                    "/terminal_status ",
                    "/terminal_read ",
                    "/terminal_send ",
                    "/terminal_close ",
                ),
            ),
        ]

    def handle_command(self, context: CommandContext) -> str:
        stripped = context.stripped
        if stripped == "/terminal_list":
            return self._list_response()
        if stripped == "/terminal_open":
            return "usage: /terminal_open <claude|codex> [cwd]"
        if stripped.startswith("/terminal_open "):
            parts = stripped.split(maxsplit=2)
            if len(parts) not in {2, 3}:
                return "usage: /terminal_open <claude|codex> [cwd]"
            cwd = parts[2] if len(parts) == 3 else None
            return self._open_response(parts[1], cwd=cwd)
        if stripped == "/terminal_status":
            return "usage: /terminal_status <session_id>"
        if stripped.startswith("/terminal_status "):
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                return "usage: /terminal_status <session_id>"
            return self._status_response(parts[1])
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
            return self._read_response(parts[1], offset=offset)
        if stripped == "/terminal_send":
            return "usage: /terminal_send <session_id> <text>"
        if stripped.startswith("/terminal_send "):
            parts = stripped.split(maxsplit=2)
            if len(parts) != 3:
                return "usage: /terminal_send <session_id> <text>"
            return self._send_response(parts[1], parts[2])
        if stripped == "/terminal_close":
            return "usage: /terminal_close <session_id>"
        parts = stripped.split(maxsplit=1)
        if len(parts) != 2:
            return "usage: /terminal_close <session_id>"
        return self._close_response(parts[1])

    def _list_response(self) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            sessions = self.terminal_bridge.list_sessions()
        except Exception as exc:
            return f"terminal list error: {exc}"
        return json.dumps({"sessions": sessions}, indent=2, sort_keys=True)

    def _open_response(self, tool: str, *, cwd: str | None) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.open(tool, cwd=cwd)
        except Exception as exc:
            return f"terminal open error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _status_response(self, session_id: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.status(session_id)
        except Exception as exc:
            return f"terminal status error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _read_response(self, session_id: str, *, offset: int) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.read(session_id, offset=offset, limit=3000)
        except Exception as exc:
            return f"terminal read error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _send_response(self, session_id: str, text: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.send(session_id, text)
        except Exception as exc:
            return f"terminal send error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)

    def _close_response(self, session_id: str) -> str:
        if self.terminal_bridge is None:
            return "terminal bridge unavailable"
        try:
            result = self.terminal_bridge.close(session_id)
        except Exception as exc:
            return f"terminal close error: {exc}"
        return json.dumps(result, indent=2, sort_keys=True)
