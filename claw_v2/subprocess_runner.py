from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any


_COMMUNICATE_POLL_SECONDS = 0.1
_SENSITIVE_ARG_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "github_token",
    "openai_api_key",
    "password",
    "secret",
    "token",
)


def _is_sensitive_arg(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return any(marker in normalized for marker in _SENSITIVE_ARG_MARKERS)


def _redact_command(args: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            redacted.append("REDACTED")
            redact_next = False
            continue
        if _is_sensitive_arg(arg):
            if "=" in arg:
                key, _sep, _value = arg.partition("=")
                redacted.append(f"{key}=REDACTED")
            else:
                redacted.append("REDACTED")
                redact_next = True
            continue
        redacted.append(arg)
    return redacted


def _truncate_output(value: str, max_chars: int) -> str:
    if max_chars <= 0 or len(value) <= max_chars:
        return value
    # Paso 4 (2026-06-12): unified marker format ("kept X of Y chars"),
    # keeping this site's tail-preserving semantics ("last").
    marker = f"\n[truncated: kept last {max_chars} of {len(value)} chars]\n"
    keep = max(0, max_chars - len(marker))
    return marker + value[-keep:]


def _is_truncated(value: str, max_chars: int) -> bool:
    return max_chars > 0 and len(value) > max_chars


def _kill_process_group(proc: subprocess.Popen[str], *, kill_process_group: bool) -> bool:
    killed = False
    if kill_process_group:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            killed = True
        except ProcessLookupError:
            return killed
        except (OSError, PermissionError):
            pass
    if not killed:
        try:
            proc.terminate()
            killed = True
        except ProcessLookupError:
            return killed
    return killed


def _force_kill_process_group(proc: subprocess.Popen[str], *, kill_process_group: bool) -> bool:
    killed = False
    if kill_process_group:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            killed = True
        except ProcessLookupError:
            return killed
        except (OSError, PermissionError):
            pass
    if not killed:
        try:
            proc.kill()
            killed = True
        except ProcessLookupError:
            return killed
    return killed


def _emit(observe: Any | None, event_type: str, payload: dict[str, Any]) -> None:
    if observe is None:
        return
    try:
        observe.emit(event_type, payload=payload)
    except Exception:
        pass


def run_subprocess_bounded(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout_s: float,
    max_output_chars: int = 20_000,
    kill_process_group: bool = True,
    check: bool = False,
    env: Mapping[str, str] | None = None,
    observe: Any | None = None,
    cancel_event: threading.Event | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [str(arg) for arg in args]
    event_command = _redact_command(command)
    start = time.monotonic()
    _emit(
        observe,
        "subprocess_started",
        {
            "args": event_command,
            "cwd": str(cwd) if cwd is not None else None,
            "timeout_s": timeout_s,
            "kill_process_group": kill_process_group,
        },
    )
    proc = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=kill_process_group,
    )
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise subprocess.TimeoutExpired(command, time.monotonic() - start)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_s)
            try:
                stdout, stderr = proc.communicate(
                    timeout=min(_COMMUNICATE_POLL_SECONDS, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired as exc:
        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled:
            _emit(
                observe,
                "subprocess_cancelled",
                {
                    "args": event_command,
                    "cwd": str(cwd) if cwd is not None else None,
                    "timeout_s": timeout_s,
                    "elapsed_s": round(time.monotonic() - start, 3),
                    "cancelled": True,
                },
            )
        _emit(
            observe,
            "subprocess_timeout",
            {
                "args": event_command,
                "cwd": str(cwd) if cwd is not None else None,
                "timeout_s": timeout_s,
                "elapsed_s": round(time.monotonic() - start, 3),
                "cancelled": cancelled,
                "timed_out": True,
            },
        )
        terminated = _kill_process_group(proc, kill_process_group=kill_process_group)
        try:
            stdout, stderr = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            killed = _force_kill_process_group(proc, kill_process_group=kill_process_group)
            _emit(
                observe,
                    "subprocess_killed",
                    {
                        "args": event_command,
                        "cwd": str(cwd) if cwd is not None else None,
                        "timeout_s": timeout_s,
                        "elapsed_s": round(time.monotonic() - start, 3),
                        "terminated": terminated,
                        "killed": killed,
                        "cancelled": cancelled,
                        "timed_out": True,
                    },
                )
            # LOW (2026-06-12): even post-SIGKILL, communicate() can hang
            # forever when a surviving grandchild holds the pipes open.
            # PR #95 review: keep whatever partial output the exception
            # captured instead of discarding it.
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired as drain_exc:
                stdout = drain_exc.stdout or ""
                stderr = drain_exc.stderr or ""
        else:
            _emit(
                observe,
                    "subprocess_killed",
                    {
                        "args": event_command,
                        "cwd": str(cwd) if cwd is not None else None,
                        "timeout_s": timeout_s,
                        "elapsed_s": round(time.monotonic() - start, 3),
                        "terminated": terminated,
                        "killed": False,
                        "cancelled": cancelled,
                        "timed_out": True,
                    },
                )
        exc.output = _truncate_output(stdout or "", max_output_chars)
        exc.stderr = _truncate_output(stderr or "", max_output_chars)
        raise exc

    raw_stdout = stdout or ""
    raw_stderr = stderr or ""
    stdout_truncated = _is_truncated(raw_stdout, max_output_chars)
    stderr_truncated = _is_truncated(raw_stderr, max_output_chars)
    stdout = _truncate_output(raw_stdout, max_output_chars)
    stderr = _truncate_output(raw_stderr, max_output_chars)
    completed = subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)
    _emit(
        observe,
        "subprocess_finished",
        {
            "args": event_command,
            "cwd": str(cwd) if cwd is not None else None,
            "returncode": proc.returncode,
            "elapsed_s": round(time.monotonic() - start, 3),
            "timed_out": False,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_chars": len(raw_stdout),
            "stderr_chars": len(raw_stderr),
        },
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


async def run_subprocess_bounded_off_loop(
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout_s: float,
    max_output_chars: int = 20_000,
    kill_process_group: bool = True,
    check: bool = False,
    env: Mapping[str, str] | None = None,
    observe: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    cancel_event = threading.Event()
    worker = asyncio.create_task(
        asyncio.to_thread(
            partial(
                run_subprocess_bounded,
                args,
                cwd=cwd,
                timeout_s=timeout_s,
                max_output_chars=max_output_chars,
                kill_process_group=kill_process_group,
                check=check,
                env=env,
                observe=observe,
                cancel_event=cancel_event,
            )
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(worker), timeout=3)
        raise
