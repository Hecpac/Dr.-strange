"""P0-B: per-turn correlator (``turn_id``) shared across layers.

A Telegram turn fans out to many subsystems — dispatch, brain, tool
calls, task ledger, approvals, learning loop, observe events — that
today only correlate by wall-clock timestamps. ``turn_id_context`` lets
the entry point set a single opaque identifier that every downstream
layer can pick up via ``current_turn_id()`` and stamp on its persisted
artifacts.

Lives in its own module so heavyweight layers (observe, task_ledger,
approval) can import the ContextVar without pulling in ``bot_helpers``
and creating cycles.
"""

from __future__ import annotations

import contextlib
import secrets
from contextvars import ContextVar
from typing import Iterator

__all__ = [
    "current_turn_id",
    "new_turn_id",
    "turn_id_context",
    "CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID",
]


_TURN_ID_CONTEXT: ContextVar[str | None] = ContextVar(
    "claw_turn_id_context", default=None
)


CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID: frozenset[str] = frozenset(
    {
        "brain_turn_started",
        "brain_turn_completed",
        "brain_tooluse_ledger_started",
        "brain_tooluse_ledger_needs_verification",
        "brain_tooluse_ledger_completed_with_warnings",
        "brain_tooluse_ledger_failed",
        "dispatch_decision",
        # Reactivated once ``BotService.handle_text`` opens a
        # ``turn_id_context`` for every Telegram turn. Daemon-side
        # task creations (Kairos, heartbeat, recovery) still emit a
        # ``turn_id_missing`` sibling — that surfaces a real gap rather
        # than noise, because those paths should eventually carry a
        # correlator too.
        "task_ledger_created",
        "approval_pending",
    }
)
"""Events whose receipts depend on turn_id correlation. Emitting one
outside a turn_id context triggers a sibling ``turn_id_missing`` event
so the gap is visible instead of silent.
"""


def new_turn_id() -> str:
    """Return an opaque, URL-safe turn identifier (~16 hex chars)."""
    return secrets.token_hex(8)


@contextlib.contextmanager
def turn_id_context(turn_id: str) -> Iterator[None]:
    """Stamp the current async/sync stack with ``turn_id``.

    Downstream layers call :func:`current_turn_id` to pick up the value
    and merge it into payloads, metadata, and artifacts they persist.
    """
    token = _TURN_ID_CONTEXT.set(turn_id)
    try:
        yield
    finally:
        _TURN_ID_CONTEXT.reset(token)


def current_turn_id() -> str | None:
    """Return the active turn_id or None when no context is open."""
    return _TURN_ID_CONTEXT.get()
