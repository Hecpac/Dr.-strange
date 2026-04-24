from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from claw_v2.chrome import ManagedChrome
from claw_v2.chat_api import LocalChatAPI
from claw_v2.main import build_runtime
from claw_v2.notebooklm import NotebookLMService
from claw_v2.telegram import TelegramTransport
from claw_v2.web_transport import WebTransport

logger = logging.getLogger(__name__)

_DEFAULT_PID_PATH = Path.home() / ".claw" / "claw.pid"
_STARTED_AT_PATH = Path.home() / ".claw" / "started_at.txt"
_RESTART_MARKER_PATH = Path.home() / ".claw" / "restart_requested.json"
_LIVENESS_STAMP_PATH = Path.home() / ".claw" / "liveness_last_sent.txt"
_LIVENESS_HOUR = 9


def _format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def load_soul(soul_path: Path | None = None) -> str:
    if soul_path is None:
        soul_path = Path(__file__).parent / "SOUL.md"
    if soul_path.exists():
        return soul_path.read_text(encoding="utf-8")
    return "You are Claw."


def should_send_fitness_reminder(now: datetime, stamp_path: Path) -> bool:
    if now.hour != 5:
        return False
    today_key = now.strftime("%Y-%m-%d")
    if stamp_path.exists() and stamp_path.read_text().strip() == today_key:
        return False
    return True


class PidLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PID_PATH

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                existing_pid = int(self.path.read_text().strip())
                os.kill(existing_pid, 0)
                print(f"Claw is already running (pid {existing_pid}).", file=sys.stderr)
                raise SystemExit(1)
            except (ValueError, ProcessLookupError, PermissionError):
                self.path.unlink(missing_ok=True)
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


