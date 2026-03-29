from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

from claw_v2 import terminal_bridge_cli
from claw_v2.terminal_bridge import TerminalBridgeService, _pump_input, run_session


def _write_session(
    root: Path,
    session_id: str,
    *,
    tool: str = "codex",
    command: list[str] | None = None,
    cwd: str | None = None,
    status: str = "running",
    child_pid: int | None = None,
    runner_pid: int | None = None,
) -> Path:
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=False)
    (session_dir / "input.queue").write_bytes(b"")
    (session_dir / "output.log").write_bytes(b"")
    (session_dir / "runner.stderr.log").write_text("", encoding="utf-8")
    meta = {
        "session_id": session_id,
        "tool": tool,
        "command": command or [f"/usr/local/bin/{tool}"],
        "cwd": cwd or str(root),
        "status": status,
        "created_at": "2026-03-28T00:00:00Z",
        "updated_at": "2026-03-28T00:00:00Z",
        "runner_pid": runner_pid,
        "child_pid": child_pid,
        "return_code": None,
        "input_path": str(session_dir / "input.queue"),
        "output_path": str(session_dir / "output.log"),
        "error_path": str(session_dir / "runner.stderr.log"),
    }
    (session_dir / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return session_dir


class TerminalBridgeServiceTests(unittest.TestCase):
    def test_open_creates_session_and_spawns_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            calls: list[dict[str, object]] = []

            def popen_factory(cmd, stdin, stdout, stderr, cwd, start_new_session, close_fds):
                calls.append(
                    {
                        "cmd": cmd,
                        "stdin": stdin,
                        "stdout": stdout,
                        "stderr": stderr,
                        "cwd": cwd,
                        "start_new_session": start_new_session,
                        "close_fds": close_fds,
                    }
                )
                return SimpleNamespace(pid=4321)

            with mock.patch.dict(os.environ, {"CODEX_CLI_PATH": "/opt/homebrew/bin/codex"}, clear=False):
                service = TerminalBridgeService(root=root, popen_factory=popen_factory)
                meta = service.open("codex", cwd=workspace)

            self.assertEqual(meta["tool"], "codex")
            self.assertEqual(meta["command"], ["/opt/homebrew/bin/codex"])
            self.assertEqual(meta["runner_pid"], 4321)
            session_dir = root / meta["session_id"]
            self.assertTrue(session_dir.exists())
            stored = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["runner_pid"], 4321)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                calls[0]["cmd"][:3],
                [sys.executable, "-m", "claw_v2.terminal_bridge"],
            )
            self.assertEqual(calls[0]["cmd"][3], "__run_session")
            self.assertEqual(calls[0]["cmd"][4], str(session_dir))
            self.assertEqual(calls[0]["cwd"], str(workspace.resolve()))
            self.assertTrue(calls[0]["start_new_session"])
            self.assertTrue(calls[0]["close_fds"])

    def test_send_read_list_and_close_use_session_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session_dir = _write_session(root, "sess-1", status="running")
            (session_dir / "output.log").write_text("hello world", encoding="utf-8")
            service = TerminalBridgeService(root=root)

            written = service.send("sess-1", "ping")
            self.assertEqual(written, {"session_id": "sess-1", "bytes_written": 5})
            self.assertEqual((session_dir / "input.queue").read_bytes(), b"ping\n")

            chunk = service.read("sess-1", offset=6, limit=5)
            self.assertEqual(chunk["output"], "world")
            self.assertEqual(chunk["next_offset"], 11)

            listing = service.list_sessions()
            self.assertEqual(len(listing), 1)
            self.assertEqual(listing[0]["session_id"], "sess-1")
            self.assertFalse(listing[0]["alive"])

            status = service.status("sess-1")
            self.assertEqual(status["session_id"], "sess-1")
            self.assertFalse(status["alive"])

            closed = service.close("sess-1")
            self.assertEqual(closed, {"session_id": "sess-1", "status": "closing"})
            stored = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "closing")

    def test_pump_input_returns_new_offset_without_skipping_appended_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.queue"
            input_path.write_bytes(b"hello")
            read_fd, write_fd = os.pipe()
            try:
                offset = _pump_input(input_path, write_fd, 0)
                self.assertEqual(offset, 5)
                self.assertEqual(os.read(read_fd, 5), b"hello")

                with input_path.open("ab") as handle:
                    handle.write(b" world")
                offset = _pump_input(input_path, write_fd, offset)
                self.assertEqual(offset, 11)
                self.assertEqual(os.read(read_fd, 6), b" world")
            finally:
                os.close(read_fd)
                os.close(write_fd)

    def test_run_session_marks_meta_as_error_when_spawn_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            session_dir = _write_session(
                root,
                "sess-err",
                command=["/definitely/missing/terminal-cli"],
                cwd=str(root),
                status="starting",
            )

            with self.assertRaises(FileNotFoundError):
                run_session(session_dir)

            meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["status"], "error")
            self.assertIsNone(meta["return_code"])
            self.assertIn("missing", meta["error"])


class TerminalBridgeCliTests(unittest.TestCase):
    def test_main_opens_session_and_prints_json(self) -> None:
        with mock.patch("claw_v2.terminal_bridge_cli.TerminalBridgeService") as mock_service_cls:
            mock_service = mock_service_cls.return_value
            mock_service.open.return_value = {"session_id": "sess-1", "tool": "codex"}
            with mock.patch("sys.stdout.write") as mock_stdout:
                exit_code = terminal_bridge_cli.main(["open", "codex", "--cwd", "/tmp/workspace"])

        self.assertEqual(exit_code, 0)
        mock_service.open.assert_called_once_with("codex", cwd="/tmp/workspace", args=[])
        written = "".join(call.args[0] for call in mock_stdout.call_args_list)
        self.assertIn('"session_id": "sess-1"', written)


if __name__ == "__main__":
    unittest.main()
