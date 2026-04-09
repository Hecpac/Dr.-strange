from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.chat_api import LocalChatAPI
from claw_v2.observe import ObserveStream
from tests.helpers import make_config


class LocalChatAPITests(unittest.TestCase):
    def test_post_chat_routes_to_bot_service(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        bot_service.handle_text.return_value = "reply text"
        api = LocalChatAPI(bot_service=bot_service)

        status_code, headers, body = api.handle_http(
            method="POST",
            path="/api/chat",
            body=json.dumps({"session_id": "mac-main", "text": "hola"}).encode("utf-8"),
        )

        self.assertEqual(status_code, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["reply"], "reply text")
        self.assertEqual(payload["session_id"], "mac-main")
        self.assertIsNone(payload["trace_id"])
        bot_service.handle_text.assert_called_once_with(
            user_id="123",
            session_id="mac-main",
            text="hola",
        )

    def test_post_chat_uses_local_default_user_when_bot_has_no_allowed_user(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = None
        bot_service.handle_text.return_value = "reply text"
        api = LocalChatAPI(bot_service=bot_service)

        api.handle_http(
            method="POST",
            path="/api/chat",
            body=json.dumps({"session_id": "mac-main", "text": "hola"}).encode("utf-8"),
        )

        bot_service.handle_text.assert_called_once_with(
            user_id="local-user",
            session_id="mac-main",
            text="hola",
        )

    def test_post_chat_rejects_invalid_json(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        api = LocalChatAPI(bot_service=bot_service)

        status_code, _, body = api.handle_http(
            method="POST",
            path="/api/chat",
            body=b"{not-json",
        )

        self.assertEqual(status_code, 400)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["error"], "request body must be valid JSON")
        bot_service.handle_text.assert_not_called()

    def test_post_chat_rejects_missing_fields(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        api = LocalChatAPI(bot_service=bot_service)

        status_code, _, body = api.handle_http(
            method="POST",
            path="/api/chat",
            body=json.dumps({"session_id": "", "text": ""}).encode("utf-8"),
        )

        self.assertEqual(status_code, 400)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["error"], "session_id must be a non-empty string")

    def test_unknown_route_returns_404(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        api = LocalChatAPI(bot_service=bot_service)

        status_code, _, body = api.handle_http(method="GET", path="/api/nope", body=b"")

        self.assertEqual(status_code, 404)
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("not found", payload["error"])

    def test_wrong_method_returns_405(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        api = LocalChatAPI(bot_service=bot_service)

        status_code, _, body = api.handle_http(method="GET", path="/api/chat", body=b"")

        self.assertEqual(status_code, 405)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["allowed"], ["POST"])

    def test_wsgi_app_wraps_http_handler(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        bot_service.handle_text.return_value = "reply text"
        api = LocalChatAPI(bot_service=bot_service)
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/chat",
            "CONTENT_LENGTH": "43",
            "wsgi.input": io.BytesIO(b'{"session_id":"mac-main","text":"hola"}'),
        }
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = headers

        chunks = api.wsgi_app(environ, start_response)

        self.assertEqual(captured["status"], "200 OK")
        body = b"".join(chunks)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["reply"], "reply text")

    def test_get_traces_returns_recent_trace_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="claude", trace_id="trace-1")
            observe.emit("brain_turn_complete", lane="brain", provider="anthropic", model="claude", trace_id="trace-1")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="claude", trace_id="trace-2")
            bot_service = MagicMock()
            bot_service.allowed_user_id = "123"
            api = LocalChatAPI(bot_service=bot_service, observe=observe)

            status_code, _, body = api.handle_http(method="GET", path="/api/traces?limit=2", body=b"")

            self.assertEqual(status_code, 200)
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(len(payload["traces"]), 2)
            self.assertEqual(payload["traces"][0]["trace_id"], "trace-2")
            self.assertEqual(payload["traces"][1]["trace_id"], "trace-1")

    def test_get_trace_replay_returns_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            observe.emit("brain_turn_start", lane="brain", provider="anthropic", model="claude", trace_id="trace-1")
            observe.emit("llm_response", lane="brain", provider="anthropic", model="claude", trace_id="trace-1")
            bot_service = MagicMock()
            bot_service.allowed_user_id = "123"
            api = LocalChatAPI(bot_service=bot_service, observe=observe)

            status_code, _, body = api.handle_http(method="GET", path="/api/traces/trace-1", body=b"")

            self.assertEqual(status_code, 200)
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(payload["trace_id"], "trace-1")
            self.assertEqual(payload["event_count"], 2)
            self.assertEqual(payload["events"][0]["event_type"], "brain_turn_start")

    def test_get_trace_replay_returns_503_when_observe_missing(self) -> None:
        bot_service = MagicMock()
        bot_service.allowed_user_id = "123"
        bot_service.observe = None
        api = LocalChatAPI(bot_service=bot_service)

        status_code, _, body = api.handle_http(method="GET", path="/api/traces/trace-missing", body=b"")

        self.assertEqual(status_code, 503)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["error"], "observe stream unavailable")

    def test_get_trace_replay_returns_404_when_trace_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            bot_service = MagicMock()
            bot_service.allowed_user_id = "123"
            api = LocalChatAPI(bot_service=bot_service, observe=observe)

            status_code, _, body = api.handle_http(method="GET", path="/api/traces/trace-missing", body=b"")

            self.assertEqual(status_code, 404)
            payload = json.loads(body.decode("utf-8"))
            self.assertEqual(payload["error"], "trace not found: trace-missing")


class MakeConfigCompatibilityTests(unittest.TestCase):
    def test_existing_helper_config_still_builds_runtime_compatible_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config(Path(tmpdir))
        self.assertEqual(config.telegram_allowed_user_id, "123")
