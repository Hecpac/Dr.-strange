from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Callable
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from claw_v2.chat_api import LocalChatAPI


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


def _content_type_for(path: str) -> str:
    if path.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if path.endswith(".css"):
        return "text/css; charset=utf-8"
    return "text/html; charset=utf-8"


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # pragma: no cover - suppress stdlib noise
        return


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

        self._server = make_server(self._host, self._port, _app, handler_class=_QuietHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        await asyncio.sleep(0)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