async def run() -> int:
    pid_lock = PidLock()
    pid_lock.acquire()
    _STARTED_AT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STARTED_AT_PATH.write_text(str(int(time.time())), encoding="utf-8")
    try:
        system_prompt = load_soul()
        runtime = build_runtime(system_prompt=system_prompt)
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, shutdown.set)

        transport = TelegramTransport(
            bot_service=runtime.bot,
            token=runtime.config.telegram_bot_token,
            allowed_user_id=runtime.config.telegram_allowed_user_id,
            voice_api_key=runtime.config.openai_api_key,
        )
        web_transport = WebTransport(
            chat_api=LocalChatAPI(
                bot_service=runtime.bot,
                default_user_id=runtime.config.telegram_allowed_user_id,
                auth_token=runtime.config.web_chat_token,
            ),
            host=runtime.config.web_chat_host,
            port=runtime.config.web_chat_port,
        )

        try:
            await transport.start()
        except Exception:
            logger.error("Telegram transport failed to start", exc_info=True)
            raise
        if runtime.config.web_chat_enabled:
            await web_transport.start()

        # Wire NotebookLM with Telegram notify callback
        _loop = asyncio.get_running_loop()

        def _send_telegram(text: str, *, parse_mode: str | None = None) -> None:
            if not (runtime.config.telegram_allowed_user_id and transport._app):
                return
            kwargs = {"chat_id": int(runtime.config.telegram_allowed_user_id), "text": text}
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            asyncio.run_coroutine_threadsafe(
                transport._app.bot.send_message(**kwargs),
                _loop,
            )

        if _RESTART_MARKER_PATH.exists():
            try:
                marker = json.loads(_RESTART_MARKER_PATH.read_text(encoding="utf-8"))
                reason = marker.get("reason", "unknown")
                requested_at = marker.get("requested_at", "-")
                _send_telegram(
                    f"✅ Restart completado (motivo: {reason}, solicitado: {requested_at})"
                )
            except Exception:
                logger.warning("Failed to process restart marker", exc_info=True)
            finally:
                _RESTART_MARKER_PATH.unlink(missing_ok=True)

        def _nlm_notify(message: str) -> None:
            if runtime.config.telegram_allowed_user_id and transport._app:
                asyncio.run_coroutine_threadsafe(
                    transport._app.bot.send_message(
                        chat_id=int(runtime.config.telegram_allowed_user_id),
                        text=message,
                    ),
                    _loop,
                )

        nlm_service = NotebookLMService(notify=_nlm_notify, observe=runtime.observe, jobs=runtime.jobs)
        runtime.bot.notebooklm = nlm_service

        # NotebookLM → Wiki sync (every 12h)
        if runtime.bot.wiki is not None:
            from claw_v2.cron import ScheduledJob
            _wiki_ref = runtime.bot.wiki
            _nlm_ref = nlm_service
            runtime.scheduler.register(ScheduledJob(
                name="nlm_wiki_sync",
                interval_seconds=43200,
                handler=lambda: _wiki_ref.ingest_from_notebooklm(_nlm_ref),
            ))
            # Also let Kairos trigger it on demand
            runtime.kairos.nlm_service = _nlm_ref

        # Daily fitness reminder at ~5 AM
        import random as _rnd
        _ROUTINES = {
            0: ("Pecho / Hombro / Tríceps",
                "Bench Press 4x6-8 | Incline DB Press 3x8-10 | Cable Fly 3x12-15 | "
                "Seated DB Press 4x8-10 | Lateral Raise 4x12-15 | "
                "Overhead Cable Ext 3x12-15 | Tricep Pushdown 3x15"),
            1: ("Espalda / Bíceps",
                "Deadlift 4x5-6 | Pull-ups 4x6-10 | Barbell Row 3x8-10 | "
                "Seated Cable Row 3x10-12 | Face Pull 3x15 | "
                "Barbell Curl 3x10-12 | Hammer Curl 3x12-15"),
            2: ("Piernas",
                "Squat 4x6-8 | Romanian Deadlift 3x8-10 | Leg Press 3x10-12 | "
                "Walking Lunges 3x12/pierna | Leg Curl 3x12-15 | Calf Raise 4x15-20"),
            3: ("Upper Body (volumen)",
                "Incline BB Press 4x8-10 | DB Row 3x10-12 | Dips 3xfallo | "
                "Lat Pulldown 3x10-12 | Lateral Raise cable 4x15 | "
                "Reverse Pec Deck 3x15 | Superset Curl+Pushdown 3x12"),
            4: ("Piernas + Core",
                "Front Squat 4x8-10 | Bulgarian Split 3x10/pierna | Hip Thrust 4x10-12 | "
                "Leg Extension 3x15 | Seated Calf 4x15-20 | "
                "Hanging Leg Raise 3x15 | Cable Woodchop 3x12/lado"),
        }
        _QUOTES = [
            "El dolor es temporal. Rendirse es para siempre.",
            "No entrenas para hoy. Entrenas para los próximos 40 años.",
            "La disciplina supera a la motivación. Todos los días.",
            "Tu cuerpo puede soportar casi todo. Es tu mente la que hay que convencer.",
            "Cada rep cuenta. Cada día cuenta. Sin excusas.",
            "El mejor momento para empezar fue ayer. El segundo mejor es ahora.",
            "No busques fácil. Busca que valga la pena.",
            "La consistencia le gana al talento cuando el talento no es consistente.",
            "Sé la versión más fuerte de ti mismo.",
            "Los resultados llegan cuando dejas de buscar atajos.",
        ]

        _FITNESS_STAMP = Path.home() / ".claw" / "fitness_last_sent.txt"

        def _fitness_reminder() -> None:
            now = datetime.now()
            today_key = now.strftime("%Y-%m-%d")
            if not should_send_fitness_reminder(now, _FITNESS_STAMP):
                return
            weekday = now.weekday()  # 0=Mon, 6=Sun
            if weekday >= 5:  # Sat/Sun = rest
                return
            _FITNESS_STAMP.parent.mkdir(parents=True, exist_ok=True)
            _FITNESS_STAMP.write_text(today_key)
            name, exercises = _ROUTINES[weekday]
            quote = _rnd.choice(_QUOTES)
            msg = (
                f"💪 Buenos días, Hector!\n\n"
                f"📋 Hoy toca: *{name}*\n\n"
                f"{exercises}\n\n"
                f"🥩 Proteína: mínimo 150g hoy\n\n"
                f"🔥 _{quote}_"
            )
            if runtime.config.telegram_allowed_user_id and transport._app:
                asyncio.run_coroutine_threadsafe(
                    transport._app.bot.send_message(
                        chat_id=int(runtime.config.telegram_allowed_user_id),
                        text=msg,
                        parse_mode="Markdown",
                    ),
                    _loop,
                )

        from claw_v2.cron import ScheduledJob as _SJ
        runtime.scheduler.register(_SJ(
            name="fitness_reminder",
            interval_seconds=300,
            handler=_fitness_reminder,
        ))

        def _daily_liveness_report() -> None:
            now = datetime.now()
            if now.hour != _LIVENESS_HOUR:
                return
            today_key = now.strftime("%Y-%m-%d")
            if (
                _LIVENESS_STAMP_PATH.exists()
                and _LIVENESS_STAMP_PATH.read_text(encoding="utf-8").strip() == today_key
            ):
                return
            _LIVENESS_STAMP_PATH.parent.mkdir(parents=True, exist_ok=True)
            _LIVENESS_STAMP_PATH.write_text(today_key, encoding="utf-8")

            uptime_str = "desconocido"
            try:
                started_ts = int(_STARTED_AT_PATH.read_text(encoding="utf-8").strip())
                uptime_str = _format_uptime(int(time.time()) - started_ts)
            except Exception:
                pass

            pending = "?"
            try:
                pending = str(runtime.heartbeat.collect().pending_approvals)
            except Exception:
                pass

            cost_str = "n/a"
            try:
                snapshot = runtime.metrics.snapshot()
                cost_today = sum(
                    float(entry.get("cost", 0.0))
                    for entry in snapshot.values()
                    if isinstance(entry, dict)
                )
                cost_str = f"${cost_today:.2f}"
            except Exception:
                pass

            cap_parts = []
            for cap in ("chrome_cdp", "computer_use"):
                status = runtime.bot._capability_status.get(cap, {})
                available = status.get("available", True)
                cap_parts.append(f"{cap}={'OK' if available else 'degraded'}")

            msg = (
                f"✅ Claw online — {now.strftime('%Y-%m-%d %H:%M')}\n\n"
                f"• Uptime: {uptime_str}\n"
                f"• Approvals pendientes: {pending}\n"
                f"• Costo hoy: {cost_str}\n"
                f"• Capacidades: {' | '.join(cap_parts)}"
            )
            _send_telegram(msg)

        runtime.scheduler.register(_SJ(
            name="daily_liveness",
            interval_seconds=300,
            handler=_daily_liveness_report,
        ))

        # Wire ManagedChrome
        managed_chrome = None
        if runtime.config.chrome_cdp_enabled and runtime.config.browse_backend in {"auto", "chrome_cdp"}:
            try:
                managed_chrome = ManagedChrome(port=runtime.config.claw_chrome_port)
                managed_chrome.start()
                runtime.bot.set_capability_status("chrome_cdp", available=True)
            except Exception:
                logger.warning("ManagedChrome failed to start, CDP features disabled", exc_info=True)
                runtime.bot.set_capability_status(
                    "chrome_cdp",
                    available=False,
                    reason=(
                        f"Chrome no pudo iniciar en el puerto {runtime.config.claw_chrome_port}; "
                        "la navegación autenticada queda temporalmente desactivada."
                    ),
                )
                managed_chrome = None
        runtime.bot.managed_chrome = managed_chrome

        # Re-wire BrowserUseService with managed CDP URL
        if managed_chrome is not None:
            from claw_v2.computer import BrowserUseService
            runtime.bot.browser_use = BrowserUseService(cdp_url=managed_chrome.cdp_url)

        def _telegram_health_check() -> None:
            if not transport.is_polling_healthy(stale_seconds=600.0):
                logger.warning("Telegram polling stale, scheduling restart")
                asyncio.run_coroutine_threadsafe(transport.restart_polling(), _loop)

        runtime.scheduler.register(_SJ(
            name="telegram_health",
            interval_seconds=120,
            handler=_telegram_health_check,
        ))

        try:
            await runtime.daemon.run_loop(shutdown)
        finally:
            if managed_chrome is not None:
                managed_chrome.stop()
            await web_transport.stop()
            await transport.stop()
    finally:
        pid_lock.release()
    return 0
