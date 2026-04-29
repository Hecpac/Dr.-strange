from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from claw_v2.chat_api import LocalChatAPI

logger = logging.getLogger(__name__)


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


def _content_type_for(path: str) -> str:
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    return "text/html; charset=utf-8"


def _kill_port_holder(port: int) -> None:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=3,
        )
        for pid_str in result.stdout.strip().splitlines():
            try:
                os.kill(int(pid_str), signal.SIGTERM)
                logger.warning("Sent SIGTERM to pid %s holding port %d", pid_str.strip(), port)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


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
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> None:
        self._chat_api = chat_api
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
            if path.startswith("/api/"):
                return self._chat_api.wsgi_app(environ, start_response)
            static_path = "chat.html" if path in {"", "/"} else path.lstrip("/")
            file_path = _static_dir() / static_path
            if not file_path.exists() or not file_path.is_file():
                body = json.dumps({"error": f"not found: {path}"}).encode("utf-8")
                start_response(
                    "404 Not Found",
                    [
                        ("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(body))),
                    ],
                )
                return [body]
            body = file_path.read_bytes()
            start_response(
                "200 OK",
                [
                    ("Content-Type", _content_type_for(static_path)),
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
                if attempt == 0:
                    _kill_port_holder(self._port)
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
