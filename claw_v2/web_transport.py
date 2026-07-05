from __future__ import annotations

import asyncio
import errno
import json
import logging
import mimetypes
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from claw_v2.chat_api import LocalChatAPI
from claw_v2.observability_dashboard import ObservabilityDashboard

logger = logging.getLogger(__name__)


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


def _static_root() -> Path:
    return _static_dir().resolve()


def _content_type_for(path: str) -> str:
    # Explicit overrides for text assets where we want a charset; mimetypes
    # falls back to the stdlib database for everything else (images, fonts,
    # etc.) so static assets get the right MIME instead of text/html.
    if path.endswith(".html") or path.endswith(".htm"):
        return "text/html; charset=utf-8"
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "application/octet-stream"


def _decode_path_info(path_info: str) -> str:
    value = path_info or "/"
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _resolve_static_path(path_info: str) -> Path | None:
    decoded = _decode_path_info(path_info)
    relative = decoded.lstrip("/") or "chat.html"
    root = _static_root()
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _normalize_hostname(hostname: str | None) -> str | None:
    value = (hostname or "").strip().lower().rstrip(".")
    return value or None


def _allowed_hostnames(bound_host: str) -> set[str]:
    hostnames = {"127.0.0.1", "localhost", "::1"}
    normalized = _normalize_hostname(bound_host)
    if normalized and normalized not in {"0.0.0.0", "::"}:
        hostnames.add(normalized)
    return hostnames


def _split_host_header(value: str | None) -> tuple[str, int | None] | None:
    raw = (value or "").strip()
    if not raw or "," in raw:
        return None
    if raw.startswith("["):
        end = raw.find("]")
        if end <= 1:
            return None
        hostname = _normalize_hostname(raw[1:end])
        rest = raw[end + 1 :]
        if rest.startswith(":"):
            port_raw = rest[1:]
        elif rest:
            return None
        else:
            port_raw = ""
    else:
        if raw.count(":") > 1:
            return None
        hostname_raw, separator, port_raw = raw.partition(":")
        hostname = _normalize_hostname(hostname_raw)
        if not separator:
            port_raw = ""
    if hostname is None:
        return None
    if not port_raw:
        return hostname, None
    if not port_raw.isdigit():
        return None
    port = int(port_raw)
    if port < 1 or port > 65535:
        return None
    return hostname, port


def _host_header_allowed(value: str | None, *, bound_host: str, bound_port: int) -> bool:
    parsed = _split_host_header(value)
    if parsed is None:
        return False
    hostname, port = parsed
    if hostname not in _allowed_hostnames(bound_host):
        return False
    if port is None:
        return bound_port in {80, 443}
    return port == bound_port


def _origin_header_allowed(value: str | None, *, bound_host: str, bound_port: int) -> bool:
    raw = (value or "").strip()
    if not raw:
        return True
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = _normalize_hostname(parsed.hostname)
    if hostname not in _allowed_hostnames(bound_host):
        return False
    effective_port = port if port is not None else (443 if parsed.scheme == "https" else 80)
    return effective_port == bound_port


def _json_response(
    start_response: Callable[[str, list[tuple[str, str]]], object],
    status: str,
    payload: dict[str, Any],
) -> list[bytes]:
    body = json.dumps(payload).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


class _QuietHandler(WSGIRequestHandler):
    def log_message(
        self, format: str, *args: object
    ) -> None:  # pragma: no cover - suppress stdlib noise
        return


class _ReusableWSGIServer(WSGIServer):
    allow_reuse_address = True


class WebTransport:
    def __init__(
        self,
        *,
        chat_api: LocalChatAPI,
        observability_dashboard: ObservabilityDashboard | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self._chat_api = chat_api
        self._observability_dashboard = observability_dashboard
        self._host = host
        self._port = port
        self._server: WSGIServer | None = None
        self._thread: threading.Thread | None = None
        self._started_at: float | None = None

    def is_serving(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._server is None:
            return self._port
        return int(self._server.server_port)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def start(self) -> None:
        if self._server is not None:
            return

        def _app(
            environ: dict[str, Any], start_response: Callable[[str, list[tuple[str, str]]], object]
        ) -> list[bytes]:
            path = str(environ.get("PATH_INFO", "/"))
            if path == "/health":
                now = time.time()
                body = json.dumps(
                    {
                        "status": "ok",
                        "pid": os.getpid(),
                        "ts": now,
                        "uptime_s": (now - self._started_at) if self._started_at else None,
                        "port": self.port,
                    }
                ).encode("utf-8")
                start_response(
                    "200 OK",
                    [
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body))),
                    ],
                )
                return [body]
            headers = LocalChatAPI._headers_from_environ(environ)
            if path == "/observability" or path.startswith("/observability/"):
                if not self._chat_api.is_authorized(headers):
                    return _json_response(
                        start_response, "401 Unauthorized", {"error": "unauthorized"}
                    )
                if self._observability_dashboard is None:
                    return _json_response(
                        start_response,
                        "503 Service Unavailable",
                        {"error": "observability unavailable"},
                    )
                return self._observability_dashboard.wsgi_app(environ, start_response)
            if path == "/api/chat":
                if not _host_header_allowed(
                    headers.get("Host"), bound_host=self.host, bound_port=self.port
                ):
                    return _json_response(start_response, "403 Forbidden", {"error": "forbidden"})
                if not _origin_header_allowed(
                    headers.get("Origin"), bound_host=self.host, bound_port=self.port
                ):
                    return _json_response(start_response, "403 Forbidden", {"error": "forbidden"})
            if path.startswith("/api/"):
                return self._chat_api.wsgi_app(environ, start_response)
            file_path = _resolve_static_path(path)
            if file_path is None:
                return _json_response(
                    start_response, "404 Not Found", {"error": f"not found: {path}"}
                )
            body = file_path.read_bytes()
            start_response(
                "200 OK",
                [
                    ("Content-Type", _content_type_for(file_path.name)),
                    ("Content-Length", str(len(body))),
                ],
            )
            return [body]

        last_error: OSError | None = None
        for attempt in range(5):
            try:
                self._server = make_server(
                    self._host,
                    self._port,
                    _app,
                    server_class=_ReusableWSGIServer,
                    handler_class=_QuietHandler,
                )
                break
            except OSError as exc:
                last_error = exc
                if exc.errno != errno.EADDRINUSE or self._port == 0:
                    raise
                await asyncio.sleep(0.25 * (attempt + 1))
        if self._server is None:
            raise OSError(
                f"Port {self._host}:{self._port} is still in use after retries"
            ) from last_error
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._started_at = time.time()
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                logger.warning("Web transport thread did not stop within timeout")
        self._server = None
        self._thread = None
        self._started_at = None
