from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.lifecycle import PidLock, load_soul, run


class PidLockTests(unittest.TestCase):
    def test_acquire_writes_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = PidLock(Path(tmpdir) / "test.pid")
            lock.acquire()
            self.assertTrue(lock.path.exists())
            content = lock.path.read_text().strip()
            self.assertEqual(content, str(os.getpid()))
            lock.release()

    def test_release_removes_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            lock = PidLock(Path(tmpdir) / "test.pid")
            lock.acquire()
            lock.release()
            self.assertFalse(lock.path.exists())

    def test_acquire_fails_if_pid_alive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "test.pid"
            pid_path.write_text(str(os.getpid()))
            lock = PidLock(pid_path)
            with self.assertRaises(SystemExit):
                lock.acquire()

    def test_acquire_succeeds_if_stale_pid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pid_path = Path(tmpdir) / "test.pid"
            pid_path.write_text("999999999")
            lock = PidLock(pid_path)
            lock.acquire()
            self.assertEqual(lock.path.read_text().strip(), str(os.getpid()))
            lock.release()


class LoadSoulTests(unittest.TestCase):
    def test_loads_soul_file(self) -> None:
        prompt = load_soul()
        self.assertIn("Claw", prompt)
        self.assertIn("Hector Pachano", prompt)

    def test_fallback_when_no_file(self) -> None:
        prompt = load_soul(Path("/nonexistent/SOUL.md"))
        self.assertEqual(prompt, "You are Claw.")


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_completes_when_daemon_loop_returns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.lifecycle.PidLock") as mock_lock_cls:
                    mock_lock = MagicMock()
                    mock_lock_cls.return_value = mock_lock
                    with patch("claw_v2.lifecycle.TelegramTransport") as mock_transport_cls:
                        mock_transport = AsyncMock()
                        mock_transport_cls.return_value = mock_transport
                        with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                            mock_runtime = MagicMock()
                            mock_runtime.config.telegram_bot_token = None
                            mock_runtime.config.telegram_allowed_user_id = None
                            mock_runtime.config.openai_api_key = None
                            mock_runtime.daemon.run_loop = AsyncMock()
                            mock_build.return_value = mock_runtime
                            result = await run()
                            self.assertEqual(result, 0)
                            mock_lock.acquire.assert_called_once()
                            mock_lock.release.assert_called_once()

    async def test_run_skips_telegram_when_no_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.lifecycle.PidLock"):
                    with patch("claw_v2.lifecycle.TelegramTransport") as mock_transport_cls:
                        mock_transport = AsyncMock()
                        mock_transport_cls.return_value = mock_transport
                        with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                            mock_runtime = MagicMock()
                            mock_runtime.config.telegram_bot_token = None
                            mock_runtime.config.telegram_allowed_user_id = None
                            mock_runtime.config.openai_api_key = None
                            mock_runtime.daemon.run_loop = AsyncMock()
                            mock_build.return_value = mock_runtime
                            await run()
                            mock_transport_cls.assert_called_once()
                            args = mock_transport_cls.call_args
                            self.assertIsNone(args.kwargs.get("token") or args[1].get("token"))

    async def test_run_skips_managed_chrome_autostart_for_playwright_local_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.lifecycle.PidLock"):
                    with patch("claw_v2.lifecycle.TelegramTransport") as mock_transport_cls:
                        mock_transport = AsyncMock()
                        mock_transport_cls.return_value = mock_transport
                        with patch("claw_v2.lifecycle.ManagedChrome") as mock_managed_chrome_cls:
                            with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                mock_runtime = MagicMock()
                                mock_runtime.config.telegram_bot_token = None
                                mock_runtime.config.telegram_allowed_user_id = None
                                mock_runtime.config.openai_api_key = None
                                mock_runtime.config.chrome_cdp_enabled = True
                                mock_runtime.config.browse_backend = "playwright_local"
                                mock_runtime.daemon.run_loop = AsyncMock()
                                mock_build.return_value = mock_runtime

                                await run()

                                mock_managed_chrome_cls.assert_not_called()

    async def test_run_skips_managed_chrome_autostart_for_browserbase_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env = {
                "DB_PATH": str(root / "data" / "claw.db"),
                "WORKSPACE_ROOT": str(root / "workspace"),
                "AGENT_STATE_ROOT": str(root / "agents"),
                "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
                "APPROVALS_ROOT": str(root / "approvals"),
            }
            with patch.dict(os.environ, env, clear=False):
                with patch("claw_v2.lifecycle.PidLock"):
                    with patch("claw_v2.lifecycle.TelegramTransport") as mock_transport_cls:
                        mock_transport = AsyncMock()
                        mock_transport_cls.return_value = mock_transport
                        with patch("claw_v2.lifecycle.ManagedChrome") as mock_managed_chrome_cls:
                            with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                mock_runtime = MagicMock()
                                mock_runtime.config.telegram_bot_token = None
                                mock_runtime.config.telegram_allowed_user_id = None
                                mock_runtime.config.openai_api_key = None
                                mock_runtime.config.chrome_cdp_enabled = True
                                mock_runtime.config.browse_backend = "browserbase_cdp"
                                mock_runtime.daemon.run_loop = AsyncMock()
                                mock_build.return_value = mock_runtime

                                await run()

                                mock_managed_chrome_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
