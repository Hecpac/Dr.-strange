from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from claw_v2.bot import BotService
from claw_v2.chat_api import LocalChatAPI
from claw_v2.web_transport import WebTransport


class _StubBotService:
    allowed_user_id = "123"

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        return f"reply:{user_id}:{session_id}:{text}"


class WebTransportTests(unittest.IsolatedAsyncioTestCase):
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
