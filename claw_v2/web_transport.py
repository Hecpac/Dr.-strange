from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from claw_v2.chat_api import LocalChatAPI
from claw_v2.observability_dashboard import ObservabilityDashboard

logger = logging.getLogger(__name__)


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


def _static_root() -> Path:
    return _static_dir().resolve()


def _content_type_for(path: str) -> str:
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    return "text/html; charset=utf-8"


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
    def log_message(self, format: str, *args: object) -> None:  # pragma: no cover - suppress stdlib noise
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
        return (
            self._server is not None
            and self._thread is not None
            and self._thread.is_alive()
        )

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

        def _app(environ: dict[str, Any], start_response: Callable[[str, list[tuple[str, str]]], object]) -> list[bytes]:
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
            if path == "/observability" or path.startswith("/observability/"):
                if not self._chat_api.is_authorized(
                    LocalChatAPI._headers_from_environ(environ)
                ):
                    return _json_response(start_response, "401 Unauthorized", {"error": "unauthorized"})
                if self._observability_dashboard is None:
                    return _json_response(
                        start_response,
                        "503 Service Unavailable",
                        {"error": "observability unavailable"},
                    )
                return self._observability_dashboard.wsgi_app(environ, start_response)
            if path.startswith("/api/"):
                return self._chat_api.wsgi_app(environ, start_response)
            file_path = _resolve_static_path(path)
            if file_path is None:
                return _json_response(start_response, "404 Not Found", {"error": f"not found: {path}"})
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
            raise OSError(f"Port {self._host}:{self._port} is still in use after retries") from last_error
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
