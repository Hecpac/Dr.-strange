from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import inspect
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import IO, Any, Callable

from asyncio import CancelledError as _AsyncioCancelledError
from asyncio import create_task as _asyncio_create_task
from asyncio import get_running_loop as _asyncio_get_running_loop
from asyncio import shield as _asyncio_shield
from asyncio import sleep as _asyncio_sleep
from concurrent.futures import ThreadPoolExecutor

from telegram import LinkPreviewOptions, Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

from claw_v2.subprocess_runner import run_subprocess_bounded_off_loop


# --- P0 hotfix E: polling singleton lock keyed by token hash ---------------
#
# The PID file alone failed to prevent two daemons polling the same token
# (we logged ``Conflict: terminated by other getUpdates request`` on
# 2026-05-24). A token-hash flock + PID staleness check keeps two PIDs
# from racing the same Telegram bot.

_DEFAULT_POLLING_LOCK_DIR = Path.home() / ".claw"


class PollingLockConflict(RuntimeError):
    """Another live process already holds the polling lock for this token."""

    def __init__(self, owner_pid: int, lock_path: Path) -> None:
        super().__init__(
            f"Telegram polling lock for this token is held by PID {owner_pid} ({lock_path})"
        )
        self.owner_pid = owner_pid
        self.lock_path = lock_path


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _polling_lock_path(token: str, *, base_dir: Path | None = None) -> Path:
    base = base_dir if base_dir is not None else _DEFAULT_POLLING_LOCK_DIR
    return base / f"telegram-poll-{_token_hash(token)}.lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (PermissionError,):
        # Permission denied means the PID exists but is owned by another
        # user — treat as alive (conservative).
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def acquire_polling_lock(
    token: str,
    *,
    base_dir: Path | None = None,
    observe: Callable[[str, dict], None] | None = None,
) -> IO[str]:
    """Atomically claim the polling lock for ``token``.

    Returns an open file handle that holds the flock for the lifetime of
    the caller. Closing it releases the lock.

    Raises ``PollingLockConflict`` if another live PID already polls the
    same token. On conflict, the optional ``observe`` callback receives
    ``("telegram_polling_duplicate_instance", {...})`` so the daemon can
    log it without raw-token leakage.
    """
    lock_path = _polling_lock_path(token, base_dir=base_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    existing_pid: int | None = None
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
        except (OSError, ValueError):
            existing_pid = None

    if (
        existing_pid is not None
        and existing_pid != os.getpid()
        and _pid_alive(existing_pid)
    ):
        if observe is not None:
            try:
                observe(
                    "telegram_polling_duplicate_instance",
                    {
                        "owner_pid": existing_pid,
                        "token_hash": _token_hash(token),
                        "lock_path": str(lock_path),
                    },
                )
            except Exception:
                logger.debug("observe callback raised in acquire_polling_lock", exc_info=True)
        raise PollingLockConflict(existing_pid, lock_path)

    fh = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        fh.close()
        owner_pid = existing_pid if existing_pid is not None else -1
        if observe is not None:
            try:
                observe(
                    "telegram_polling_duplicate_instance",
                    {
                        "owner_pid": owner_pid,
                        "token_hash": _token_hash(token),
                        "lock_path": str(lock_path),
                        "flock_error": str(exc)[:200],
                    },
                )
            except Exception:
                logger.debug("observe callback raised in acquire_polling_lock", exc_info=True)
        raise PollingLockConflict(owner_pid, lock_path) from exc

    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

from claw_v2.bot import BotService
from claw_v2.bot_helpers import _sanitize_chat_response
from claw_v2.voice import VoiceUnavailableError, extract_audio, synthesize_voice_note, transcribe

logger = logging.getLogger(__name__)

MAX_TELEGRAM_LEN = 4096
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
DEFAULT_IMAGE_PROMPT = "El usuario envio esta imagen por Telegram. Analizala y responde de forma util."
DEFAULT_VIDEO_PROMPT = "El usuario envio este video por Telegram. Analiza los frames adjuntos y responde de forma util."
_IMAGES_DIR = Path.home() / ".claw" / "images"
_VIDEOS_DIR = Path.home() / ".claw" / "videos"
DEFAULT_CONNECTION_POOL_SIZE = 32
DEFAULT_POOL_TIMEOUT = 30.0
DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_MEDIA_WRITE_TIMEOUT = 60.0
DEFAULT_GET_UPDATES_POOL_SIZE = 8
DEFAULT_CONCURRENT_UPDATES = 8
DEFAULT_VIDEO_FRAME_COUNT = 4
DEFAULT_VIDEO_FRAME_TIMEOUT_SECONDS = 90.0
DEFAULT_TEXT_SEND_RETRIES = 1
DEFAULT_TEXT_SEND_RETRY_DELAY = 1.0
DEFAULT_TEXT_SEND_CONNECT_TIMEOUT = 5.0
DEFAULT_LATE_DELIVERY_GRACE_SECONDS = 0.5

# Slash commands allowed to bypass the per-chat ordering lock so the operator
# can inspect, approve, or freeze the agent while a long turn is running.
# Everything here is state-inspection or acts on its own locking layer
# (approval files / observation window), not on the active turn's session.
_INTERRUPT_COMMANDS = frozenset(
    {
        "freeze",
        "unfreeze",
        "status",
        "budget_status",
        "approvals",
        "approve",
        "approval_status",
        "task_approve",
        "task_abort",
        "action_approve",
        "action_abort",
    }
)


def _is_interrupt_command(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return False
    command = stripped.split()[0][1:].split("@", 1)[0].lower()
    return command in _INTERRUPT_COMMANDS

_IMAGE_PATH_RE = re.compile(r"(/[^`\s]+?\.(?:png|jpe?g|webp))", re.IGNORECASE)
_SEND_IMAGE_REQUEST_WORDS = (
    "ponla",
    "mandala",
    "mándala",
    "enviala",
    "envíala",
    "subela",
    "súbela",
    "pasala",
    "pásala",
)
_TELEGRAM_TARGET_WORDS = ("telegram", "aqui", "aquí", "chat")
_NONFATAL_SEND_ERRORS = (BrokenPipeError, ConnectionResetError)
_NONRETRYABLE_TEXT_SEND_ERRORS = {
    "BadRequest",
    "ChatMigrated",
    "Forbidden",
    "InvalidToken",
}


def _split_message(text: str, max_len: int = MAX_TELEGRAM_LEN) -> list[str]:
    if not text:
        return [text]
    parts: list[str] = []
    while text:
        parts.append(text[:max_len])
        text = text[max_len:]
    return parts


def _looks_like_latest_image_send_request(text: str) -> bool:
    lowered = text.strip().lower()
    return (
        any(word in lowered for word in _SEND_IMAGE_REQUEST_WORDS)
        and any(word in lowered for word in _TELEGRAM_TARGET_WORDS)
    )


def _latest_existing_image_path(messages: list[dict[str, Any]]) -> Path | None:
    for message in reversed(messages):
        content = str(message.get("content") or "")
        for match in reversed(_IMAGE_PATH_RE.findall(content)):
            path = Path(match).expanduser()
            if path.exists() and path.is_file():
                return path
    return None


def _log_nonfatal_send_error(operation: str, exc: BaseException) -> None:
    logger.warning("%s failed with non-fatal stream error: %s", operation, exc)


def _is_retryable_text_send_error(exc: BaseException) -> bool:
    if isinstance(exc, _NONFATAL_SEND_ERRORS):
        return True
    name = type(exc).__name__
    if name in _NONRETRYABLE_TEXT_SEND_ERRORS:
        return False
    return name in {"NetworkError", "RetryAfter", "TimedOut"}


def _reply_context_metadata(update: Update) -> dict[str, Any] | None:
    message = getattr(update, "message", None)
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None
    raw_text = getattr(reply, "text", None)
    raw_caption = getattr(reply, "caption", None)
    text = raw_text if isinstance(raw_text, str) else ""
    if not text and isinstance(raw_caption, str):
        text = raw_caption
    text = str(text).strip()
    if not text:
        return None
    return {
        "reply_context": {
            "source": "telegram_reply",
            "text": text[:2000],
        }
    }


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _build_image_content_blocks(
    image_path: Path,
    *,
    caption: str | None,
    mime_type: str | None = None,
    durable_path: Path | None = None,
) -> tuple[list[dict[str, Any]], str]:
    prompt_text = caption.strip() if caption and caption.strip() else DEFAULT_IMAGE_PROMPT
    memory_text = f"[Imagen adjunta] path: {durable_path or image_path}"
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


def _build_video_content_blocks(
    frame_paths: list[Path],
    *,
    caption: str | None,
    durable_video_path: Path | None,
    transcript: str | None,
    audio_error: str | None,
    duration_seconds: float | None,
) -> tuple[list[dict[str, Any]], str]:
    prompt_text = caption.strip() if caption and caption.strip() else DEFAULT_VIDEO_PROMPT
    detail_lines = [
        prompt_text,
        "",
        f"[Video adjunto: {len(frame_paths)} frames muestreados para inspeccion visual.]",
    ]
    if duration_seconds is not None and duration_seconds > 0:
        detail_lines.append(f"[Duracion aproximada: {duration_seconds:.1f}s.]")
    if transcript and transcript.strip():
        detail_lines.extend(["", "[Audio transcrito]:", transcript.strip()[:8000]])
    elif audio_error:
        detail_lines.append("[Audio]: no disponible o no extraible; analiza visualmente los frames.")

    memory_lines = [f"[Video adjunto] path: {durable_video_path}" if durable_video_path else "[Video adjunto]"]
    if duration_seconds is not None and duration_seconds > 0:
        memory_lines.append(f"duracion_aproximada={duration_seconds:.1f}s")
    memory_lines.append("frames=" + ", ".join(str(path) for path in frame_paths))
    if caption and caption.strip():
        memory_lines.append(caption.strip())
    if transcript and transcript.strip():
        memory_lines.append("[Audio transcrito]: " + transcript.strip()[:8000])

    blocks: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(detail_lines)}]
    for frame_path in frame_paths:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _resolve_image_mime_type(frame_path, "image/jpeg"),
                    "data": base64.b64encode(frame_path.read_bytes()).decode("ascii"),
                },
            }
        )
    return blocks, "\n".join(memory_lines)


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


