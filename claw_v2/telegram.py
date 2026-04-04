from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from telegram import LinkPreviewOptions, Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

from claw_v2.bot import BotService
from claw_v2.voice import VoiceUnavailableError, extract_audio, transcribe

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LEN = 4096
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
DEFAULT_IMAGE_PROMPT = "El usuario envio esta imagen por Telegram. Analizala y responde de forma util."


def _split_message(text: str, max_len: int = MAX_TELEGRAM_LEN) -> list[str]:
    if not text:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts


def _build_image_content_blocks(
    image_path: Path,
    *,
    caption: str | None,
    mime_type: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    prompt_text = caption.strip() if caption and caption.strip() else DEFAULT_IMAGE_PROMPT
    memory_text = "[Imagen adjunta]"
    if caption and caption.strip():
        memory_text = f"{memory_text}\n{caption.strip()}"
    resolved_mime_type = _resolve_image_mime_type(image_path, mime_type)
    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return (
        [
            {"type": "text", "text": prompt_text},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": resolved_mime_type,
                    "data": image_data,
                },
            },
        ],
        memory_text,
    )


def _resolve_image_mime_type(image_path: Path, declared_mime_type: str | None) -> str:
    if declared_mime_type and declared_mime_type.startswith("image/"):
        return declared_mime_type
    guessed_mime_type, _ = mimetypes.guess_type(str(image_path))
    if guessed_mime_type and guessed_mime_type.startswith("image/"):
        return guessed_mime_type
    return "image/jpeg"


