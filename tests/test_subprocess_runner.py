from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.subprocess_runner import run_subprocess_bounded, run_subprocess_bounded_off_loop


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return True
    if result.returncode != 0:
        return False
    return not result.stdout.strip().startswith("Z")
    return True


class SubprocessRunnerTests(unittest.TestCase):
    def test_hanging_subprocess_times_out_promptly(self) -> None:
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_subprocess_bounded(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_s=0.1,
            )
        self.assertLess(time.monotonic() - started, 3.0)

    def test_process_group_kill_terminates_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "child.pid"
            script = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"open({str(pid_path)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
                "time.sleep(30)\n"
            )
            with self.assertRaises(subprocess.TimeoutExpired):
                run_subprocess_bounded([sys.executable, "-c", script], timeout_s=0.2)

            deadline = time.monotonic() + 3.0
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            while time.monotonic() < deadline and _pid_alive(child_pid):
                time.sleep(0.05)
            self.assertFalse(_pid_alive(child_pid))

    def test_process_group_kill_escalates_and_terminates_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            child_pid_path = Path(tmpdir) / "child.pid"
            grandchild_pid_path = Path(tmpdir) / "grandchild.pid"
            child_script = (
                "import os, signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "grandchild = subprocess.Popen([\n"
                "    sys.executable,\n"
                "    '-c',\n"
                "    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',\n"
                "])\n"
                f"open({str(child_pid_path)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
                f"open({str(grandchild_pid_path)!r}, 'w', encoding='utf-8').write(str(grandchild.pid))\n"
                "time.sleep(30)\n"
            )
            parent_script = (
                "from pathlib import Path\n"
                "import signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
                f"deadline = time.monotonic() + 5\n"
                f"while time.monotonic() < deadline and not (Path({str(child_pid_path)!r}).exists() and Path({str(grandchild_pid_path)!r}).exists()):\n"
                "    time.sleep(0.01)\n"
                "time.sleep(30)\n"
            )

            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                run_subprocess_bounded([sys.executable, "-c", parent_script], timeout_s=0.5)
            self.assertLess(time.monotonic() - started, 5.0)

            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and (
                _pid_alive(child_pid) or _pid_alive(grandchild_pid)
            ):
                time.sleep(0.05)
            self.assertFalse(_pid_alive(child_pid))
            self.assertFalse(_pid_alive(grandchild_pid))

    def test_large_output_is_captured_and_bounded_without_deadlock(self) -> None:
        result = run_subprocess_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 200000); sys.stderr.write('y' * 200000)",
            ],
            timeout_s=5,
            max_output_chars=2000,
        )
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout), 2000)
        self.assertLessEqual(len(result.stderr), 2000)

    def test_check_true_raises_called_process_error_with_output(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            run_subprocess_bounded(
                [sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"],
                timeout_s=5,
                check=True,
            )
        self.assertEqual(ctx.exception.returncode, 7)
        self.assertIn("bad", ctx.exception.output)

    def test_events_are_flat_snake_case(self) -> None:
        class Observe:
            def __init__(self) -> None:
                self.events: list[str] = []

            def emit(self, event_type: str, *, payload: dict) -> None:
                self.events.append(event_type)

        observe = Observe()
        run_subprocess_bounded([sys.executable, "-c", "print('ok')"], timeout_s=5, observe=observe)
        self.assertEqual(observe.events, ["subprocess_started", "subprocess_finished"])

    def test_events_redact_sensitive_arguments_and_report_truncation(self) -> None:
        class Observe:
            def __init__(self) -> None:
                self.payloads: list[dict] = []

            def emit(self, event_type: str, *, payload: dict) -> None:
                self.payloads.append(payload)

        observe = Observe()
        run_subprocess_bounded(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.write('sensitive failure tail')",
                "--api-key",
                "secret-value",
                "GITHUB_TOKEN=abc123",
            ],
            timeout_s=5,
            max_output_chars=10,
            observe=observe,
        )

        started, finished = observe.payloads
        self.assertIn("REDACTED", started["args"])
        self.assertIn("GITHUB_TOKEN=REDACTED", started["args"])
        self.assertNotIn("secret-value", started["args"])
        self.assertTrue(finished["stderr_truncated"])
        self.assertEqual(finished["returncode"], 0)


class AsyncSubprocessRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_loop_runner_does_not_block_event_loop(self) -> None:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                await asyncio.sleep(0.02)
                ticks += 1

        result, _ = await asyncio.gather(
            run_subprocess_bounded_off_loop(
                [sys.executable, "-c", "import time; time.sleep(0.2); print('done')"],
                timeout_s=2,
            ),
            ticker(),
        )
        self.assertEqual(result.returncode, 0)
        self.assertGreaterEqual(ticks, 5)

    async def test_off_loop_timeout_returns_promptly(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            await asyncio.wait_for(
                run_subprocess_bounded_off_loop(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    timeout_s=0.1,
                ),
                timeout=3,
            )

    async def test_off_loop_cancellation_kills_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "child.pid"
            script = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"open({str(pid_path)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
                "time.sleep(30)\n"
            )
            task = asyncio.create_task(
                run_subprocess_bounded_off_loop(
                    [sys.executable, "-c", script],
                    timeout_s=30,
                )
            )

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not pid_path.exists():
                await asyncio.sleep(0.02)
            self.assertTrue(pid_path.exists())

            child_pid = int(pid_path.read_text(encoding="utf-8"))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and _pid_alive(child_pid):
                await asyncio.sleep(0.05)
            self.assertFalse(_pid_alive(child_pid))
