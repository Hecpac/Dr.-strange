from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.memory import MemoryStore
from claw_v2.telegram import TelegramTransport, _split_message


class SplitMessageTests(unittest.TestCase):
    def test_short_message_unchanged(self) -> None:
        self.assertEqual(_split_message("hello"), ["hello"])

    def test_long_message_split(self) -> None:
        text = "a" * 5000
        parts = _split_message(text, max_len=4096)
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), 4096)
        self.assertEqual(len(parts[1]), 904)

    def test_empty_message(self) -> None:
        self.assertEqual(_split_message(""), [""])


class TransportStartTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_noop_without_token(self) -> None:
        transport = TelegramTransport(bot_service=MagicMock(), token=None)
        await transport.start()
        await transport.stop()

    @patch("claw_v2.telegram.ApplicationBuilder")
    async def test_start_builds_and_polls_with_token(self, mock_builder_cls) -> None:
        mock_app = AsyncMock()
        mock_app.updater = AsyncMock()
        mock_app.add_handler = MagicMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app
        mock_builder_cls.return_value = mock_builder

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        await transport.start()
        mock_builder.connection_pool_size.assert_called_once_with(32)
        mock_builder.get_updates_connection_pool_size.assert_called_once_with(8)
        mock_builder.pool_timeout.assert_called_once_with(30.0)
        mock_builder.get_updates_pool_timeout.assert_called_once_with(30.0)
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_app.updater.start_polling.assert_awaited_once()
        await transport.stop()

    async def test_stop_swallows_pool_cleanup_errors_and_emits_observe_event(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(bot_service=bot_service, token="test-token")
        app = MagicMock()
        app.updater.stop = AsyncMock(side_effect=RuntimeError("Pool timeout"))
        app.stop = AsyncMock()
        app.shutdown = AsyncMock()
        transport._app = app

        await transport.stop()

        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()
        bot_service.observe.emit.assert_called_once()
        self.assertEqual(bot_service.observe.emit.call_args.args[0], "telegram_transport_stop_error")
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["error_count"], 1)
        self.assertIn("Pool timeout", payload["errors"][0])

    async def test_set_commands_uses_curated_short_menu(self) -> None:
        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()

        await transport._set_commands()

        transport._app.bot.set_my_commands.assert_awaited_once()
        commands = transport._app.bot.set_my_commands.await_args.args[0]
        names = [command.command for command in commands]
        self.assertEqual(
            names,
            [
                "browse",
                "status",
                "approvals",
                "models",
                "model",
                "jobs",
                "pipeline_status",
                "agents",
                "screen",
                "computer",
                "terminal_list",
                "nlm_list",
                "nlm_create",
                "grill",
                "tdd",
                "improve_arch",
                "playbooks",
                "backtest",
                "effort",
                "verify",
                "focus",
                "voice",
                "design",
                "help",
            ],
        )


class HandleTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_user_silently_dropped(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(), token="t", allowed_user_id="999",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.message.reply_text = AsyncMock()
        await transport._handle_text(update, MagicMock())
        update.message.reply_text.assert_not_awaited()

    async def test_authorized_user_gets_response(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited()
        bot_service.observe.emit.assert_called_once()
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["message_kind"], "text")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response_parts"], 1)
        self.assertGreaterEqual(payload["total_ms"], 0.0)

    async def test_authorized_text_uses_agent_runtime_when_available(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        agent_runtime = MagicMock()
        agent_runtime.handle_text.return_value = SimpleNamespace(text="runtime response", session_id="tg-1")
        transport = TelegramTransport(
            bot_service=bot_service,
            agent_runtime=agent_runtime,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited()
        bot_service.handle_text.assert_not_called()
        agent_runtime.handle_text.assert_called_once_with(
            channel="telegram",
            external_user_id="123",
            external_session_id="1",
            session_id="tg-1",
            text="hello",
        )

    async def test_send_latest_image_request_bypasses_agent_and_sends_photo(self) -> None:
        db_path = Path(tempfile.mkdtemp()) / "test.db"
        memory = MemoryStore(db_path)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            image_path = Path(tmp.name)
        try:
            memory.store_message(
                "tg-1",
                "assistant",
                f"Resultado generado: `{image_path}`",
            )
            bot_service = MagicMock()
            bot_service.observe = MagicMock()
            bot_service.memory = memory
            transport = TelegramTransport(
                bot_service=bot_service, token="t", allowed_user_id="123",
            )
            transport._app = MagicMock()
            transport._app.bot = AsyncMock()
            update = MagicMock()
            update.effective_user.id = 123
            update.effective_chat.id = 1
            update.message.text = "Ponla aquí en telegram"
            update.message.reply_text = AsyncMock()
            update.message.chat.send_action = AsyncMock()

            await transport._handle_text(update, MagicMock())

            transport._app.bot.send_photo.assert_awaited_once()
            bot_service.handle_text.assert_not_called()
            self.assertEqual(update.message.reply_text.await_args.args[0], "Te la puse aquí en Telegram.")
            messages = memory.get_recent_messages("tg-1")
            self.assertEqual(messages[-2]["content"], "Ponla aquí en telegram")
            self.assertIn("Imagen enviada por Telegram", messages[-1]["content"])
        finally:
            image_path.unlink(missing_ok=True)

    async def test_claude_sdk_failures_return_specific_message(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(), token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(
                side_effect=RuntimeError("Claude SDK execution failed: Control request timeout: initialize")
            )
            await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "El runtime de Claude falló: Claude SDK execution failed: Control request timeout: initialize",
        )


class HandleVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_message_transcribed_and_handled(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response to voice"
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
            voice_api_key="test-key",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.voice.file_id = "file123"
        update.message.voice.file_unique_id = "uniq123"
        update.message.voice.file_size = 1024
        update.message.reply_text = AsyncMock()

        mock_file = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("claw_v2.telegram.transcribe", new_callable=AsyncMock, return_value="hola"):
            with patch("claw_v2.telegram.asyncio") as mock_asyncio:
                mock_asyncio.to_thread = AsyncMock(return_value="response to voice")
                await transport._handle_voice(update, mock_context)

        update.message.reply_text.assert_awaited()

    async def test_voice_without_api_key_replies_error(self) -> None:
        from claw_v2.voice import VoiceUnavailableError

        transport = TelegramTransport(
            bot_service=MagicMock(), token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.message.voice.file_id = "file123"
        update.message.voice.file_unique_id = "uniq123"
        update.message.voice.file_size = 1024
        update.message.reply_text = AsyncMock()

        mock_file = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch(
            "claw_v2.telegram.transcribe",
            new_callable=AsyncMock,
            side_effect=VoiceUnavailableError("no key"),
        ):
            await transport._handle_voice(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        call_args = update.message.reply_text.call_args[0][0]
        self.assertIn("not available", call_args.lower())


class HandleImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_message_downloaded_and_forwarded_as_multimodal(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.caption = "revisa esta foto"
        update.message.photo = [
            MagicMock(file_id="small", file_unique_id="uniq1", file_size=512),
            MagicMock(file_id="large", file_unique_id="uniq1", file_size=4096),
        ]
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        mock_file = AsyncMock()
        mock_file.file_path = "photos/test.png"

        async def download_to_drive(path: str) -> None:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

        mock_file.download_to_drive.side_effect = download_to_drive
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("claw_v2.telegram.asyncio.to_thread", new_callable=AsyncMock, return_value="image response") as mock_to_thread:
            await transport._handle_photo(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(update.message.reply_text.await_args.args[0], "image response")
        bot_service.observe.emit.assert_called_once()
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["message_kind"], "image")
        self.assertEqual(payload["response_parts"], 1)
        _, kwargs = mock_to_thread.await_args
        self.assertEqual(kwargs["user_id"], "123")
        self.assertEqual(kwargs["session_id"], "tg-1")
        self.assertIn("[Imagen adjunta] path:", kwargs["memory_text"])
        self.assertIn("revisa esta foto", kwargs["memory_text"])
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["text"], "revisa esta foto")
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), b"\x89PNG\r\n\x1a\n")

    async def test_image_document_without_caption_uses_default_prompt(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.caption = None
        update.message.document.file_id = "doc1"
        update.message.document.file_unique_id = "uniq-doc"
        update.message.document.mime_type = "image/jpeg"
        update.message.document.file_size = 2048
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        mock_file = AsyncMock()
        mock_file.file_path = "docs/test.jpg"

        async def download_to_drive(path: str) -> None:
            Path(path).write_bytes(b"\xff\xd8\xff")

        mock_file.download_to_drive.side_effect = download_to_drive
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("claw_v2.telegram.asyncio.to_thread", new_callable=AsyncMock, return_value="doc response") as mock_to_thread:
            await transport._handle_image_document(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(update.message.reply_text.await_args.args[0], "doc response")
        _, kwargs = mock_to_thread.await_args
        self.assertIn("[Imagen adjunta] path:", kwargs["memory_text"])
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("Telegram", blocks[0]["text"])
        self.assertEqual(blocks[1]["source"]["media_type"], "image/jpeg")

    async def test_handle_text_content_emits_transcript_latency(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()

        with patch("claw_v2.telegram.asyncio.to_thread", new_callable=AsyncMock, return_value="voice response"):
            await transport._handle_text_content(update, "hola")

        bot_service.observe.emit.assert_called_once()
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["message_kind"], "transcript")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response_chars"], len("voice response"))


class SendPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_screenshot_sends_photo_to_chat(self) -> None:
        bot_service = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        transport._app = MagicMock()
        mock_bot = AsyncMock()
        transport._app.bot = mock_bot

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            tmp_path = tmp.name

        try:
            await transport.send_photo(chat_id=1, photo_path=tmp_path, caption="screenshot")
            mock_bot.send_photo.assert_awaited_once()
            call_kwargs = mock_bot.send_photo.call_args
            self.assertEqual(call_kwargs.kwargs["chat_id"], 1)
            self.assertEqual(call_kwargs.kwargs["caption"], "screenshot")
        finally:
            Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
