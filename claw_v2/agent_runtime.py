from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelRoute:
    """External channel route for an agent session.

    The runtime owns the agent session; channels only provide routing
    coordinates so results can be delivered back to the right place.
    """

    channel: str
    external_session_id: str
    external_user_id: str
    session_id: str | None = None
    thread_id: str | None = None
    account_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.channel}:{self.external_session_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentEvent:
    route: ChannelRoute
    kind: str
    text: str | None = None
    content_blocks: list[dict[str, Any]] | None = None
    memory_text: str | None = None
    event_id: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class AgentResponse:
    session_id: str
    route: ChannelRoute
    text: str
    status: str = "ok"
    event_id: str | None = None
    duration_ms: float = 0.0


class AgentRuntime:
    """Central runtime facade for channel-agnostic agent events.

    Phase 1 keeps BotService as the execution engine, but moves channel
    dispatch, route persistence, and observability into a dedicated runtime.
    """

    def __init__(
        self,
        *,
        bot_service: Any,
        memory: Any | None = None,
        observe: Any | None = None,
    ) -> None:
        self.bot_service = bot_service
        self.memory = memory or getattr(getattr(bot_service, "brain", None), "memory", None)
        self.observe = observe or getattr(bot_service, "observe", None)

    def handle_text(
        self,
        *,
        channel: str,
        external_user_id: str,
        external_session_id: str,
        text: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentResponse:
        route = ChannelRoute(
            channel=channel,
            external_user_id=external_user_id,
            external_session_id=external_session_id,
            session_id=session_id,
            metadata=dict(metadata or {}),
        )
        return self.handle_event(AgentEvent(route=route, kind="text", text=text))

    def handle_multimodal(
        self,
        *,
        channel: str,
        external_user_id: str,
        external_session_id: str,
        content_blocks: list[dict[str, Any]],
        memory_text: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentResponse:
        route = ChannelRoute(
            channel=channel,
            external_user_id=external_user_id,
            external_session_id=external_session_id,
            session_id=session_id,
            metadata=dict(metadata or {}),
        )
        return self.handle_event(
            AgentEvent(
                route=route,
                kind="multimodal",
                content_blocks=content_blocks,
                memory_text=memory_text,
            )
        )

    def handle_event(self, event: AgentEvent) -> AgentResponse:
        session_id = self.resolve_session_id(event.route)
        started_at = time.perf_counter()
        self._remember_route(session_id, event.route)
        self._emit(
            "agent_event_received",
            {
                "event_id": event.event_id,
                "session_id": session_id,
                "kind": event.kind,
                "route": event.route.to_dict(),
            },
        )
        try:
            if event.kind == "text":
                text = self.bot_service.handle_text(
                    user_id=event.route.external_user_id,
                    session_id=session_id,
                    text=event.text or "",
                )
            elif event.kind == "multimodal":
                text = self.bot_service.handle_multimodal(
                    user_id=event.route.external_user_id,
                    session_id=session_id,
                    content_blocks=list(event.content_blocks or []),
                    memory_text=event.memory_text or "",
                )
            else:
                raise ValueError(f"unsupported agent event kind: {event.kind}")
        except Exception as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000
            self._emit(
                "agent_event_failed",
                {
                    "event_id": event.event_id,
                    "session_id": session_id,
                    "kind": event.kind,
                    "route": event.route.to_dict(),
                    "duration_ms": round(duration_ms, 1),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            raise
        duration_ms = (time.perf_counter() - started_at) * 1000
        response = AgentResponse(
            session_id=session_id,
            route=event.route,
            text=text,
            event_id=event.event_id,
            duration_ms=duration_ms,
        )
        self._emit(
            "agent_response_ready",
            {
                "event_id": event.event_id,
                "session_id": session_id,
                "kind": event.kind,
                "route": event.route.to_dict(),
                "duration_ms": round(duration_ms, 1),
                "response_length": len(text or ""),
            },
        )
        return response

    def resolve_session_id(self, route: ChannelRoute) -> str:
        if route.session_id:
            return route.session_id
        channel = route.channel.strip().lower()
        external = route.external_session_id.strip()
        if channel == "telegram":
            return f"tg-{external}"
        if channel in {"web", "local"}:
            return external
        safe_channel = channel.replace(":", "_") or "channel"
        return f"{safe_channel}-{external}"

    def _remember_route(self, session_id: str, route: ChannelRoute) -> None:
        if self.memory is None:
            return
        get_state = getattr(self.memory, "get_session_state", None)
        update_state = getattr(self.memory, "update_session_state", None)
        if not callable(get_state) or not callable(update_state):
            return
        try:
            state = get_state(session_id)
            active_object = dict(state.get("active_object") or {})
            routes = dict(active_object.get("channel_routes") or {})
            route_payload = route.to_dict()
            route_payload["resolved_session_id"] = session_id
            route_payload["last_seen_at"] = time.time()
            routes[route.key()] = route_payload
            active_object["channel_routes"] = routes
            active_object["last_channel_route"] = route_payload
            update_state(session_id, active_object=active_object)
        except Exception:
            logger.debug("Could not persist channel route for %s", session_id, exc_info=True)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        self.observe.emit(event_type, lane="agent_runtime", payload=payload)