def _safe_media_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "media")).strip("._-")
    return (cleaned or "media")[:80]


def _video_frame_timestamps(duration_seconds: float, max_frames: int) -> list[float]:
    if duration_seconds <= 0:
        return []
    if max_frames <= 1:
        return [max(0.0, min(duration_seconds / 2.0, duration_seconds - 0.1))]
    if duration_seconds < 2.0:
        return [max(0.0, duration_seconds / 2.0)]
    anchors = [0.08, 0.33, 0.66, 0.92]
    if max_frames != len(anchors):
        step = 1.0 / (max_frames + 1)
        anchors = [step * index for index in range(1, max_frames + 1)]
    return [max(0.0, min(duration_seconds - 0.25, duration_seconds * anchor)) for anchor in anchors[:max_frames]]


async def _probe_video_duration_seconds(video_path: Path) -> float | None:
    try:
        result = await run_subprocess_bounded_off_loop(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            timeout_s=10.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        duration = float((result.stdout or "").strip())
    except ValueError:
        return None
    return duration if duration > 0 else None


async def _run_ffmpeg_silent(cmd: list[str], *, timeout_seconds: float) -> bool:
    try:
        result = await run_subprocess_bounded_off_loop(list(cmd), timeout_s=timeout_seconds)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


async def _extract_video_frame_paths(
    video_path: Path,
    *,
    file_unique_id: str,
    max_frames: int = DEFAULT_VIDEO_FRAME_COUNT,
) -> tuple[list[Path], float | None]:
    _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    stem = _safe_media_stem(file_unique_id)
    duration_seconds = await _probe_video_duration_seconds(video_path)
    frame_paths: list[Path] = []
    if duration_seconds is not None:
        for index, timestamp in enumerate(_video_frame_timestamps(duration_seconds, max_frames), start=1):
            frame_path = _IMAGES_DIR / f"{stem}-frame-{index:02d}.jpg"
            ok = await _run_ffmpeg_silent(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-ss",
                    f"{timestamp:.2f}",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "3",
                    str(frame_path),
                ],
                timeout_seconds=DEFAULT_VIDEO_FRAME_TIMEOUT_SECONDS,
            )
            if ok and frame_path.exists() and frame_path.stat().st_size > 0:
                frame_paths.append(frame_path)
    if frame_paths:
        return frame_paths, duration_seconds

    pattern = _IMAGES_DIR / f"{stem}-frame-%03d.jpg"
    ok = await _run_ffmpeg_silent(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            "fps=1",
            "-frames:v",
            str(max_frames),
            "-q:v",
            "3",
            str(pattern),
        ],
        timeout_seconds=DEFAULT_VIDEO_FRAME_TIMEOUT_SECONDS,
    )
    if ok:
        frame_paths = [
            path
            for path in sorted(_IMAGES_DIR.glob(f"{stem}-frame-*.jpg"))
            if path.exists() and path.stat().st_size > 0
        ][:max_frames]
    return frame_paths, duration_seconds


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
    # Best-effort indicator: a transient network error here must never kill
    # the handler before the message is processed (2026-06-10 audit C1).
    try:
        result = message.chat.send_action(action)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.debug("chat action %s failed", action, exc_info=True)


