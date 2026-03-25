from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from claw_v2.bot import BotService
from claw_v2.voice import VoiceUnavailableError, transcribe

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LEN = 4096


def _split_message(text: str, max_len: int = MAX_TELEGRAM_LEN) -> list[str]:
    if not text:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts


class TelegramTransport:
    def __init__(
        self,
        bot_service: BotService,
        token: str | None,
        allowed_user_id: str | None = None,
        voice_api_key: str | None = None,
    ) -> None:
        self._bot_service = bot_service
        self._token = token
        self._allowed_user_id = allowed_user_id
        self._voice_api_key = voice_api_key
        self._app = None

    async def start(self) -> None:
        if self._token is None:
            return
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self) -> None:
        if self._app is None:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        text = update.message.text or ""
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_text, user_id=user_id, session_id=session_id, text=text,
            )
        except Exception:
            logger.exception("Error handling message")
            response = "Error processing your message."
        if not response or not response.strip():
            response = "(sin respuesta)"
        for part in _split_message(response):
            await update.message.reply_text(part)

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        tmp_path = Path(f"/tmp/claw-voice-{voice.file_unique_id}.ogg")
        await file.download_to_drive(str(tmp_path))
        try:
            text = await transcribe(tmp_path, api_key=self._voice_api_key)
        except VoiceUnavailableError:
            await update.message.reply_text("Voice not available — OPENAI_API_KEY not configured.")
            return
        finally:
            tmp_path.unlink(missing_ok=True)
        await self._handle_text_content(update, text)

    async def _handle_text_content(self, update: Update, text: str) -> None:
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_text, user_id=user_id, session_id=session_id, text=text,
            )
        except Exception:
            logger.exception("Error handling voice message")
            response = "Error processing your voice message."
        for part in _split_message(response):
            await update.message.reply_text(part)

    def _is_authorized(self, update: Update) -> bool:
        if self._allowed_user_id is None:
            return True
        return str(update.effective_user.id) == self._allowed_user_id
