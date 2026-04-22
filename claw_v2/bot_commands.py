from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass(slots=True)
class CommandContext:
    user_id: str
    session_id: str
    text: str
    stripped: str


CommandHandler = Callable[[CommandContext], Any]


@dataclass(slots=True)
class BotCommand:
    name: str
    handler: CommandHandler
    exact: tuple[str, ...] = field(default_factory=tuple)
    prefixes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.exact, str):
            self.exact = (self.exact,)
        if isinstance(self.prefixes, str):
            self.prefixes = (self.prefixes,)

    def matches(self, stripped: str) -> bool:
        return stripped in self.exact or any(stripped.startswith(prefix) for prefix in self.prefixes)


@dataclass(slots=True)
class HandlerRegistry:
    phase: str
    _commands: list[BotCommand] = field(default_factory=list)

    def register(self, command: BotCommand) -> None:
        self._commands.append(command)

    def extend(self, commands: Iterable[BotCommand]) -> None:
        for command in commands:
            self.register(command)

    @property
    def commands(self) -> list[BotCommand]:
        return list(self._commands)

    def can_handle(self, context: CommandContext) -> bool:
        return any(command.matches(context.stripped) for command in self._commands)

    def execute(self, context: CommandContext) -> Any | None:
        for command in self._commands:
            if command.matches(context.stripped):
                return command.handler(context)
        return None


def dispatch_commands(commands: list[BotCommand], context: CommandContext) -> Any | None:
    for command in commands:
        if command.matches(context.stripped):
            return command.handler(context)
    return None