class TelegramTransport:
    def __init__(
        self,
        bot_service: BotService,
        token: str | None,
        allowed_user_id: str | None = None,
        voice_api_key: str | None = None,
        agent_runtime: object | None = None,
        xai_api_key: str | None = None,
    ) -> None:
        self._bot_service = bot_service
        self._agent_runtime = agent_runtime
        self._token = token
        self._allowed_user_id = allowed_user_id
        self._voice_api_key = voice_api_key
        self._xai_api_key = xai_api_key or os.environ.get("XAI_API_KEY")
        self._app = None
        self._rate_limits: dict[str, list[float]] = {}
        self._rate_max = 10  # max requests per window
        self._rate_window = 60.0  # seconds
        self._connection_pool_size = _env_int("TELEGRAM_CONNECTION_POOL_SIZE", DEFAULT_CONNECTION_POOL_SIZE)
        self._pool_timeout = _env_float("TELEGRAM_POOL_TIMEOUT", DEFAULT_POOL_TIMEOUT)
        self._request_timeout = _env_float("TELEGRAM_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
        self._media_write_timeout = _env_float("TELEGRAM_MEDIA_WRITE_TIMEOUT", DEFAULT_MEDIA_WRITE_TIMEOUT)
        self._get_updates_pool_size = _env_int("TELEGRAM_GET_UPDATES_POOL_SIZE", DEFAULT_GET_UPDATES_POOL_SIZE)
        self._concurrent_updates = _env_int("TELEGRAM_CONCURRENT_UPDATES", DEFAULT_CONCURRENT_UPDATES)
        # Per-chat ordering: agent turns for the same session run one at a
        # time even with concurrent update processing enabled.
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._text_send_retries = max(1, _env_int("TELEGRAM_TEXT_SEND_RETRIES", DEFAULT_TEXT_SEND_RETRIES))
        self._text_send_retry_delay = max(
            0.0,
            _env_float("TELEGRAM_TEXT_SEND_RETRY_DELAY", DEFAULT_TEXT_SEND_RETRY_DELAY),
        )
        self._text_send_connect_timeout = max(
            1.0,
            _env_float("TELEGRAM_TEXT_SEND_CONNECT_TIMEOUT", DEFAULT_TEXT_SEND_CONNECT_TIMEOUT),
        )
        self._late_delivery_grace_seconds = max(
            0.0,
            _env_float("TELEGRAM_LATE_DELIVERY_GRACE_SECONDS", DEFAULT_LATE_DELIVERY_GRACE_SECONDS),
        )
        # P0 hotfix E: held for the lifetime of the polling loop.
        self._polling_lock_fh: IO[str] | None = None
        # Diagnostic observe events are persisted on this dedicated single
        # worker so the synchronous locked-SQLite retry sleep in
        # ObserveStream._persist_event (up to ~0.3s under contention) never
        # runs on the asyncio event loop and stutters Telegram handling during
        # a lock storm (2026-06-10 incident). One worker keeps emit ordering;
        # isolating it from the loop's default executor stops emit retry-naps
        # from starving message delivery. Created lazily on first emit.
        self._observe_executor: ThreadPoolExecutor | None = None
        self._observe_executor_closed = False

    def _chat_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(session_id)
        if lock is None:
            lock = self._chat_locks.setdefault(session_id, asyncio.Lock())
        return lock

    def _emit_transport_event(self, event_type: str, payload: dict[str, Any]) -> None:
        observe = getattr(self._bot_service, "observe", None)
        if observe is None:
            return
        try:
            observe.emit(event_type, payload=payload)
        except Exception:
            logger.debug("Could not emit Telegram transport event", exc_info=True)

    def _emit_polling_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Callback signature acquire_polling_lock expects (event_type, payload)."""
        self._emit_transport_event(event_type, payload)

    def _observe_emit_executor(self) -> ThreadPoolExecutor | None:
        if self._observe_executor_closed:
            # stop() already shut the executor down: don't resurrect one
            # nobody will shut down again.
            return None
        executor = self._observe_executor
        if executor is None:
            # Created on the event-loop thread (the only caller of the async
            # emit path), so this lazy init needs no extra locking.
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tg-observe-emit"
            )
            self._observe_executor = executor
        return executor

    def _shutdown_observe_executor(self) -> None:
        self._observe_executor_closed = True
        executor = self._observe_executor
        if executor is not None:
            self._observe_executor = None
            # wait=False: don't block shutdown on a lock-storm retry; queued
            # diagnostic emits still drain on the worker before it exits.
            executor.shutdown(wait=False)

    async def _aemit_transport_event(
        self, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Persist a transport event off the event-loop thread.

        ``observe.emit`` -> ``ObserveStream._persist_event`` retries
        locked-SQLite writes with a synchronous ``time.sleep``; running it
        inline on the loop stutters all Telegram handling during a lock storm.
        Offload the (unchanged) synchronous emit to the dedicated single-worker
        executor and await it: the loop stays free to service other handlers
        while the write — and any retry sleep — happens on the worker thread,
        and awaiting keeps each session's events ordered and visible to the
        caller once its handler returns.
        """
        executor = self._observe_emit_executor()
        if executor is None:
            # Shutdown-tail emit: keep the audit event, accept the inline write.
            self._emit_transport_event(event_type, payload)
            return
        loop = _asyncio_get_running_loop()
        await loop.run_in_executor(
            executor,
            self._emit_transport_event,
            event_type,
            payload,
        )

    async def _emit_latency(
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
        await self._aemit_transport_event(
            "telegram_latency",
            {
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

    @staticmethod
    def _outbound_text_payload(
        *,
        session_id: str,
        user_id: str,
        message_kind: str,
        method: str,
        part_index: int,
        part_count: int,
        part_chars: int,
        attempt: int | None = None,
        error: BaseException | None = None,
        message_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "message_kind": message_kind,
            "method": method,
            "part_index": part_index,
            "part_count": part_count,
            "part_chars": part_chars,
        }
        if attempt is not None:
            payload["attempt"] = attempt
        if message_id is not None:
            payload["message_id"] = message_id
        if error is not None:
            payload["error_type"] = type(error).__name__
            payload["error"] = str(error)[:300]
        return payload

    async def _emit_outbound_text_event(
        self,
        event_type: str,
        **kwargs: Any,
    ) -> None:
        await self._aemit_transport_event(
            event_type, self._outbound_text_payload(**kwargs)
        )

    def _emit_outbound_text_event_nowait(
        self,
        event_type: str,
        **kwargs: Any,
    ) -> None:
        """Fire-and-forget emit on the single-worker executor.

        For per-attempt telemetry on the send critical path: under SQLite
        contention an awaited emit costs up to ~0.3s per write, which a
        multipart reply pays 2-3 times per part (T5, 2026-06-12). The single
        worker preserves ordering; the final sent/error emit stays awaited
        and acts as the barrier.
        """
        payload = self._outbound_text_payload(**kwargs)
        executor = self._observe_emit_executor()
        if executor is None:
            # Shutdown-tail emit: keep the audit event, accept the inline write.
            self._emit_transport_event(event_type, payload)
            return
        executor.submit(self._emit_transport_event, event_type, payload)

    async def _sleep_before_text_send_retry(self, exc: BaseException, attempt: int) -> None:
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            delay = float(retry_after)
        else:
            delay = self._text_send_retry_delay * attempt
        if delay > 0:
            await asyncio.sleep(delay)

    def _send_text_timeout_kwargs(self) -> dict[str, float]:
        return {
            "connect_timeout": self._text_send_connect_timeout,
            "read_timeout": self._request_timeout,
            "write_timeout": self._request_timeout,
            "pool_timeout": self._pool_timeout,
        }

    async def _send_reply_text_parts(
        self,
        update: Update,
        parts: list[str],
        *,
        session_id: str,
        user_id: str,
        message_kind: str,
    ) -> tuple[int, bool]:
        sent_parts = 0
        part_count = len(parts)
        for index, part in enumerate(parts, start=1):
            sent = await self._send_reply_text_part(
                update,
                part,
                session_id=session_id,
                user_id=user_id,
                message_kind=message_kind,
                part_index=index,
                part_count=part_count,
            )
            if not sent:
                return sent_parts, False
            sent_parts += 1
        return sent_parts, True

    async def _send_reply_text_part(
        self,
        update: Update,
        part: str,
        *,
        session_id: str,
        user_id: str,
        message_kind: str,
        part_index: int,
        part_count: int,
    ) -> bool:
        timeout_kwargs = self._send_text_timeout_kwargs()
        for attempt in range(1, self._text_send_retries + 1):
            self._emit_outbound_text_event_nowait(
                "telegram_outbound_attempt",
                session_id=session_id,
                user_id=user_id,
                message_kind=message_kind,
                method="reply_text",
                part_index=part_index,
                part_count=part_count,
                part_chars=len(part),
                attempt=attempt,
            )
            try:
                result = await update.message.reply_text(
                    part,
                    link_preview_options=_NO_PREVIEW,
                    **timeout_kwargs,
                )
            except Exception as exc:
                retryable = _is_retryable_text_send_error(exc)
                logger.warning(
                    "Telegram reply_text failed%s",
                    "; retrying" if retryable and attempt < self._text_send_retries else "",
                    exc_info=True,
                )
                await self._emit_outbound_text_event(
                    "telegram_outbound_error",
                    session_id=session_id,
                    user_id=user_id,
                    message_kind=message_kind,
                    method="reply_text",
                    part_index=part_index,
                    part_count=part_count,
                    part_chars=len(part),
                    attempt=attempt,
                    error=exc,
                )
                if not retryable:
                    break
                if attempt < self._text_send_retries:
                    await self._sleep_before_text_send_retry(exc, attempt)
            else:
                message_id = getattr(result, "message_id", None)
                await self._emit_outbound_text_event(
                    "telegram_outbound_sent",
                    session_id=session_id,
                    user_id=user_id,
                    message_kind=message_kind,
                    method="reply_text",
                    part_index=part_index,
                    part_count=part_count,
                    part_chars=len(part),
                    attempt=attempt,
                    message_id=message_id if isinstance(message_id, int) else None,
                )
                return True

        if self._app is None:
            return False
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        if chat_id is None:
            return False

        # PTB reply_text and bot.send_message share the same httpx transport. If
        # reply_text times out, try the stdlib Bot API path before spending more
        # time in the same failing client stack.
        if await self._send_text_direct_bot_api(
            part,
            chat_id=chat_id,
            session_id=session_id,
            user_id=user_id,
            message_kind=message_kind,
            part_index=part_index,
            part_count=part_count,
        ):
            return True

        for attempt in range(1, self._text_send_retries + 1):
            self._emit_outbound_text_event_nowait(
                "telegram_outbound_attempt",
                session_id=session_id,
                user_id=user_id,
                message_kind=message_kind,
                method="send_message_fallback",
                part_index=part_index,
                part_count=part_count,
                part_chars=len(part),
                attempt=attempt,
            )
            try:
                result = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    link_preview_options=_NO_PREVIEW,
                    **timeout_kwargs,
                )
            except Exception as exc:
                retryable = _is_retryable_text_send_error(exc)
                logger.warning(
                    "Telegram send_message fallback failed%s",
                    "; retrying" if retryable and attempt < self._text_send_retries else "",
                    exc_info=True,
                )
                await self._emit_outbound_text_event(
                    "telegram_outbound_error",
                    session_id=session_id,
                    user_id=user_id,
                    message_kind=message_kind,
                    method="send_message_fallback",
                    part_index=part_index,
                    part_count=part_count,
                    part_chars=len(part),
                    attempt=attempt,
                    error=exc,
                )
                if not retryable:
                    return False
                if attempt < self._text_send_retries:
                    await self._sleep_before_text_send_retry(exc, attempt)
                continue

            message_id = getattr(result, "message_id", None)
            await self._emit_outbound_text_event(
                "telegram_outbound_sent",
                session_id=session_id,
                user_id=user_id,
                message_kind=message_kind,
                method="send_message_fallback",
                part_index=part_index,
                part_count=part_count,
                part_chars=len(part),
                attempt=attempt,
                message_id=message_id if isinstance(message_id, int) else None,
            )
            return True
        return False

    async def _send_text_direct_bot_api(
        self,
        part: str,
        *,
        chat_id: int | str,
        session_id: str,
        user_id: str,
        message_kind: str,
        part_index: int,
        part_count: int,
    ) -> bool:
        if not self._token:
            return False
        self._emit_outbound_text_event_nowait(
            "telegram_outbound_attempt",
            session_id=session_id,
            user_id=user_id,
            message_kind=message_kind,
            method="bot_api_direct_fallback",
            part_index=part_index,
            part_count=part_count,
            part_chars=len(part),
            attempt=1,
        )
        try:
            message_id = await asyncio.to_thread(
                self._send_text_direct_bot_api_sync,
                chat_id=chat_id,
                text=part,
            )
        except Exception as exc:
            logger.warning("Telegram direct Bot API fallback failed", exc_info=True)
            await self._emit_outbound_text_event(
                "telegram_outbound_error",
                session_id=session_id,
                user_id=user_id,
                message_kind=message_kind,
                method="bot_api_direct_fallback",
                part_index=part_index,
                part_count=part_count,
                part_chars=len(part),
                attempt=1,
                error=exc,
            )
            return False
        await self._emit_outbound_text_event(
            "telegram_outbound_sent",
            session_id=session_id,
            user_id=user_id,
            message_kind=message_kind,
            method="bot_api_direct_fallback",
            part_index=part_index,
            part_count=part_count,
            part_chars=len(part),
            attempt=1,
            message_id=message_id if isinstance(message_id, int) else None,
        )
        return True

    def _send_text_direct_bot_api_sync(self, *, chat_id: int | str, text: str) -> int | None:
        import json
        import urllib.parse
        import urllib.request

        if not self._token:
            raise RuntimeError("telegram token unavailable")
        body = urllib.parse.urlencode(
            {
                "chat_id": str(chat_id),
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=body,
            method="POST",
        )
        timeout = self._text_send_connect_timeout
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            description = str(payload.get("description") or "unknown")[:300]
            raise RuntimeError(f"telegram direct API failed: {description}")
        message_id = payload.get("result", {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    _PID_FILE = Path.home() / ".claw" / "telegram.pid"

    async def start(self) -> None:
        if self._token is None:
            return
        # Single-instance guard: stop stale polling process if PID file exists.
        if self._PID_FILE.exists():
            try:
                old_pid = int(self._PID_FILE.read_text().strip())
                import os, signal
                if old_pid != os.getpid():
                    proc = await run_subprocess_bounded_off_loop(
                        ["ps", "-p", str(old_pid), "-o", "command="],
                        check=False,
                        timeout_s=5,
                        kill_process_group=False,
                    )
                    if "claw_v2.main" in proc.stdout:
                        os.kill(old_pid, signal.SIGTERM)
                        logger.warning("Requested stale Telegram poller stop (pid %d)", old_pid)
                        for _ in range(10):
                            await asyncio.sleep(0.2)
                            try:
                                os.kill(old_pid, 0)
                            except ProcessLookupError:
                                break
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self._PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        import os
        # P0 hotfix E: acquire token-hash flock BEFORE writing the PID file.
        # If a conflict aborts startup, we must not leave a pidfile that lies
        # about who owns polling — the watchdog would SIGTERM the wrong PID
        # on the next launch.
        try:
            self._polling_lock_fh = acquire_polling_lock(
                self._token, observe=self._emit_polling_event
            )
        except PollingLockConflict as exc:
            logger.error(
                "Refusing to start Telegram polling: another live process (PID %d) holds the lock",
                exc.owner_pid,
            )
            return
        # (Re)starting: late emits may offload again.
        self._observe_executor_closed = False
        self._PID_FILE.write_text(str(os.getpid()))
        builder = ApplicationBuilder().token(self._token)
        # Process updates concurrently so operator interrupts (/freeze,
        # /approve) are not queued behind a multi-minute agent turn; per-chat
        # ordering for agent turns is enforced by _chat_lock.
        builder.concurrent_updates(self._concurrent_updates)
        builder.connection_pool_size(self._connection_pool_size)
        builder.pool_timeout(self._pool_timeout)
        builder.connect_timeout(self._request_timeout)
        builder.read_timeout(self._request_timeout)
        builder.write_timeout(self._request_timeout)
        builder.media_write_timeout(self._media_write_timeout)
        builder.get_updates_connection_pool_size(self._get_updates_pool_size)
        builder.get_updates_pool_timeout(self._pool_timeout)
        builder.get_updates_connect_timeout(self._request_timeout)
        builder.get_updates_read_timeout(self._request_timeout)
        builder.get_updates_write_timeout(self._request_timeout)
        self._app = builder.build()
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
        if self._app is None:
            return
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("Dr. Strange online at %s", now)

    async def stop(self) -> None:
        if self._app is None:
            self._shutdown_observe_executor()
            return
        app = self._app
        self._app = None
        errors: list[str] = []
        steps = (
            ("updater_stop", app.updater.stop),
            ("application_stop", app.stop),
            ("application_shutdown", app.shutdown),
        )
        for step, call in steps:
            try:
                result = call()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                detail = f"{type(exc).__name__}: {str(exc)[:300]}"
                errors.append(f"{step}: {detail}")
                logger.warning("Telegram transport %s failed: %s", step, detail, exc_info=True)
        if errors:
            self._emit_transport_event(
                "telegram_transport_stop_error",
                payload={"errors": errors, "error_count": len(errors)},
            )
        try:
            import os
            if self._PID_FILE.exists() and self._PID_FILE.read_text().strip() == str(os.getpid()):
                self._PID_FILE.unlink(missing_ok=True)
        except Exception:
            logger.debug("Could not clear Telegram PID file", exc_info=True)
        # P0 hotfix E: release the polling singleton lock.
        if self._polling_lock_fh is not None:
            try:
                self._polling_lock_fh.close()
            except Exception:
                logger.debug("Could not close Telegram polling lock", exc_info=True)
            self._polling_lock_fh = None
        self._shutdown_observe_executor()

    async def _set_commands(self) -> None:
        from telegram import BotCommand
        commands = [
            BotCommand("browse", "Abrir y revisar cualquier URL — /browse <url>"),
            BotCommand("status", "Estado del sistema (heartbeat)"),
            BotCommand("freeze", "Pausar autoexec durante observación"),
            BotCommand("unfreeze", "Reactivar autoexec"),
            BotCommand("budget_status", "Costo y presupuesto de observación"),
            BotCommand("approvals", "Ver aprobaciones pendientes"),
            BotCommand("models", "Listar modelos y billing"),
            BotCommand("model", "Ver/cambiar modelo — /model status"),
            BotCommand("jobs", "Ver trabajos autónomos persistidos"),
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
        delivery_state = {"normal_send_started": False}
        await _maybe_send_chat_action(update.message, "typing")
        try:
            direct_response = await self._maybe_send_latest_generated_image(
                update=update,
                user_id=user_id,
                session_id=session_id,
                text=text,
            )
            if direct_response is not None:
                response = direct_response
            else:
                response_task = _asyncio_create_task(
                    self._handle_agent_text(
                        user_id=user_id,
                        session_id=session_id,
                        text=text,
                        context_metadata=_reply_context_metadata(update),
                    )
                )
                self._attach_late_text_delivery_guard(
                    response_task,
                    update=update,
                    session_id=session_id,
                    user_id=user_id,
                    started_at=started_at,
                    delivery_state=delivery_state,
                )
                response = await _asyncio_shield(response_task)
        except _AsyncioCancelledError:
            logger.warning("Telegram text handler cancelled before normal delivery; late delivery guard armed")
            raise
        except Exception as exc:
            logger.exception("Error handling message")
            err_str = str(exc)
            if "Could not process image" in err_str:
                response = "No pude procesar la imagen/screenshot. Intento sin captura visual."
                try:
                    response = await self._handle_agent_text(
                        user_id=user_id,
                        session_id=session_id,
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
            elif "database is locked" in err_str.lower():
                response = "El runtime local tuvo contención de base de datos. Intenta de nuevo en unos segundos."
            else:
                response = "Error procesando tu mensaje. Intenta de nuevo."
        bot_done_at = time.perf_counter()
        if response is None:
            finished_at = time.perf_counter()
            await self._emit_latency(
                session_id=session_id,
                user_id=user_id,
                message_kind="text",
                status="no_reply",
                bot_ms=(bot_done_at - started_at) * 1000,
                reply_ms=(finished_at - bot_done_at) * 1000,
                total_ms=(finished_at - started_at) * 1000,
                response_parts=0,
                response_chars=0,
            )
            return
        if not response or not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        response = self._sanitize_outbound_response(session_id, response)
        parts = _split_message(response)
        voice_name = self._bot_service.is_voice_mode(session_id)
        sent_parts = 0
        delivery_ok = False
        delivery_state["normal_send_started"] = True
        if voice_name and (self._voice_api_key or self._xai_api_key):
            try:
                await _maybe_send_chat_action(update.message, "record_voice")
                ogg_path = await synthesize_voice_note(
                    response,
                    api_key=self._voice_api_key,
                    voice=voice_name,
                    xai_api_key=self._xai_api_key,
                )
                try:
                    with open(ogg_path, "rb") as f:
                        await update.message.reply_voice(voice=f)
                finally:
                    ogg_path.unlink(missing_ok=True)
                sent_parts = len(parts)
                delivery_ok = True
            except Exception:
                logger.warning("TTS failed, falling back to text", exc_info=True)
                sent_parts, delivery_ok = await self._send_reply_text_parts(
                    update,
                    parts,
                    session_id=session_id,
                    user_id=user_id,
                    message_kind="text",
                )
        else:
            sent_parts, delivery_ok = await self._send_reply_text_parts(
                update,
                parts,
                session_id=session_id,
                user_id=user_id,
                message_kind="text",
            )
        finished_at = time.perf_counter()
        await self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="text",
            status="ok" if delivery_ok else "send_failed",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=sent_parts,
            response_chars=len(response),
        )

    def _attach_late_text_delivery_guard(
        self,
        response_task: "asyncio.Task[str | None]",
        *,
        update: Update,
        session_id: str,
        user_id: str,
        started_at: float,
        delivery_state: dict[str, bool],
    ) -> None:
        def _schedule_late_delivery(task: "asyncio.Task[str | None]") -> None:
            try:
                _asyncio_create_task(
                    self._late_deliver_text_response(
                        task,
                        update=update,
                        session_id=session_id,
                        user_id=user_id,
                        started_at=started_at,
                        delivery_state=delivery_state,
                    )
                )
            except RuntimeError:
                logger.warning("Could not schedule Telegram late delivery guard", exc_info=True)

        response_task.add_done_callback(_schedule_late_delivery)

    async def _late_deliver_text_response(
        self,
        response_task: "asyncio.Task[str | None]",
        *,
        update: Update,
        session_id: str,
        user_id: str,
        started_at: float,
        delivery_state: dict[str, bool],
    ) -> None:
        if self._late_delivery_grace_seconds > 0:
            await _asyncio_sleep(self._late_delivery_grace_seconds)
        if delivery_state.get("normal_send_started"):
            return
        if response_task.cancelled():
            return
        try:
            response = response_task.result()
        except Exception:
            logger.debug("Telegram late delivery guard saw failed response task", exc_info=True)
            return
        if response is None or not str(response).strip():
            return
        response = self._sanitize_outbound_response(session_id, str(response))
        parts = _split_message(response)
        chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
        if chat_id is None:
            return
        sent_parts = 0
        for index, part in enumerate(parts, start=1):
            try:
                message_id = await asyncio.to_thread(
                    self._send_text_direct_bot_api_sync,
                    chat_id=chat_id,
                    text=part,
                )
            except Exception as exc:
                logger.warning("Telegram late direct delivery failed", exc_info=True)
                await self._emit_outbound_text_event(
                    "telegram_outbound_error",
                    session_id=session_id,
                    user_id=user_id,
                    message_kind="text",
                    method="bot_api_late_delivery",
                    part_index=index,
                    part_count=len(parts),
                    part_chars=len(part),
                    attempt=1,
                    error=exc,
                )
                break
            sent_parts += 1
            await self._emit_outbound_text_event(
                "telegram_outbound_sent",
                session_id=session_id,
                user_id=user_id,
                message_kind="text",
                method="bot_api_late_delivery",
                part_index=index,
                part_count=len(parts),
                part_chars=len(part),
                attempt=1,
                message_id=message_id if isinstance(message_id, int) else None,
            )
        finished_at = time.perf_counter()
        if sent_parts:
            await self._emit_latency(
                session_id=session_id,
                user_id=user_id,
                message_kind="text",
                status="late_ok" if sent_parts == len(parts) else "late_partial",
                bot_ms=0.0,
                reply_ms=(finished_at - started_at) * 1000,
                total_ms=(finished_at - started_at) * 1000,
                response_parts=sent_parts,
                response_chars=len(response),
            )


    async def _maybe_send_latest_generated_image(
        self,
        *,
        update: Update,
        user_id: str,
        session_id: str,
        text: str,
    ) -> str | None:
        if not _looks_like_latest_image_send_request(text):
            return None
        memory = getattr(self._bot_service, "memory", None)
        if memory is None or not hasattr(memory, "get_recent_messages"):
            return None
        messages = memory.get_recent_messages(session_id, limit=20)
        image_path = _latest_existing_image_path(messages)
        if image_path is None:
            return None
        try:
            chat_id = int(update.effective_chat.id)
        except (TypeError, ValueError):
            return None
        sent = await self.send_photo(
            chat_id=chat_id,
            photo_path=str(image_path),
            caption="Imagen generada por Dr. Strange",
        )
        if not sent:
            return f"Encontré la imagen, pero Telegram bloqueó el envío desde `{image_path}`."
        self._record_direct_image_send(session_id=session_id, user_id=user_id, text=text, image_path=image_path)
        return "Te la puse aquí en Telegram."

    def _record_direct_image_send(self, *, session_id: str, user_id: str, text: str, image_path: Path) -> None:
        memory = getattr(self._bot_service, "memory", None)
        if memory is None or not hasattr(memory, "store_message"):
            return
        try:
            memory.store_message(session_id, "user", text)
            memory.store_message(session_id, "assistant", f"Imagen enviada por Telegram: `{image_path}`")
        except Exception:
            logger.debug("Could not record direct Telegram image send for user %s", user_id, exc_info=True)

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
        await self._handle_text_content(update, f"[Nota de voz]: {text}", force_voice_reply=True)

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
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        started_at = time.perf_counter()
        tmp_video = Path(f"/tmp/claw-video-{video.file_unique_id}.mp4")
        durable_video: Path | None = None
        try:
            file = await context.bot.get_file(video.file_id, read_timeout=60, connect_timeout=15)
            await file.download_to_drive(str(tmp_video))
            _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
            durable_video = _VIDEOS_DIR / f"{_safe_media_stem(video.file_unique_id)}.mp4"
            shutil.copy2(str(tmp_video), str(durable_video))
        except Exception:
            logger.exception("Video download failed")
            await update.message.reply_text("No pude descargar el video. Intenta de nuevo.")
            return
        tmp_audio: Path | None = None
        transcript: str | None = None
        audio_error: str | None = None
        try:
            try:
                tmp_audio = await extract_audio(tmp_video)
                transcript = await transcribe(tmp_audio, api_key=self._voice_api_key)
            except VoiceUnavailableError as exc:
                audio_error = "voice_unavailable"
                logger.debug("Video audio transcription unavailable; falling back to frames: %s", exc)
            except RuntimeError as exc:
                audio_error = "audio_extract_failed"
                logger.debug("Video audio extraction failed; falling back to frames: %s", exc)
            except Exception:
                audio_error = "audio_transcription_failed"
                logger.warning("Video transcription failed; falling back to frames", exc_info=True)

            frame_paths, duration_seconds = await _extract_video_frame_paths(
                tmp_video,
                file_unique_id=str(video.file_unique_id),
            )
            caption = update.message.caption or ""
            if frame_paths:
                # Reads + base64-encodes every frame: off the event loop (T6).
                content_blocks, memory_text = await asyncio.to_thread(
                    _build_video_content_blocks,
                    frame_paths,
                    caption=caption,
                    durable_video_path=durable_video,
                    transcript=transcript,
                    audio_error=audio_error,
                    duration_seconds=duration_seconds,
                )
                try:
                    # AH7/M19 (2026-06-11): same per-chat ordering as text,
                    # image and document turns — without the lock a concurrent
                    # text turn races this read-modify-write of session state.
                    async with self._chat_lock(session_id):
                        response = await asyncio.to_thread(
                            self._handle_agent_multimodal_sync,
                            user_id=user_id,
                            session_id=session_id,
                            content_blocks=content_blocks,
                            memory_text=memory_text,
                        )
                except Exception:
                    logger.exception("Error handling video message")
                    response = "Error procesando tu video. Intenta de nuevo."
            elif transcript and transcript.strip():
                prefix = f"{caption}\n[Video transcrito]: " if caption else "[Video transcrito]: "
                try:
                    response = await self._handle_agent_text(
                        user_id=user_id,
                        session_id=session_id,
                        text=prefix + transcript,
                    )
                except Exception:
                    logger.exception("Error handling video transcript")
                    response = "Error procesando la transcripcion del video. Intenta de nuevo."
            else:
                response = "No pude extraer frames ni audio del video. Reenvialo como video corto o screenshot."
        finally:
            tmp_video.unlink(missing_ok=True)
            if tmp_audio:
                tmp_audio.unlink(missing_ok=True)

        bot_done_at = time.perf_counter()
        if response is None:
            await self._emit_latency(
                session_id=session_id,
                user_id=user_id,
                message_kind="video",
                status="suppressed",
                bot_ms=(bot_done_at - started_at) * 1000,
                reply_ms=0.0,
                total_ms=(bot_done_at - started_at) * 1000,
                response_parts=0,
                response_chars=0,
            )
            return
        if not response.strip():
            response = "(procesando... intenta de nuevo en unos segundos)"
        response = self._sanitize_outbound_response(session_id, response)
        parts = _split_message(response)
        sent_parts, delivery_ok = await self._send_reply_text_parts(
            update,
            parts,
            session_id=session_id,
            user_id=user_id,
            message_kind="video",
        )
        finished_at = time.perf_counter()
        await self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="video",
            status="ok" if delivery_ok else "send_failed",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=sent_parts,
            response_chars=len(response),
        )

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
        try:
            file = await context.bot.get_file(file_id)
            suffix = _download_suffix(getattr(file, 'file_path', None), mime_type)
            tmp_path = Path(f"/tmp/claw-image-{file_unique_id}{suffix}")
            _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            durable_path = _IMAGES_DIR / f"{file_unique_id}{suffix}"
            await file.download_to_drive(str(tmp_path))
        except Exception:
            # A failed download used to abort the handler in total silence
            # (the voice/document handlers already apologize) — C1.
            logger.exception("Error downloading image message")
            await self._send_reply_text_parts(
                update,
                ["No pude descargar la imagen. Reenvíala e intento de nuevo."],
                session_id=session_id,
                user_id=user_id,
                message_kind="image",
            )
            await self._emit_latency(
                session_id=session_id,
                user_id=user_id,
                message_kind="image",
                status="download_failed",
                bot_ms=0.0,
                reply_ms=0.0,
                total_ms=(time.perf_counter() - started_at) * 1000,
                response_parts=1,
                response_chars=0,
            )
            return
        try:
            import shutil
            shutil.copy2(str(tmp_path), str(durable_path))
        except Exception:
            durable_path = tmp_path
        try:
            # Reads + base64-encodes up to 20MB: off the event loop (T6).
            content_blocks, memory_text = await asyncio.to_thread(
                _build_image_content_blocks,
                tmp_path,
                caption=caption,
                mime_type=mime_type,
                durable_path=durable_path,
            )
            async with self._chat_lock(session_id):
                response = await asyncio.to_thread(
                    self._handle_agent_multimodal_sync,
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
        response = self._sanitize_outbound_response(session_id, response)
        parts = _split_message(response)
        _, delivered = await self._send_reply_text_parts(
            update,
            parts,
            session_id=session_id,
            user_id=user_id,
            message_kind="image",
        )
        finished_at = time.perf_counter()
        await self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="image",
            status="ok" if delivered else "send_failed",
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
            async with self._chat_lock(session_id):
                response = await asyncio.to_thread(
                    self._handle_agent_multimodal_sync,
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
        response = self._sanitize_outbound_response(session_id, response)
        parts = _split_message(response)
        _, delivered = await self._send_reply_text_parts(
            update,
            parts,
            session_id=session_id,
            user_id=user_id,
            message_kind="document",
        )
        finished_at = time.perf_counter()
        await self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="document",
            status="ok" if delivered else "send_failed",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=len(parts),
            response_chars=len(response),
        )

    async def send_photo(self, *, chat_id: int, photo_path: str, caption: str | None = None) -> bool:
        if self._app is None:
            return False
        import tempfile
        resolved = Path(photo_path).resolve()
        allowed_roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/private/tmp"), Path.home())
        if not any(resolved.is_relative_to(root) for root in allowed_roots):
            logger.error("send_photo blocked: %s is outside allowed directories", resolved)
            return False
        try:
            with open(resolved, "rb") as photo:
                await self._app.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption)
        except _NONFATAL_SEND_ERRORS as exc:
            _log_nonfatal_send_error("send_photo", exc)
            return False
        except Exception:
            # A BadRequest here used to blow up the whole text handler
            # (_maybe_send_latest_generated_image) into "Error procesando
            # tu mensaje" (2026-06-12 audit T4).
            logger.warning("send_photo failed", exc_info=True)
            return False
        return True

    async def send_video_url(self, *, chat_id: int, video_url: str, caption: str | None = None) -> None:
        """Send a video by URL to a Telegram chat."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_video(chat_id=chat_id, video=video_url, caption=caption)
        except _NONFATAL_SEND_ERRORS as exc:
            _log_nonfatal_send_error("send_video_url", exc)

    async def send_text(self, *, chat_id: int, text: str, parse_mode: str | None = None) -> bool:
        """Send a proactive text message, split to Telegram's message limit.

        This is the delivery path for task-completion notifications,
        observability alerts and NotebookLM. Returns False when a part
        ultimately failed; later parts are never attempted so the caller
        sees the message as lost instead of delivered with a hole in the
        middle (2026-06-12 audit T1).
        """
        if self._app is None:
            return False
        session_id = f"tg-{chat_id}"
        response = self._sanitize_outbound_response(session_id, text)
        parts = _split_message(response)
        part_count = len(parts)
        for index, part in enumerate(parts, start=1):
            sent = await self._send_proactive_text_part(
                part,
                chat_id=chat_id,
                parse_mode=parse_mode,
                session_id=session_id,
                part_index=index,
                part_count=part_count,
            )
            if not sent:
                return False
        return True

    async def _send_proactive_text_part(
        self,
        part: str,
        *,
        chat_id: int,
        parse_mode: str | None,
        session_id: str,
        part_index: int,
        part_count: int,
    ) -> bool:
        user_id = str(chat_id)
        timeout_kwargs = self._send_text_timeout_kwargs()
        for attempt in range(1, self._text_send_retries + 1):
            self._emit_outbound_text_event_nowait(
                "telegram_outbound_attempt",
                session_id=session_id,
                user_id=user_id,
                message_kind="proactive",
                method="send_message",
                part_index=part_index,
                part_count=part_count,
                part_chars=len(part),
                attempt=attempt,
            )
            try:
                result = await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    parse_mode=parse_mode,
                    link_preview_options=_NO_PREVIEW,
                    **timeout_kwargs,
                )
            except Exception as exc:
                retryable = _is_retryable_text_send_error(exc)
                logger.warning(
                    "Telegram proactive send_message failed%s",
                    "; retrying" if retryable and attempt < self._text_send_retries else "",
                    exc_info=True,
                )
                await self._emit_outbound_text_event(
                    "telegram_outbound_error",
                    session_id=session_id,
                    user_id=user_id,
                    message_kind="proactive",
                    method="send_message",
                    part_index=part_index,
                    part_count=part_count,
                    part_chars=len(part),
                    attempt=attempt,
                    error=exc,
                )
                if not retryable:
                    break
                if attempt < self._text_send_retries:
                    await self._sleep_before_text_send_retry(exc, attempt)
            else:
                message_id = getattr(result, "message_id", None)
                await self._emit_outbound_text_event(
                    "telegram_outbound_sent",
                    session_id=session_id,
                    user_id=user_id,
                    message_kind="proactive",
                    method="send_message",
                    part_index=part_index,
                    part_count=part_count,
                    part_chars=len(part),
                    attempt=attempt,
                    message_id=message_id if isinstance(message_id, int) else None,
                )
                return True

        # The direct Bot API fallback sends plain text only; with a
        # parse_mode the silently-unformatted message could change meaning,
        # so report the loss instead.
        if parse_mode is not None:
            return False
        return await self._send_text_direct_bot_api(
            part,
            chat_id=chat_id,
            session_id=session_id,
            user_id=user_id,
            message_kind="proactive",
            part_index=part_index,
            part_count=part_count,
        )

    async def _handle_text_content(
        self,
        update: Update,
        text: str,
        *,
        force_voice_reply: bool = False,
    ) -> None:
        user_id = str(update.effective_user.id)
        session_id = f"tg-{update.effective_chat.id}"
        started_at = time.perf_counter()
        try:
            response = await self._handle_agent_text(user_id=user_id, session_id=session_id, text=text)
        except Exception:
            logger.exception("Error handling voice message")
            response = "Error processing your voice message."
        bot_done_at = time.perf_counter()
        if response is None:
            await self._emit_latency(
                session_id=session_id,
                user_id=user_id,
                message_kind="transcript",
                status="suppressed",
                bot_ms=(bot_done_at - started_at) * 1000,
                reply_ms=0.0,
                total_ms=(bot_done_at - started_at) * 1000,
                response_parts=0,
                response_chars=0,
            )
            return
        response = self._sanitize_outbound_response(session_id, response)
        parts = _split_message(response)
        voice_name = self._bot_service.is_voice_mode(session_id)
        reply_as_voice = force_voice_reply or bool(voice_name)
        if reply_as_voice and (self._voice_api_key or self._xai_api_key):
            try:
                await _maybe_send_chat_action(update.message, "record_voice")
                ogg_path = await synthesize_voice_note(
                    response,
                    api_key=self._voice_api_key,
                    voice=voice_name or "alloy",
                    xai_api_key=self._xai_api_key,
                    prefer_realtime=force_voice_reply and bool(self._voice_api_key),
                )
                try:
                    with open(ogg_path, "rb") as f:
                        await update.message.reply_voice(voice=f)
                finally:
                    ogg_path.unlink(missing_ok=True)
            except Exception:
                logger.warning("TTS failed, falling back to text", exc_info=True)
                _, delivered = await self._send_reply_text_parts(
                    update,
                    parts,
                    session_id=session_id,
                    user_id=user_id,
                    message_kind="transcript",
                )
            else:
                delivered = True
        else:
            _, delivered = await self._send_reply_text_parts(
                update,
                parts,
                session_id=session_id,
                user_id=user_id,
                message_kind="transcript",
            )
        finished_at = time.perf_counter()
        await self._emit_latency(
            session_id=session_id,
            user_id=user_id,
            message_kind="transcript",
            status="ok" if delivered else "send_failed",
            bot_ms=(bot_done_at - started_at) * 1000,
            reply_ms=(finished_at - bot_done_at) * 1000,
            total_ms=(finished_at - started_at) * 1000,
            response_parts=len(parts),
            response_chars=len(response),
        )

    def _sanitize_outbound_response(self, session_id: str, response: str) -> str:
        sanitized = _sanitize_chat_response(response)
        if sanitized != response:
            self._emit_transport_event(
                "internal_message_suppressed_from_chat",
                payload={
                    "session_id": session_id,
                    "reason": "telegram_outbound_sanitizer",
                    "original_length": len(response),
                    "sanitized_length": len(sanitized),
                },
            )
        return sanitized

    async def _handle_agent_text(
        self,
        *,
        user_id: str,
        session_id: str,
        text: str,
        context_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if _is_interrupt_command(text):
            # Operator interrupts (/freeze, /approve, /status, ...) must not
            # queue behind a long-running turn of the same chat.
            return await asyncio.to_thread(
                self._handle_agent_text_sync,
                user_id,
                session_id,
                text,
                context_metadata,
            )
        async with self._chat_lock(session_id):
            return await asyncio.to_thread(
                self._handle_agent_text_sync,
                user_id,
                session_id,
                text,
                context_metadata,
            )

    def _handle_agent_text_sync(
        self,
        user_id: str,
        session_id: str,
        text: str,
        context_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        if self._agent_runtime is None:
            kwargs = {
                "user_id": user_id,
                "session_id": session_id,
                "text": text,
                "runtime_channel": "telegram",
            }
            if context_metadata is not None:
                kwargs["context_metadata"] = context_metadata
            return self._bot_service.handle_text(**kwargs)
        external_session_id = session_id.removeprefix("tg-")
        kwargs = {
            "channel": "telegram",
            "external_user_id": user_id,
            "external_session_id": external_session_id,
            "session_id": session_id,
            "text": text,
        }
        if context_metadata is not None:
            kwargs["metadata"] = context_metadata
        response = self._agent_runtime.handle_text(**kwargs)
        return response.text

    def _handle_agent_multimodal_sync(
        self,
        user_id: str,
        session_id: str,
        content_blocks: list[dict[str, Any]],
        memory_text: str,
    ) -> str:
        if self._agent_runtime is None:
            return self._bot_service.handle_multimodal(
                user_id=user_id,
                session_id=session_id,
                content_blocks=content_blocks,
                memory_text=memory_text,
            )
        external_session_id = session_id.removeprefix("tg-")
        response = self._agent_runtime.handle_multimodal(
            channel="telegram",
            external_user_id=user_id,
            external_session_id=external_session_id,
            session_id=session_id,
            content_blocks=content_blocks,
            memory_text=memory_text,
        )
        return response.text

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
