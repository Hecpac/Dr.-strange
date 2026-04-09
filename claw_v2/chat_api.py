from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.bot import BotService
from claw_v2.observe import ObserveStream


JsonDict = dict[str, Any]
StartResponse = Callable[[str, list[tuple[str, str]]], object]


@dataclass(slots=True)
class ChatAPIResponse:
    status_code: int
    payload: JsonDict

    def to_http(self) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(self.payload, indent=2, sort_keys=True).encode("utf-8")
        return (
            self.status_code,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            },
            body,
        )


class LocalChatAPI:
    def __init__(
        self,
        *,
        bot_service: BotService,
        default_user_id: str | None = None,
        observe: ObserveStream | None = None,
    ) -> None:
        self._bot_service = bot_service
        self._default_user_id = default_user_id or bot_service.allowed_user_id or "local-user"
        self._observe = observe or getattr(bot_service, "observe", None)

    def handle_http(self, *, method: str, path: str, body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
        response = self.dispatch(method=method, path=path, body=body)
        return response.to_http()

    def dispatch(self, *, method: str, path: str, body: bytes | None = None) -> ChatAPIResponse:
        normalized_path = path.split("?", 1)[0]
        if normalized_path == "/api/traces":
            return self._dispatch_traces(method=method, path=path)
        if normalized_path.startswith("/api/traces/"):
            trace_id = normalized_path.removeprefix("/api/traces/").strip()
            return self._dispatch_trace_replay(method=method, trace_id=trace_id)
        if normalized_path != "/api/chat":
            return ChatAPIResponse(status_code=404, payload={"error": f"not found: {normalized_path}"})
        if method.upper() != "POST":
            return ChatAPIResponse(status_code=405, payload={"error": "method not allowed", "allowed": ["POST"]})

        try:
            payload = self._decode_json(body)
        except ValueError as exc:
            return ChatAPIResponse(status_code=400, payload={"error": str(exc)})

        session_id = payload.get("session_id")
        text = payload.get("text")
        if not isinstance(session_id, str) or not session_id.strip():
            return ChatAPIResponse(status_code=400, payload={"error": "session_id must be a non-empty string"})
        if not isinstance(text, str) or not text.strip():
            return ChatAPIResponse(status_code=400, payload={"error": "text must be a non-empty string"})

        reply = self._bot_service.handle_text(
            user_id=self._default_user_id,
            session_id=session_id.strip(),
            text=text,
        )
        return ChatAPIResponse(
            status_code=200,
            payload={
                "reply": reply,
                "session_id": session_id.strip(),
                "trace_id": None,
            },
        )

    def _dispatch_traces(self, *, method: str, path: str) -> ChatAPIResponse:
        if method.upper() != "GET":
            return ChatAPIResponse(status_code=405, payload={"error": "method not allowed", "allowed": ["GET"]})
        if self._observe is None:
            return ChatAPIResponse(status_code=503, payload={"error": "observe stream unavailable"})
        limit = self._query_param_as_int(path, "limit", default=10)
        events = self._observe.recent_events(limit=max(limit * 10, 50))
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
        return ChatAPIResponse(status_code=200, payload={"traces": traces})

    def _dispatch_trace_replay(self, *, method: str, trace_id: str) -> ChatAPIResponse:
        if method.upper() != "GET":
            return ChatAPIResponse(status_code=405, payload={"error": "method not allowed", "allowed": ["GET"]})
        if not trace_id:
            return ChatAPIResponse(status_code=400, payload={"error": "trace_id must be provided"})
        if self._observe is None:
            return ChatAPIResponse(status_code=503, payload={"error": "observe stream unavailable"})
        events = self._observe.trace_events(trace_id)
        if not events:
            return ChatAPIResponse(status_code=404, payload={"error": f"trace not found: {trace_id}"})
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
        return ChatAPIResponse(
            status_code=200,
            payload={"trace_id": trace_id, "event_count": len(replay), "events": replay},
        )

    def wsgi_app(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = str(environ.get("PATH_INFO", "/"))
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            content_length = 0
        body_stream = environ.get("wsgi.input")
        body = body_stream.read(content_length) if body_stream is not None and content_length > 0 else b""
        status_code, headers, response_body = self.handle_http(method=method, path=path, body=body)
        status_line = f"{status_code} {self._reason_phrase(status_code)}"
        start_response(status_line, list(headers.items()))
        return [response_body]

    def _decode_json(self, body: bytes | None) -> JsonDict:
        raw = (body or b"").strip()
        if not raw:
            raise ValueError("request body must be valid JSON")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("request body must be valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("request body must be a JSON object")
        return decoded

    @staticmethod
    def _query_param_as_int(path: str, name: str, *, default: int) -> int:
        if "?" not in path:
            return default
        query = path.split("?", 1)[1]
        for chunk in query.split("&"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            if key != name:
                continue
            try:
                parsed = int(value)
            except ValueError:
                return default
            return parsed if parsed > 0 else default
        return default

    @staticmethod
    def _reason_phrase(status_code: int) -> str:
        mapping = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }
        return mapping.get(status_code, "OK")