def _download_suffix(file_path: str | None, mime_type: str | None) -> str:
    if file_path:
        suffix = Path(file_path).suffix
        if suffix:
            return suffix
    if mime_type:
        guessed_suffix = mimetypes.guess_extension(mime_type)
        if guessed_suffix:
            return guessed_suffix
    return ".jpg"


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
        self._rate_limits: dict[str, list[float]] = {}
        self._rate_max = 10  # max requests per window
        self._rate_window = 60.0  # seconds

    async def start(self) -> None:
        if self._token is None:
            return
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self._app.add_handler(MessageHandler(filters.Document.IMAGE, self._handle_image_document))
        self._app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        self._app.add_handler(MessageHandler(filters.AUDIO, self._handle_audio))
        self._app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, self._handle_video))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()
        await self._set_commands()
        await self._notify_startup()

    async def _notify_startup(self) -> None:
        if self._allowed_user_id is None or self._app is None:
            return
        try:
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await self._app.bot.send_message(
                chat_id=int(self._allowed_user_id),
                text=f"Claw online. {now}",
            )
        except Exception:
            logger.warning("Could not send startup notification", exc_info=True)

    async def stop(self) -> None:
        if self._app is None:
            return
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    async def _set_commands(self) -> None:
        from telegram import BotCommand
        commands = [
            BotCommand("browse", "Abrir y revisar cualquier URL — /browse <url>"),
            BotCommand("status", "Estado del sistema (heartbeat)"),
            BotCommand("approvals", "Ver aprobaciones pendientes"),
            BotCommand("pipeline_status", "Ver pipelines activos"),
            BotCommand("agents", "Listar agentes registrados"),
            BotCommand("screen", "Screenshot del escritorio actual"),
            BotCommand("computer", "Control de escritorio — /computer <instruccion>"),
            BotCommand("terminal_list", "Listar sesiones PTY de claude/codex"),
            BotCommand("nlm_list", "Listar cuadernos de NotebookLM"),
            BotCommand("nlm_create", "Crear cuaderno + Deep Research — /nlm_create <tema>"),
            BotCommand("help", "Ayuda por tema — /help [topic]"),
        ]
        try:
            await self._app.bot.set_my_commands(commands)
        except Exception:
            logger.warning("Could not set bot commands menu")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = str(update.effective_user.id)
        if self._is_rate_limited(user_id):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        session_id = f"tg-{update.effective_chat.id}"
        text = update.message.text or ""
        await update.message.chat.send_action("typing")
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_text, user_id=user_id, session_id=session_id, text=text,
            )
        except Exception as exc:
            logger.exception("Error handling message")
            err_str = str(exc)
            if "Could not process image" in err_str:
                response = "No pude procesar la imagen/screenshot. Intento sin captura visual."
                try:
                    response = await asyncio.to_thread(
                        self._bot_service.handle_text, user_id=user_id, session_id=session_id,
                        text=text + " (sin usar screenshots ni imágenes)",
                    )
                except Exception:
                    response = "Error procesando tu mensaje. Intenta de nuevo."
            elif "Claude SDK execution failed" in err_str or "Control request timeout: initialize" in err_str:
                response = "El runtime de Claude falló al iniciar esta solicitud. Intenta de nuevo en unos segundos."
            elif "API Error" in err_str or "invalid_request" in err_str:
                response = "Error con la API. Intenta de nuevo en unos segundos."
            else:
                response = "Error procesando tu mensaje. Intenta de nuevo."
        if not response or not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        for part in _split_message(response):
            await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)

    async def _handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        voice = update.message.voice
        if voice.file_size and voice.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Archivo de audio demasiado grande (máx 20 MB).")
            return
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

    async def _handle_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        audio = update.message.audio
        if audio.file_size and audio.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Archivo de audio demasiado grande (máx 20 MB).")
            return
        file = await context.bot.get_file(audio.file_id)
        suffix = Path(audio.file_name).suffix if audio.file_name else ".mp3"
        tmp_path = Path(f"/tmp/claw-audio-{audio.file_unique_id}{suffix}")
        await file.download_to_drive(str(tmp_path))
        try:
            text = await transcribe(tmp_path, api_key=self._voice_api_key)
        except VoiceUnavailableError:
            await update.message.reply_text("Voice not available — OPENAI_API_KEY not configured.")
            return
        finally:
            tmp_path.unlink(missing_ok=True)
        caption = update.message.caption or ""
        prefix = f"{caption}\n[Audio transcrito]: " if caption else "[Audio transcrito]: "
        await self._handle_text_content(update, prefix + text)

    async def _handle_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        video = update.message.video or update.message.video_note
        if video is None:
            return
        if hasattr(video, "file_size") and video.file_size and video.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Video demasiado grande (máx 20 MB).")
            return
        await update.message.chat.send_action("typing")
        file = await context.bot.get_file(video.file_id)
        tmp_video = Path(f"/tmp/claw-video-{video.file_unique_id}.mp4")
        await file.download_to_drive(str(tmp_video))
        tmp_audio: Path | None = None
        try:
            tmp_audio = await extract_audio(tmp_video)
            text = await transcribe(tmp_audio, api_key=self._voice_api_key)
        except VoiceUnavailableError:
            await update.message.reply_text("Voice not available — OPENAI_API_KEY not configured.")
            return
        except RuntimeError:
            await update.message.reply_text("No pude extraer audio del video.")
            return
        finally:
            tmp_video.unlink(missing_ok=True)
            if tmp_audio:
                tmp_audio.unlink(missing_ok=True)
        caption = update.message.caption or ""
        prefix = f"{caption}\n[Video transcrito]: " if caption else "[Video transcrito]: "
        await self._handle_text_content(update, prefix + text)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        if not update.message.photo:
            return
        photo = update.message.photo[-1]
        if hasattr(photo, "file_size") and photo.file_size and photo.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Imagen demasiado grande (máx 20 MB).")
            return
        await self._handle_image_content(
            update,
            context,
            file_id=photo.file_id,
            file_unique_id=photo.file_unique_id,
            caption=update.message.caption,
        )

    async def _handle_image_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        document = update.message.document
        if document is None:
            return
        if hasattr(document, "file_size") and document.file_size and document.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Documento demasiado grande (máx 20 MB).")
            return
        await self._handle_image_content(
            update,
            context,
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            caption=update.message.caption,
            mime_type=document.mime_type,
        )

    async def _handle_image_content(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        file_id: str,
        file_unique_id: str,
        caption: str | None,
        mime_type: str | None = None,
    ) -> None:
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        await update.message.chat.send_action("typing")
        file = await context.bot.get_file(file_id)
        tmp_path = Path(
            f"/tmp/claw-image-{file_unique_id}{_download_suffix(getattr(file, 'file_path', None), mime_type)}"
        )
        await file.download_to_drive(str(tmp_path))
        try:
            content_blocks, memory_text = _build_image_content_blocks(
                tmp_path,
                caption=caption,
                mime_type=mime_type,
            )
            response = await asyncio.to_thread(
                self._bot_service.handle_multimodal,
                user_id=user_id,
                session_id=session_id,
                content_blocks=content_blocks,
                memory_text=memory_text,
            )
        except Exception:
            logger.exception("Error handling image message")
            response = "Error procesando tu imagen. Intenta de nuevo."
        finally:
            tmp_path.unlink(missing_ok=True)
        if not response or not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        for part in _split_message(response):
            await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)

    async def send_photo(self, *, chat_id: int, photo_path: str, caption: str | None = None) -> None:
        if self._app is None:
            return
        import tempfile
        resolved = Path(photo_path).resolve()
        allowed_roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/private/tmp"), Path.home())
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            logger.error("send_photo blocked: %s is outside allowed directories", resolved)
            return
        with open(resolved, "rb") as photo:
            await self._app.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)

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
            await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)

    def _is_authorized(self, update: Update) -> bool:
        if self._allowed_user_id is None:
            return True
        return str(update.effective_user.id) == self._allowed_user_id

    def _is_rate_limited(self, user_id: str) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.get(user_id, [])
        timestamps = [t for t in timestamps if now - t < self._rate_window]
        if len(timestamps) >= self._rate_max:
            self._rate_limits[user_id] = timestamps
            return True
        timestamps.append(now)
        self._rate_limits[user_id] = timestamps
        return False
