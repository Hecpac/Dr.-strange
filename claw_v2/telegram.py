from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from claw_v2.bot import BotService
from claw_v2.voice import VoiceUnavailableError, transcribe

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LEN = 4096
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

    async def start(self) -> None:
        if self._token is None:
            return
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self._app.add_handler(MessageHandler(filters.Document.IMAGE, self._handle_image_document))
        self._app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
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
            pass

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
            BotCommand("chrome_pages", "Listar tabs de Chrome con CDP"),
            BotCommand("chrome_browse", "Abrir URL en tu Chrome — /chrome_browse <url>"),
            BotCommand("chrome_shot", "Screenshot del tab actual de Chrome"),
            BotCommand("screen", "Screenshot del escritorio actual"),
            BotCommand("computer", "Control de escritorio — /computer <instruccion>"),
            BotCommand("computer_abort", "Cancelar sesión activa de Computer Use"),
            BotCommand("tokens", "Ver uso de contexto y tokens"),
            BotCommand("config", "Ver configuración de modelos LLM"),
            BotCommand("status", "Estado del sistema (heartbeat)"),
            BotCommand("agents", "Listar agentes registrados"),
            BotCommand("terminal_list", "Listar sesiones PTY de claude/codex"),
            BotCommand("terminal_open", "Abrir puente PTY — /terminal_open <claude|codex> [cwd]"),
            BotCommand("terminal_status", "Ver estado PTY — /terminal_status <session_id>"),
            BotCommand("terminal_read", "Leer salida PTY — /terminal_read <session_id> [offset]"),
            BotCommand("terminal_send", "Enviar texto a una PTY — /terminal_send <session_id> <text>"),
            BotCommand("terminal_close", "Cerrar una PTY — /terminal_close <session_id>"),
            BotCommand("action_approve", "Aprobar acción pendiente — /action_approve <id> <token>"),
            BotCommand("action_abort", "Abortar acción pendiente — /action_abort <id>"),
            BotCommand("pipeline", "Ejecutar pipeline — /pipeline <issue_id>"),
            BotCommand("pipeline_status", "Ver pipelines activos"),
            BotCommand("social_status", "Ver cuentas sociales"),
            BotCommand("social_preview", "Preview de posts — /social_preview <cuenta>"),
            BotCommand("approvals", "Ver aprobaciones pendientes"),
            BotCommand("nlm_list", "Listar cuadernos de NotebookLM"),
            BotCommand("nlm_create", "Crear cuaderno + Deep Research — /nlm_create <tema>"),
            BotCommand("nlm_podcast", "Generar podcast del cuaderno activo"),
        ]
        try:
            await self._app.bot.set_my_commands(commands)
        except Exception:
            logger.warning("Could not set bot commands menu")

    async def _handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        user_id = str(update.effective_user.id)
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

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if not update.message.photo:
            return
        photo = update.message.photo[-1]
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
        document = update.message.document
        if document is None:
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
            await update.message.reply_text(part)

    async def send_photo(self, *, chat_id: int, photo_path: str, caption: str | None = None) -> None:
        if self._app is None:
            return
        with open(photo_path, "rb") as photo:
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
            await update.message.reply_text(part)

    def _is_authorized(self, update: Update) -> bool:
        if self._allowed_user_id is None:
            return True
        return str(update.effective_user.id) == self._allowed_user_id
