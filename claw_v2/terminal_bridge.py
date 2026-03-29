from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable


class TerminalBridgeError(RuntimeError):
    """Raised when terminal bridge operations fail."""


PopenFactory = Callable[..., subprocess.Popen]

DEFAULT_TERMINAL_BRIDGE_ROOT = Path.home() / ".claw" / "terminal_bridge"


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TerminalBridgeService:
    def __init__(
        self,
        *,
        root: Path | str | None = None,
        popen_factory: PopenFactory | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else DEFAULT_TERMINAL_BRIDGE_ROOT
        self._popen = popen_factory or subprocess.Popen

    def open(self, tool: str, *, cwd: str | Path | None = None, args: list[str] | None = None) -> dict[str, Any]:
        command = self._resolve_command(tool, args=args)
        return self.open_command(tool=tool, command=command, cwd=cwd)

    def open_command(
        self,
        *,
        tool: str,
        command: list[str],
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        working_dir = Path(cwd) if cwd is not None else Path.home()
        working_dir = working_dir.expanduser().resolve(strict=False)
        if not working_dir.exists() or not working_dir.is_dir():
            raise TerminalBridgeError(f"cwd does not exist or is not a directory: {working_dir}")

        session_id = uuid.uuid4().hex[:12]
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=False)
        input_path = session_dir / "input.queue"
        output_path = session_dir / "output.log"
        error_path = session_dir / "runner.stderr.log"
        input_path.write_bytes(b"")
        output_path.write_bytes(b"")
        error_path.write_text("", encoding="utf-8")

        meta = {
            "session_id": session_id,
            "tool": tool,
            "command": command,
            "cwd": str(working_dir),
            "status": "starting",
            "created_at": _utc_timestamp(),
            "updated_at": _utc_timestamp(),
            "runner_pid": None,
            "child_pid": None,
            "return_code": None,
            "input_path": str(input_path),
            "output_path": str(output_path),
            "error_path": str(error_path),
        }
        self._write_meta(session_dir, meta)

        stderr_handle = error_path.open("a", encoding="utf-8")
        try:
            runner = self._popen(
                [sys.executable, "-m", "claw_v2.terminal_bridge", "__run_session", str(session_dir)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                cwd=str(working_dir),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            stderr_handle.close()
        meta["runner_pid"] = runner.pid
        meta["updated_at"] = _utc_timestamp()
        self._write_meta(session_dir, meta)
        return meta

    def list_sessions(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        sessions: list[dict[str, Any]] = []
        for session_dir in sorted(self.root.iterdir()):
            if not session_dir.is_dir():
                continue
            try:
                meta = self._read_meta(session_dir)
            except FileNotFoundError:
                continue
            meta["alive"] = self._session_alive(meta)
            sessions.append(meta)
        return sessions

    def status(self, session_id: str) -> dict[str, Any]:
        session_dir = self._session_dir(session_id)
        meta = self._read_meta(session_dir)
        meta["alive"] = self._session_alive(meta)
        return meta

    def send(self, session_id: str, text: str, *, append_newline: bool = True) -> dict[str, Any]:
        session_dir = self._session_dir(session_id)
        meta = self._read_meta(session_dir)
        payload = text if not append_newline or text.endswith("\n") else f"{text}\n"
        raw = payload.encode("utf-8")
        with (session_dir / "input.queue").open("ab") as handle:
            handle.write(raw)
        meta["updated_at"] = _utc_timestamp()
        self._write_meta(session_dir, meta)
        return {"session_id": session_id, "bytes_written": len(raw)}

    def read(self, session_id: str, *, offset: int = 0, limit: int = 4000) -> dict[str, Any]:
        session_dir = self._session_dir(session_id)
        output_path = session_dir / "output.log"
        raw = output_path.read_bytes()
        if offset < 0:
            offset = 0
        chunk = raw[offset : offset + limit]
        return {
            "session_id": session_id,
            "offset": offset,
            "next_offset": offset + len(chunk),
            "output": chunk.decode("utf-8", errors="replace"),
        }

    def close(self, session_id: str, *, force: bool = False) -> dict[str, Any]:
        session_dir = self._session_dir(session_id)
        meta = self._read_meta(session_dir)
        sig = signal.SIGKILL if force else signal.SIGTERM
        child_pid = meta.get("child_pid")
        runner_pid = meta.get("runner_pid")

        if isinstance(child_pid, int):
            try:
                os.killpg(child_pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError:
                os.kill(child_pid, sig)

        if isinstance(runner_pid, int):
            try:
                os.kill(runner_pid, sig)
            except ProcessLookupError:
                pass

        meta["status"] = "closing"
        meta["updated_at"] = _utc_timestamp()
        self._write_meta(session_dir, meta)
        return {"session_id": session_id, "status": "closing"}

    def _resolve_command(self, tool: str, *, args: list[str] | None = None) -> list[str]:
        normalized = tool.strip().lower()
        extra_args = list(args or [])
        if normalized == "claude":
            executable = os.getenv("CLAUDE_CLI_PATH") or shutil.which("claude")
        elif normalized == "codex":
            executable = os.getenv("CODEX_CLI_PATH") or shutil.which("codex")
        else:
            raise TerminalBridgeError("tool must be one of: claude, codex")
        if not executable:
            raise TerminalBridgeError(f"{normalized} CLI not found")
        return [str(Path(executable).expanduser()), *extra_args]

    def _session_dir(self, session_id: str) -> Path:
        session_dir = self.root / session_id
        if not session_dir.exists():
            raise FileNotFoundError(session_dir)
        return session_dir

    @staticmethod
    def _meta_path(session_dir: Path) -> Path:
        return session_dir / "meta.json"

    def _read_meta(self, session_dir: Path) -> dict[str, Any]:
        return json.loads(self._meta_path(session_dir).read_text(encoding="utf-8"))

    def _write_meta(self, session_dir: Path, meta: dict[str, Any]) -> None:
        self._meta_path(session_dir).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _session_alive(meta: dict[str, Any]) -> bool:
        child_pid = meta.get("child_pid")
        if not isinstance(child_pid, int):
            return False
        try:
            os.kill(child_pid, 0)
        except OSError:
            return False
        return True


def run_session(session_dir: Path | str) -> int:
    path = Path(session_dir)
    service = TerminalBridgeService(root=path.parent)
    meta = service._read_meta(path)
    input_path = path / "input.queue"
    output_path = path / "output.log"

    master_fd, slave_fd = pty.openpty()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    output_handle = output_path.open("ab", buffering=0)
    input_offset = 0
    child: subprocess.Popen[Any] | None = None

    try:
        child = subprocess.Popen(
            list(meta["command"]),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=meta["cwd"],
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        meta["status"] = "running"
        meta["runner_pid"] = os.getpid()
        meta["child_pid"] = child.pid
        meta["started_at"] = _utc_timestamp()
        meta["updated_at"] = _utc_timestamp()
        service._write_meta(path, meta)
        while True:
            input_offset = _pump_input(input_path, master_fd, input_offset)
            _pump_output(master_fd, output_handle, timeout=0.1)
            return_code = child.poll()
            if return_code is not None:
                _drain_output(master_fd, output_handle)
                meta["status"] = "exited"
                meta["return_code"] = return_code
                meta["ended_at"] = _utc_timestamp()
                meta["updated_at"] = _utc_timestamp()
                service._write_meta(path, meta)
                return return_code
    except Exception as exc:
        meta["status"] = "error"
        meta["return_code"] = child.poll() if child is not None else None
        meta["error"] = str(exc)
        meta["ended_at"] = _utc_timestamp()
        meta["updated_at"] = _utc_timestamp()
        service._write_meta(path, meta)
        raise
    finally:
        output_handle.close()
        try:
            os.close(slave_fd)
        except OSError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


def _pump_input(input_path: Path, master_fd: int, input_offset: int) -> int:
    size = input_path.stat().st_size
    if size <= input_offset:
        return input_offset
    with input_path.open("rb") as handle:
        handle.seek(input_offset)
        payload = handle.read(size - input_offset)
    if payload:
        os.write(master_fd, payload)
    return size


def _pump_output(master_fd: int, output_handle: Any, *, timeout: float) -> None:
    ready, _, _ = select.select([master_fd], [], [], timeout)
    if master_fd not in ready:
        return
    try:
        chunk = os.read(master_fd, 4096)
    except OSError:
        return
    if chunk:
        output_handle.write(chunk)


def _drain_output(master_fd: int, output_handle: Any) -> None:
    while True:
        ready, _, _ = select.select([master_fd], [], [], 0.0)
        if master_fd not in ready:
            return
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        output_handle.write(chunk)


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) == 2 and args[0] == "__run_session":
        session_dir = Path(args[1])
        try:
            return run_session(session_dir)
        except Exception:
            error_path = session_dir / "runner.stderr.log"
            error_path.parent.mkdir(parents=True, exist_ok=True)
            with error_path.open("a", encoding="utf-8") as handle:
                handle.write(traceback.format_exc())
                handle.write("\n")
            return 1
    raise SystemExit("terminal_bridge is an internal module; use claw_v2.terminal_bridge_cli")


if __name__ == "__main__":
    raise SystemExit(main())
