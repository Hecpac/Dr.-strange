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
from typing import Any, Iterator

__all__ = [
    "current_turn_id",
    "new_turn_id",
    "turn_id_context",
    "CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID",
    "DispatchDecisionAccumulator",
    "dispatch_decision_accumulator",
    "current_dispatch_accumulator",
]


_TURN_ID_CONTEXT: ContextVar[str | None] = ContextVar("claw_turn_id_context", default=None)


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


# ---------------------------------------------------------------------------
# F0.3c: per-turn dispatch_decision accumulator.
#
# ``BotService.handle_text`` used to emit one ``dispatch_decision`` observe
# event per pre-brain handler (~15 rows/turn, ~99% fall-through plumbing).
# Instead, each handler now *records* its decision into a turn-scoped
# accumulator and a single consolidated event is flushed once per turn.
#
# Mirrors the ``turn_id`` ContextVar pattern so concurrent Telegram turns
# each get their own list (no cross-turn bleed). Lives here, next to
# ``current_turn_id``, for the same reason: heavyweight layers can import the
# ContextVar without pulling in ``bot``/``bot_helpers`` and creating cycles.
# ---------------------------------------------------------------------------


class DispatchDecisionAccumulator:
    """Collects per-handler dispatch decisions for one turn, then flushes a
    single consolidated event. The ``flushed`` flag makes the flush
    idempotent so a ``try/finally`` backstop cannot double-emit."""

    __slots__ = ("entries", "flushed")

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.flushed: bool = False

    def record(
        self,
        *,
        handler: str | None,
        route: str,
        reason: str | None,
        captured: bool,
        matched_pattern: str | None,
    ) -> None:
        """Append one small, bounded decision entry. No prompt/system/evidence
        blobs ever land here — only the routing verdict."""
        self.entries.append(
            {
                "handler": handler,
                "route": route,
                "reason": reason,
                "captured": bool(captured),
                "matched_pattern": matched_pattern,
            }
        )


_DISPATCH_ACCUMULATOR_CONTEXT: ContextVar[DispatchDecisionAccumulator | None] = ContextVar(
    "claw_dispatch_accumulator_context", default=None
)


@contextlib.contextmanager
def dispatch_decision_accumulator() -> Iterator[DispatchDecisionAccumulator]:
    """Open a fresh per-turn dispatch accumulator and reset it on exit.

    Concurrency-safe like :func:`turn_id_context`: each turn (each async/sync
    stack) sees its own accumulator via :func:`current_dispatch_accumulator`.
    """
    acc = DispatchDecisionAccumulator()
    token = _DISPATCH_ACCUMULATOR_CONTEXT.set(acc)
    try:
        yield acc
    finally:
        _DISPATCH_ACCUMULATOR_CONTEXT.reset(token)


def current_dispatch_accumulator() -> DispatchDecisionAccumulator | None:
    """Return the active dispatch accumulator or None when no turn is open."""
    return _DISPATCH_ACCUMULATOR_CONTEXT.get()
