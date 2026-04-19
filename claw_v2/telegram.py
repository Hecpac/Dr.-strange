from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from telegram import LinkPreviewOptions, Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

from claw_v2.bot import BotService
from claw_v2.voice import VoiceUnavailableError, extract_audio, synthesize_voice_note, transcribe

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


_MAX_DOC_CHARS = 50_000


def _extract_document_text(path: Path, mime_type: str | None, file_name: str | None) -> str:
    suffix = path.suffix.lower()
    # PDF extraction via pypdf
    if suffix == ".pdf" or (mime_type and "pdf" in mime_type):
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            pages = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
            content = "\n\n".join(pages)
            if content.strip():
                if len(content) > _MAX_DOC_CHARS:
                    content = content[:_MAX_DOC_CHARS] + "\n\n[... truncado a 50k chars]"
                return content
        except Exception:
            pass
        return f"[PDF sin texto extraíble: {file_name or 'documento'}, {path.stat().st_size} bytes]"
    # Plain text / code / csv / json etc.
    try:
        content = path.read_text(errors="replace")
        if len(content) > _MAX_DOC_CHARS:
            content = content[:_MAX_DOC_CHARS] + "\n\n[... truncado a 50k chars]"
        return content
    except Exception:
        return f"[Archivo binario: {file_name or 'sin nombre'}, {path.stat().st_size} bytes, mime={mime_type}]"


