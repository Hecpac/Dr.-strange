from __future__ import annotations

import http.client
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

from claw_v2 import web_transport as web_transport_module
from claw_v2.bot import BotService
from claw_v2.chat_api import LocalChatAPI
from claw_v2.observation_window import ObservationWindowState
from claw_v2.observability_dashboard import ObservabilityDashboard
from claw_v2.web_transport import WebTransport


class _StubBotService:
    allowed_user_id = "123"

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        return f"reply:{user_id}:{session_id}:{text}"


def _http_status(port: int, path: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


class WebTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_api_uses_agent_runtime_when_available(self) -> None:
        agent_runtime = MagicMock()
        agent_runtime.handle_text.return_value = SimpleNamespace(
            text="runtime reply",
            session_id="mac-main",
        )
        api = LocalChatAPI(bot_service=_StubBotService(), agent_runtime=agent_runtime)

        response = api.dispatch(
            method="POST",
            path="/api/chat",
            body=json.dumps({"session_id": "mac-main", "text": "hola"}).encode("utf-8"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.payload["reply"], "runtime reply")
        agent_runtime.handle_text.assert_called_once_with(
            channel="web",
            external_user_id="123",
            external_session_id="mac-main",
            session_id="mac-main",
            text="hola",
        )

    async def test_serves_chat_ui_and_api(self) -> None:
        transport = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService()),
            host="127.0.0.1",
            port=0,
        )
        await transport.start()
        try:
            with urlopen(f"{transport.base_url}/") as response:
                html = response.read().decode("utf-8")
            self.assertIn("Dr. Strange", html)

            request = Request(
                f"{transport.base_url}/api/chat",
                method="POST",
                data=json.dumps({"session_id": "mac-main", "text": "hola"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["reply"], "reply:123:mac-main:hola")
        finally:
            await transport.stop()

    async def test_serves_health_endpoint(self) -> None:
        transport = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService()),
            host="127.0.0.1",
            port=0,
        )
        await transport.start()
        try:
            with urlopen(f"{transport.base_url}/health") as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["port"], transport.port)
            self.assertIsInstance(payload["pid"], int)
            self.assertIsInstance(payload["ts"], float)
            self.assertIsInstance(payload["uptime_s"], float)
            self.assertTrue(transport.is_serving())
        finally:
            await transport.stop()
        self.assertFalse(transport.is_serving())

    async def test_static_server_rejects_path_traversal(self) -> None:
        transport = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService()),
            host="127.0.0.1",
            port=0,
        )
        await transport.start()
        try:
            self.assertEqual(_http_status(transport.port, "/chat.js"), 200)
            for path in (
                "/../config.py",
                "/%2e%2e/config.py",
                "/..%2fconfig.py",
                "/%252e%252e/config.py",
            ):
                with self.subTest(path=path):
                    self.assertIn(_http_status(transport.port, path), {403, 404})
        finally:
            await transport.stop()

    def test_static_server_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            static_root = root / "static"
            static_root.mkdir()
            outside = root / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            link = static_root / "leak.txt"
            try:
                link.symlink_to(outside)
            except OSError as exc:  # pragma: no cover - platform dependent
                self.skipTest(f"symlinks unavailable: {exc}")

            with patch("claw_v2.web_transport._static_root", return_value=static_root.resolve()):
                self.assertIsNone(web_transport_module._resolve_static_path("/leak.txt"))

    async def test_serves_observability_dashboard_and_freeze_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            window = ObservationWindowState(state_path=Path(tmpdir) / "window.json")
            transport = WebTransport(
                chat_api=LocalChatAPI(bot_service=_StubBotService()),
                observability_dashboard=ObservabilityDashboard(window),
                host="127.0.0.1",
                port=0,
            )
            await transport.start()
            try:
                with urlopen(f"{transport.base_url}/observability") as response:
                    html = response.read().decode("utf-8")
                self.assertIn("Claw Observability", html)

                request = Request(f"{transport.base_url}/observability/freeze", method="POST")
                with urlopen(request) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["frozen"])
                self.assertTrue(window.frozen)
            finally:
                await transport.stop()

    async def test_observability_requires_token_when_chat_api_is_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            window = ObservationWindowState(state_path=Path(tmpdir) / "window.json")
            transport = WebTransport(
                chat_api=LocalChatAPI(bot_service=_StubBotService(), auth_token="secret-token"),
                observability_dashboard=ObservabilityDashboard(window),
                host="127.0.0.1",
                port=0,
            )
            await transport.start()
            try:
                self.assertEqual(_http_status(transport.port, "/observability/state"), 401)
                self.assertEqual(_http_status(transport.port, "/observability/freeze", method="POST"), 401)
                self.assertFalse(window.frozen)
                self.assertEqual(
                    _http_status(
                        transport.port,
                        "/observability/freeze",
                        method="POST",
                        headers={"X-Chat-Token": "secret-token"},
                    ),
                    200,
                )
                self.assertTrue(window.frozen)
            finally:
                await transport.stop()

    async def test_serves_traces_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            from claw_v2.observe import ObserveStream

            class _ObservedStubBotService(_StubBotService):
                def __init__(self) -> None:
                    self.allowed_user_id = "123"
                    self.observe = ObserveStream(Path(tmpdir) / "observe.db")
                    self.observe.emit("llm_response", lane="brain", provider="anthropic", model="claude", trace_id="trace-1")

            transport = WebTransport(
                chat_api=LocalChatAPI(bot_service=_ObservedStubBotService()),
                host="127.0.0.1",
                port=0,
            )
            await transport.start()
            try:
                with urlopen(f"{transport.base_url}/api/traces?limit=5") as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["traces"][0]["trace_id"], "trace-1")
            finally:
                await transport.stop()

    async def test_stop_is_idempotent(self) -> None:
        transport = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService()),
            host="127.0.0.1",
            port=0,
        )
        await transport.start()
        await transport.stop()
        await transport.stop()

    async def test_can_restart_on_same_port_after_stop(self) -> None:
        first = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService()),
            host="127.0.0.1",
            port=0,
        )
        await first.start()
        port = first.port
        await first.stop()

        second = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService()),
            host="127.0.0.1",
            port=port,
        )
        await second.start()
        try:
            with urlopen(f"{second.base_url}/") as response:
                html = response.read().decode("utf-8")
            self.assertIn("Dr. Strange", html)
        finally:
            await second.stop()

    async def test_api_requires_token_when_chat_api_is_protected(self) -> None:
        transport = WebTransport(
            chat_api=LocalChatAPI(bot_service=_StubBotService(), auth_token="secret-token"),
            host="127.0.0.1",
            port=0,
        )
        await transport.start()
        try:
            unauthorized = Request(
                f"{transport.base_url}/api/chat",
                method="POST",
                data=json.dumps({"session_id": "mac-main", "text": "hola"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(Exception):
                urlopen(unauthorized)

            authorized = Request(
                f"{transport.base_url}/api/chat",
                method="POST",
                data=json.dumps({"session_id": "mac-main", "text": "hola"}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Chat-Token": "secret-token"},
            )
            with urlopen(authorized) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["reply"], "reply:123:mac-main:hola")
        finally:
            await transport.stop()

    async def test_start_does_not_kill_process_holding_port(self) -> None:
        async def no_sleep(_delay: float) -> None:
            return None

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = int(sock.getsockname()[1])
            transport = WebTransport(
                chat_api=LocalChatAPI(bot_service=_StubBotService()),
                host="127.0.0.1",
                port=port,
            )
            with patch("claw_v2.web_transport.os.kill") as kill_mock:
                with patch("claw_v2.web_transport.asyncio.sleep", new=no_sleep):
                    with self.assertRaises(OSError):
                        await transport.start()
                kill_mock.assert_not_called()
