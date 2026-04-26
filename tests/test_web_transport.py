from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.request import Request, urlopen

from claw_v2.bot import BotService
from claw_v2.chat_api import LocalChatAPI
from claw_v2.web_transport import WebTransport


class _StubBotService:
    allowed_user_id = "123"

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        return f"reply:{user_id}:{session_id}:{text}"


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
