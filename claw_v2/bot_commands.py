from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(slots=True)
class CommandContext:
    user_id: str
    session_id: str
    text: str
    stripped: str


CommandHandler = Callable[[CommandContext], str]


@dataclass(slots=True)
class BotCommand:
    name: str
    handler: CommandHandler
    exact: tuple[str, ...] = field(default_factory=tuple)
    prefixes: tuple[str, ...] = field(default_factory=tuple)

    def matches(self, stripped: str) -> bool:
        return stripped in self.exact or any(stripped.startswith(prefix) for prefix in self.prefixes)


def dispatch_commands(commands: list[BotCommand], context: CommandContext) -> str | None:
    for command in commands:
        if command.matches(context.stripped):
            return command.handler(context)
    return None
