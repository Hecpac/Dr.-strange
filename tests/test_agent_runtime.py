from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.agent_runtime import AgentRuntime, ChannelRoute
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream


class _StubBotService:
    allowed_user_id = "123"

    def handle_text(self, *, user_id: str, session_id: str, text: str) -> str:
        return f"text:{user_id}:{session_id}:{text}"

    def handle_multimodal(
        self,
        *,
        user_id: str,
        session_id: str,
        content_blocks: list[dict],
        memory_text: str,
    ) -> str:
        return f"multi:{user_id}:{session_id}:{len(content_blocks)}:{memory_text}"


class AgentRuntimeTests(unittest.TestCase):
    def test_routes_telegram_text_through_agent_session_and_records_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            memory = MemoryStore(root / "claw.db")
            observe = ObserveStream(root / "observe.db")
            runtime = AgentRuntime(bot_service=_StubBotService(), memory=memory, observe=observe)

            response = runtime.handle_text(
                channel="telegram",
                external_user_id="123",
                external_session_id="456",
                text="hola",
            )

            self.assertEqual(response.session_id, "tg-456")
            self.assertEqual(response.text, "text:123:tg-456:hola")
            state = memory.get_session_state("tg-456")
            route = state["active_object"]["last_channel_route"]
            self.assertEqual(route["channel"], "telegram")
            self.assertEqual(route["external_session_id"], "456")
            self.assertEqual(route["resolved_session_id"], "tg-456")
            events = observe.recent_events(limit=5)
            self.assertEqual(events[0]["event_type"], "agent_response_ready")
            self.assertTrue(any(event["event_type"] == "agent_event_received" for event in events))

    def test_preserves_explicit_web_session_id(self) -> None:
        runtime = AgentRuntime(bot_service=_StubBotService())

        response = runtime.handle_text(
            channel="web",
            external_user_id="local-user",
            external_session_id="mac-main",
            session_id="mac-main",
            text="hola",
        )

        self.assertEqual(response.session_id, "mac-main")
        self.assertEqual(response.text, "text:local-user:mac-main:hola")

    def test_multimodal_events_use_same_route_contract(self) -> None:
        runtime = AgentRuntime(bot_service=_StubBotService())

        response = runtime.handle_multimodal(
            channel="telegram",
            external_user_id="123",
            external_session_id="789",
            content_blocks=[{"type": "text", "text": "analiza"}],
            memory_text="[Imagen adjunta]",
        )

        self.assertEqual(response.session_id, "tg-789")
        self.assertEqual(response.text, "multi:123:tg-789:1:[Imagen adjunta]")

    def test_unknown_channels_get_namespaced_session_ids(self) -> None:
        runtime = AgentRuntime(bot_service=_StubBotService())

        session_id = runtime.resolve_session_id(
            ChannelRoute(channel="discord", external_session_id="abc", external_user_id="u1")
        )

        self.assertEqual(session_id, "discord-abc")


if __name__ == "__main__":
    unittest.main()
