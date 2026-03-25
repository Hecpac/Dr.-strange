from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from claw_v2.main import build_runtime
from claw_v2.telegram import TelegramTransport

logger = logging.getLogger(__name__)

_DEFAULT_PID_PATH = Path.home() / ".claw" / "claw.pid"


def load_soul(soul_path: Path | None = None) -> str:
    if soul_path is None:
        soul_path = Path(__file__).parent / "SOUL.md"
    if soul_path.exists():
        return soul_path.read_text(encoding="utf-8")
    return "You are Claw."


class PidLock:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PID_PATH

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                existing_pid = int(self.path.read_text().strip())
                os.kill(existing_pid, 0)
                print(f"Claw is already running (pid {existing_pid}).", file=sys.stderr)
                raise SystemExit(1)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
        self.path.write_text(str(os.getpid()))

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


async def run() -> int:
    pid_lock = PidLock()
    pid_lock.acquire()
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

        await transport.start()
        try:
            await runtime.daemon.run_loop(shutdown)
        finally:
            await transport.stop()
    finally:
        pid_lock.release()
    return 0
