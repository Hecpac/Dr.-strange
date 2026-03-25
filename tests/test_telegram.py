from __future__ import annotations

import asyncio
import unittest
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


if __name__ == "__main__":
    unittest.main()
