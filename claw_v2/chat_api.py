from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.bot import BotService
from claw_v2.chat_api_routes import (
    approval_detail_payload,
    approvals_payload,
    job_detail_payload,
    jobs_payload,
    trace_replay_payload,
    traces_payload,
)
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
        auth_token: str | None = None,
    ) -> None:
        self._bot_service = bot_service
        self._default_user_id = default_user_id or bot_service.allowed_user_id or "local-user"
        self._observe = observe or getattr(bot_service, "observe", None)
        self._jobs = getattr(bot_service, "job_service", None)
        self._approvals = getattr(bot_service, "approvals", None)
        self._auth_token = auth_token.strip() if isinstance(auth_token, str) and auth_token.strip() else None

    def handle_http(
        self,
        *,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        response = self.dispatch(method=method, path=path, body=body, headers=headers)
        return response.to_http()

    def dispatch(
        self,
        *,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChatAPIResponse:
        normalized_path = path.split("?", 1)[0]
        if normalized_path.startswith("/api/") and not self._is_authorized(headers):
            return ChatAPIResponse(status_code=401, payload={"error": "unauthorized"})
        if normalized_path == "/api/traces":
            return self._payload_response(method, ["GET"], *traces_payload(self._observe, path=path))
        if normalized_path.startswith("/api/traces/"):
            trace_id = normalized_path.removeprefix("/api/traces/").strip()
            return self._payload_response(method, ["GET"], *trace_replay_payload(self._observe, trace_id=trace_id))
        if normalized_path == "/api/jobs":
            return self._payload_response(method, ["GET"], *jobs_payload(self._jobs, path=path))
        if normalized_path.startswith("/api/jobs/"):
            job_id = normalized_path.removeprefix("/api/jobs/").strip()
            status, payload = job_detail_payload(self._jobs, method=method, job_id=job_id)
            return ChatAPIResponse(status_code=status, payload=payload)
        if normalized_path == "/api/approvals":
            return self._payload_response(method, ["GET"], *approvals_payload(self._approvals))
        if normalized_path.startswith("/api/approvals/"):
            parts = normalized_path.removeprefix("/api/approvals/").strip("/").split("/")
            approval_id = parts[0] if parts else ""
            action = parts[1] if len(parts) > 1 else None
            status, payload = approval_detail_payload(
                self._approvals, method=method, approval_id=approval_id, body=body, action=action,
            )
            return ChatAPIResponse(status_code=status, payload=payload)
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

    def wsgi_app(self, environ: dict[str, Any], start_response: StartResponse) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET"))
        path = str(environ.get("PATH_INFO", "/"))
        headers = self._headers_from_environ(environ)
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
        except (TypeError, ValueError):
            content_length = 0
        body_stream = environ.get("wsgi.input")
        body = body_stream.read(content_length) if body_stream is not None and content_length > 0 else b""
        status_code, response_headers, response_body = self.handle_http(
            method=method,
            path=path,
            body=body,
            headers=headers,
        )
        status_line = f"{status_code} {self._reason_phrase(status_code)}"
        start_response(status_line, list(response_headers.items()))
        return [response_body]

    def _is_authorized(self, headers: dict[str, str] | None) -> bool:
        if self._auth_token is None:
            return True
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        direct = normalized.get("x-chat-token") or normalized.get("x-web-chat-token")
        if direct == self._auth_token:
            return True
        auth = normalized.get("authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == self._auth_token:
            return True
        return False

    @staticmethod
    def _headers_from_environ(environ: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in environ.items():
            if not isinstance(value, str):
                continue
            if key.startswith("HTTP_"):
                header_name = key[5:].replace("_", "-").title()
                headers[header_name] = value
            elif key in {"CONTENT_TYPE", "CONTENT_LENGTH"}:
                header_name = key.replace("_", "-").title()
                headers[header_name] = value
        return headers

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

    def _payload_response(self, method: str, allowed: list[str], status: int, payload: JsonDict) -> ChatAPIResponse:
        if method.upper() not in allowed and status == 200:
            return ChatAPIResponse(status_code=405, payload={"error": "method not allowed", "allowed": allowed})
        return ChatAPIResponse(status_code=status, payload=payload)

    @staticmethod
    def _reason_phrase(status_code: int) -> str:
        mapping = {
            200: "OK",
            401: "Unauthorized",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }
        return mapping.get(status_code, "OK")
