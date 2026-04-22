from __future__ import annotations

from claw_v2.bot_commands import BotCommand, CommandContext, HandlerRegistry


def _context(text: str) -> CommandContext:
    return CommandContext(user_id="123", session_id="s1", text=text, stripped=text)


def test_handler_registry_executes_first_matching_command() -> None:
    registry = HandlerRegistry("pre")
    registry.register(BotCommand("help", lambda context: f"handled:{context.stripped}", exact=("/help",)))

    assert registry.can_handle(_context("/help"))
    assert registry.execute(_context("/help")) == "handled:/help"
    assert registry.execute(_context("/missing")) is None


def test_bot_command_normalizes_string_prefixes() -> None:
    command = BotCommand("improve_arch", lambda context: "ok", exact="/improve_arch", prefixes="/improve_arch ")

    assert command.matches("/improve_arch")
    assert command.matches("/improve_arch repo")
    assert not command.matches("hola")