async def _maybe_send_chat_action(message: Any, action: str) -> None:
    result = message.chat.send_action(action)
    if inspect.isawaitable(result):
        await result


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

    def _emit_latency(
        self,
        *,
        session_id: str,
        user_id: str,
        message_kind: str,
        status: str,
        bot_ms: float,
        reply_ms: float,
        total_ms: float,
        response_parts: int,
        response_chars: int,
    ) -> None:
        observe = getattr(self._bot_service, "observe", None)
        if observe is None:
            return
        try:
            observe.emit(
                "telegram_latency",
                payload={
                    "session_id": session_id,
                    "user_id": user_id,
                    "message_kind": message_kind,
                    "status": status,
                    "bot_ms": round(bot_ms, 1),
                    "reply_ms": round(reply_ms, 1),
                    "total_ms": round(total_ms, 1),
                    "response_parts": response_parts,
                    "response_chars": response_chars,
                },
            )
        except Exception:
            logger.debug("Could not emit telegram latency", exc_info=True)

    _PID_FILE = Path.home() / ".claw" / "telegram.pid"

    async def start(self) -> None:
        if self._token is None:
            return
        # Single-instance guard: kill stale polling process if PID file exists
        if self._PID_FILE.exists():
            try:
                old_pid = int(self._PID_FILE.read_text().strip())
                import os, signal
                os.kill(old_pid, signal.SIGKILL)
                logger.warning("Killed stale Telegram poller (pid %d)", old_pid)
                await asyncio.sleep(1)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self._PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        import os
        self._PID_FILE.write_text(str(os.getpid()))
        self._app = ApplicationBuilder().token(self._token).build()
        self._app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.COMMAND, self._handle_text))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._handle_photo))
        self._app.add_handler(MessageHandler(filters.Document.IMAGE, self._handle_image_document))
        self._app.add_handler(MessageHandler(filters.VOICE, self._handle_voice))
        self._app.add_handler(MessageHandler(filters.AUDIO, self._handle_audio))
        self._app.add_handler(MessageHandler(filters.Document.AUDIO, self._handle_audio_document))
        self._app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, self._handle_video))
        self._app.add_handler(MessageHandler(
            filters.Document.ALL & ~filters.Document.IMAGE & ~filters.Document.AUDIO,
            self._handle_document,
        ))
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
            BotCommand("grill", "Entrevista rigurosa sobre un plan — /grill <plan>"),
            BotCommand("tdd", "Desarrollo TDD red-green-refactor — /tdd <feature>"),
            BotCommand("improve_arch", "Review arquitectural del codebase — /improve_arch"),
            BotCommand("playbooks", "Listar playbooks disponibles"),
            BotCommand("backtest", "Backtesting QTS — /backtest <instrucción>"),
            BotCommand("effort", "Ajustar nivel de esfuerzo — /effort <level> [lane]"),
            BotCommand("verify", "Verificar trabajo actual — /verify [foco]"),
            BotCommand("focus", "Toggle focus mode (solo resultados finales)"),
            BotCommand("voice", "Responder por audio — /voice [voz]"),
            BotCommand("design", "Crear prototipo en Claude Design — /design <brief>"),
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
        started_at = time.perf_counter()
        await _maybe_send_chat_action(update.message, "typing")
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
                short = err_str[:300] if len(err_str) > 300 else err_str
                response = f"El runtime de Claude falló: {short}"
            elif "API Error" in err_str or "invalid_request" in err_str:
                short = err_str[:300] if len(err_str) > 300 else err_str
                response = f"Error con la API: {short}"
            else:
                short = err_str[:300] if len(err_str) > 300 else err_str
                response = f"Error procesando tu mensaje: {short}"
        bot_done_at = time.perf_counter()
        if not response or not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        voice_name = self._bot_service.is_voice_mode(session_id)
        if voice_name and self._voice_api_key:
            try:
                await _maybe_send_chat_action(update.message, "record_voice")
                ogg_path = await synthesize_voice_note(
                    response, api_key=self._voice_api_key, voice=voice_name,
                )
                try:
                    with open(ogg_path, "rb") as f:
                        await update.message.reply_voice(voice=f)
                finally:
                    ogg_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("TTS failed, falling back to text", exc_info=True)
                parts = _split_message(response)
                for part in parts:
                    await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)
        else:
            parts = _split_message(response)
            for part in parts:
                await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)
        finished_at = time.perf_counter()
        self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="text",
            status="ok",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=len(parts),
            response_chars=len(response),
        )

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
        await _maybe_send_chat_action(update.message, "typing")
        tmp_path = Path(f"/tmp/claw-voice-{voice.file_unique_id}.ogg")
        try:
            file = await context.bot.get_file(voice.file_id, read_timeout=30, connect_timeout=15)
            await file.download_to_drive(str(tmp_path))
        except Exception:
            logger.exception("Voice download failed")
            await update.message.reply_text("No pude descargar la nota de voz. Intenta de nuevo.")
            return
        try:
            text = await transcribe(tmp_path, api_key=self._voice_api_key)
        except VoiceUnavailableError:
            logger.exception("Voice transcription unavailable")
            await update.message.reply_text("Voice transcription not available right now.")
            return
        except Exception:
            logger.exception("Voice transcription failed")
            await update.message.reply_text("No pude transcribir la nota de voz.")
            return
        finally:
            tmp_path.unlink(missing_ok=True)
        await self._handle_text_content(update, f"[Nota de voz]: {text}")

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
        await _maybe_send_chat_action(update.message, "typing")
        suffix = Path(audio.file_name).suffix if audio.file_name else ".mp3"
        tmp_path = Path(f"/tmp/claw-audio-{audio.file_unique_id}{suffix}")
        try:
            file = await context.bot.get_file(audio.file_id, read_timeout=30, connect_timeout=15)
            await file.download_to_drive(str(tmp_path))
        except Exception:
            logger.exception("Audio download failed")
            await update.message.reply_text("No pude descargar el audio. Intenta de nuevo.")
            return
        try:
            text = await transcribe(tmp_path, api_key=self._voice_api_key)
        except VoiceUnavailableError:
            logger.exception("Audio transcription unavailable")
            await update.message.reply_text("Voice transcription not available right now.")
            return
        except Exception:
            logger.exception("Audio transcription failed")
            await update.message.reply_text("No pude transcribir el audio.")
            return
        finally:
            tmp_path.unlink(missing_ok=True)
        caption = update.message.caption or ""
        prefix = f"{caption}\n[Audio transcrito]: " if caption else "[Audio transcrito]: "
        await self._handle_text_content(update, prefix + text)

    async def _handle_audio_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        doc = update.message.document
        if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Archivo de audio demasiado grande (máx 20 MB).")
            return
        await _maybe_send_chat_action(update.message, "typing")
        suffix = Path(doc.file_name).suffix if doc.file_name else ".ogg"
        tmp_path = Path(f"/tmp/claw-audiodoc-{doc.file_unique_id}{suffix}")
        try:
            file = await context.bot.get_file(doc.file_id, read_timeout=30, connect_timeout=15)
            await file.download_to_drive(str(tmp_path))
        except Exception:
            logger.exception("Audio document download failed")
            await update.message.reply_text("No pude descargar el archivo de audio. Intenta de nuevo.")
            return
        try:
            text = await transcribe(tmp_path, api_key=self._voice_api_key)
        except Exception:
            logger.exception("Audio document transcription failed")
            await update.message.reply_text("No pude transcribir el archivo de audio.")
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
        await _maybe_send_chat_action(update.message, "typing")
        tmp_video = Path(f"/tmp/claw-video-{video.file_unique_id}.mp4")
        try:
            file = await context.bot.get_file(video.file_id, read_timeout=60, connect_timeout=15)
            await file.download_to_drive(str(tmp_video))
        except Exception:
            logger.exception("Video download failed")
            await update.message.reply_text("No pude descargar el video. Intenta de nuevo.")
            return
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
        except Exception:
            logger.exception("Video transcription failed")
            await update.message.reply_text("No pude transcribir el video.")
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
        started_at = time.perf_counter()
        await _maybe_send_chat_action(update.message, "typing")
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
        bot_done_at = time.perf_counter()
        if not response or not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        parts = _split_message(response)
        for part in parts:
            await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)
        finished_at = time.perf_counter()
        self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="image",
            status="ok",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=len(parts),
            response_chars=len(response),
        )

    async def _handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_authorized(update):
            return
        if self._is_rate_limited(str(update.effective_user.id)):
            await update.message.reply_text("Demasiados mensajes. Espera un momento.")
            return
        doc = update.message.document
        if doc is None:
            return
        if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
            await update.message.reply_text("Archivo demasiado grande (máx 20 MB).")
            return
        suffix = Path(doc.file_name).suffix if doc.file_name else ""
        tmp_path = Path(f"/tmp/claw-doc-{doc.file_unique_id}{suffix}")
        try:
            file = await context.bot.get_file(doc.file_id, read_timeout=30, connect_timeout=15)
            await file.download_to_drive(str(tmp_path))
        except Exception:
            logger.exception("Document download failed")
            await update.message.reply_text("No pude descargar el archivo. Intenta de nuevo.")
            return
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        started_at = time.perf_counter()
        await _maybe_send_chat_action(update.message, "typing")
        text_content = _extract_document_text(tmp_path, doc.mime_type, doc.file_name)
        caption = update.message.caption or ""
        prompt = caption if caption else f"El usuario envió el archivo '{doc.file_name or 'documento'}'. Analízalo."
        memory_text = f"[archivo: {doc.file_name}] {caption}".strip()
        content_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": f"{prompt}\n\n--- Contenido del archivo ---\n{text_content}"},
        ]
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_multimodal,
                user_id=user_id,
                session_id=session_id,
                content_blocks=content_blocks,
                memory_text=memory_text,
            )
        except Exception:
            logger.exception("Error handling document")
            response = "Error procesando tu documento. Intenta de nuevo."
        finally:
            tmp_path.unlink(missing_ok=True)
        bot_done_at = time.perf_counter()
        if not response or not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        parts = _split_message(response)
        for part in parts:
            await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)
        finished_at = time.perf_counter()
        self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="document",
            status="ok",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=len(parts),
            response_chars=len(response),
        )

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

    async def send_video_url(self, *, chat_id: int, video_url: str, caption: str | None = None) -> None:
        """Send a video by URL to a Telegram chat."""
        if self._app is None:
            return
        await self._app.bot.send_video(chat_id=chat_id, video=video_url, caption=caption)

    async def _handle_text_content(self, update: Update, text: str) -> None:
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        started_at = time.perf_counter()
        try:
            response = await asyncio.to_thread(
                self._bot_service.handle_text, user_id=user_id, session_id=session_id, text=text,
            )
        except Exception:
            logger.exception("Error handling voice message")
            response = "Error processing your voice message."
        bot_done_at = time.perf_counter()
        voice_name = self._bot_service.is_voice_mode(session_id)
        if voice_name and self._voice_api_key:
            try:
                await _maybe_send_chat_action(update.message, "record_voice")
                ogg_path = await synthesize_voice_note(
                    response, api_key=self._voice_api_key, voice=voice_name,
                )
                try:
                    with open(ogg_path, "rb") as f:
                        await update.message.reply_voice(voice=f)
                finally:
                    ogg_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("TTS failed, falling back to text", exc_info=True)
                parts = _split_message(response)
                for part in parts:
                    await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)
        else:
            parts = _split_message(response)
            for part in parts:
                await update.message.reply_text(part, link_preview_options=_NO_PREVIEW)
        finished_at = time.perf_counter()
        self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="transcript",
            status="ok",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=len(parts),
            response_chars=len(response),
        )

    def _is_authorized(self, update: Update) -> bool:
        if self._allowed_user_id is None:
            return False
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
