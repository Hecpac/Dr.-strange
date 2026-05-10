"""Pre-brain dispatch route registry.

Generalizes the slash-command BotCommand contract to the 15 semantic
handlers in BotService.handle_text. Every handler returns a RouteOutcome
that says either "I captured this turn — here is the response" or
"fall through, try the next route". A common dispatch loop emits
dispatch_decision per visited route, so audit-trail is uniform across
slash commands and semantic handlers (INTERNAL_WIRING.md §5.1).

Migration is incremental: handlers are registered as Routes one at a
time. Until a handler is migrated, the legacy if/early-return chain in
handle_text continues to coexist with the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal


@dataclass(slots=True)
class RouteContext:
    """Carries everything a route handler needs to decide."""

    user_id: str
    session_id: str
    text: str
    stripped: str
    runtime_channel: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RouteOutcome:
    """Result of running a single route's handler.

    `route` is the audit verb: "intercepted" means this handler owns the
    turn and `response` is the user-facing text; "fall_through" means
    keep searching. `reason` and `captured` enrich dispatch_decision
    payloads. `extra` is a free-form bag for handler-specific telemetry
    that should land on the dispatch event.
    """

    route: Literal["intercepted", "fall_through"]
    response: str | None = None
    reason: str = ""
    captured: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def fall_through(cls, reason: str = "") -> "RouteOutcome":
        return cls(route="fall_through", reason=reason)

    @classmethod
    def intercepted(
        cls,
        response: str,
        *,
        reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> "RouteOutcome":
        return cls(
            route="intercepted",
            response=response,
            reason=reason,
            captured=True,
            extra=extra or {},
        )


RouteHandler = Callable[[RouteContext], RouteOutcome]


@dataclass(slots=True)
class Route:
    """A named, addressable dispatch handler."""

    name: str
    handler: RouteHandler


DecisionCallback = Callable[[str, RouteOutcome, RouteContext], None]


def dispatch_routes(
    routes: Iterable[Route],
    context: RouteContext,
    *,
    on_decision: DecisionCallback | None = None,
) -> RouteOutcome:
    """Run handlers in order until one intercepts. Emit a decision per
    visited handler so audit-trail covers fall-throughs too — that is
    how `claw think tail --type dispatch_decision` reconstructs why a
    message ended up at the brain.
    """
    for route in routes:
        outcome = route.handler(context)
        if on_decision is not None:
            on_decision(route.name, outcome, context)
        if outcome.route == "intercepted":
            return outcome
    return RouteOutcome.fall_through(reason="no_route_matched")
