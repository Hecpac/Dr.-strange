from __future__ import annotations

import asyncio
import base64
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_app.updater.start_polling.assert_awaited_once()
        await transport.stop()


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

        update.message.reply_text.assert_awaited_once_with(
            "El runtime de Claude falló al iniciar esta solicitud. Intenta de nuevo en unos segundos."
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
        transport = TelegramTransport(
            bot_service=bot_service, token="t", allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.caption = "revisa esta foto"
        update.message.photo = [
            MagicMock(file_id="small", file_unique_id="uniq1"),
            MagicMock(file_id="large", file_unique_id="uniq1"),
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

        update.message.reply_text.assert_awaited_once_with("image response")
        _, kwargs = mock_to_thread.await_args
        self.assertEqual(kwargs["user_id"], "123")
        self.assertEqual(kwargs["session_id"], "tg-1")
        self.assertEqual(kwargs["memory_text"], "[Imagen adjunta]\nrevisa esta foto")
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["text"], "revisa esta foto")
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), b"\x89PNG\r\n\x1a\n")

    async def test_image_document_without_caption_uses_default_prompt(self) -> None:
        bot_service = MagicMock()
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

        update.message.reply_text.assert_awaited_once_with("doc response")
        _, kwargs = mock_to_thread.await_args
        self.assertEqual(kwargs["memory_text"], "[Imagen adjunta]")
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("Telegram", blocks[0]["text"])
        self.assertEqual(blocks[1]["source"]["media_type"], "image/jpeg")


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
