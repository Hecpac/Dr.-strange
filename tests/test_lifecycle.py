from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.lifecycle import (
    PidLock,
    load_soul,
    run,
    should_route_observability_telegram_notifications,
    should_send_fitness_reminder,
)


class TerminalNotificationDedupTests(unittest.TestCase):
    """AM-NOTIFY — terminal notifications dedupe per attempt, not per task."""

    def test_key_uses_attempt_from_payload_or_metadata(self) -> None:
        from claw_v2.lifecycle import terminal_notification_key

        self.assertEqual(terminal_notification_key({"task_id": "t1"}), "t1#attempt-0")
        self.assertEqual(
            terminal_notification_key({"task_id": "t1", "attempt": 2}), "t1#attempt-2"
        )
        self.assertEqual(
            terminal_notification_key({"task_id": "t1", "metadata": {"resume_count": 3}}),
            "t1#attempt-3",
        )
        self.assertEqual(
            terminal_notification_key({"task_id": "t1", "attempt": "garbage"}),
            "t1#attempt-0",
        )

    def test_resumed_attempt_is_not_suppressed_by_prior_notification(self) -> None:
        from claw_v2.lifecycle import (
            should_notify_task_ledger_terminal,
            terminal_notification_key,
        )

        notified: set[str] = set()
        payload = {
            "session_id": "tg-1",
            "task_id": "t1",
            "status": "failed",
            "runtime": "brain",
            "metadata": {},
        }
        self.assertTrue(should_notify_task_ledger_terminal(payload, notified))
        notified.add(terminal_notification_key(payload))
        self.assertFalse(
            should_notify_task_ledger_terminal(payload, notified),
            "same attempt must stay deduped",
        )
        resumed = {**payload, "metadata": {"resume_count": 1}}
        self.assertTrue(
            should_notify_task_ledger_terminal(resumed, notified),
            "a resumed attempt must notify even though attempt 0 already did",
        )


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
        self.assertEqual(prompt, "You are Dr. Strange, the autonomous personal agent for Hector Pachano.")


class FitnessReminderTests(unittest.TestCase):
    def test_fitness_reminder_only_sends_during_five_am_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stamp = Path(tmpdir) / "fitness_last_sent.txt"
            self.assertFalse(should_send_fitness_reminder(datetime(2026, 4, 15, 4, 59), stamp))
            self.assertTrue(should_send_fitness_reminder(datetime(2026, 4, 15, 5, 0), stamp))
            self.assertTrue(should_send_fitness_reminder(datetime(2026, 4, 15, 5, 59), stamp))
            self.assertFalse(should_send_fitness_reminder(datetime(2026, 4, 15, 6, 0), stamp))

    def test_fitness_reminder_stamp_blocks_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stamp = Path(tmpdir) / "fitness_last_sent.txt"
            stamp.write_text("2026-04-15")
            self.assertFalse(should_send_fitness_reminder(datetime(2026, 4, 15, 5, 30), stamp))
            self.assertTrue(should_send_fitness_reminder(datetime(2026, 4, 16, 5, 0), stamp))


class ObservabilityTelegramRoutingTests(unittest.TestCase):
    def test_requires_separate_observability_chat(self) -> None:
        self.assertFalse(should_route_observability_telegram_notifications(None, "123"))
        self.assertFalse(should_route_observability_telegram_notifications("", "123"))
        self.assertFalse(should_route_observability_telegram_notifications("123", "123"))
        self.assertFalse(should_route_observability_telegram_notifications(123, "123"))
        self.assertTrue(should_route_observability_telegram_notifications("456", "123"))


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
                        with patch("claw_v2.lifecycle.WebTransport") as mock_web_transport_cls:
                            mock_web_transport = AsyncMock()
                            mock_web_transport_cls.return_value = mock_web_transport
                            with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                mock_runtime = MagicMock()
                                mock_runtime.config.telegram_bot_token = None
                                mock_runtime.config.telegram_allowed_user_id = None
                                mock_runtime.config.openai_api_key = None
                                mock_runtime.config.web_chat_enabled = True
                                mock_runtime.config.web_chat_host = "127.0.0.1"
                                mock_runtime.config.web_chat_port = 8765
                                mock_runtime.daemon.run_loop = AsyncMock()
                                mock_build.return_value = mock_runtime
                                result = await run()
                                self.assertEqual(result, 0)
                                mock_lock.acquire.assert_called_once()
                                mock_lock.release.assert_called_once()
                                mock_web_transport.start.assert_awaited_once()
                                mock_web_transport.stop.assert_awaited_once()

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
                        with patch("claw_v2.lifecycle.WebTransport") as mock_web_transport_cls:
                            mock_web_transport = AsyncMock()
                            mock_web_transport_cls.return_value = mock_web_transport
                            with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                mock_runtime = MagicMock()
                                mock_runtime.config.telegram_bot_token = None
                                mock_runtime.config.telegram_allowed_user_id = None
                                mock_runtime.config.openai_api_key = None
                                mock_runtime.config.web_chat_enabled = False
                                mock_runtime.config.web_chat_host = "127.0.0.1"
                                mock_runtime.config.web_chat_port = 8765
                                mock_runtime.daemon.run_loop = AsyncMock()
                                mock_build.return_value = mock_runtime
                                await run()
                                mock_transport_cls.assert_called_once()
                                args = mock_transport_cls.call_args
                                self.assertIsNone(args.kwargs.get("token") or args[1].get("token"))
                                mock_web_transport.start.assert_not_awaited()
                                mock_web_transport.stop.assert_awaited_once()

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
                        with patch("claw_v2.lifecycle.WebTransport") as mock_web_transport_cls:
                            mock_web_transport = AsyncMock()
                            mock_web_transport_cls.return_value = mock_web_transport
                            with patch("claw_v2.lifecycle.ManagedChrome") as mock_managed_chrome_cls:
                                with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                    mock_runtime = MagicMock()
                                    mock_runtime.config.telegram_bot_token = None
                                    mock_runtime.config.telegram_allowed_user_id = None
                                    mock_runtime.config.openai_api_key = None
                                    mock_runtime.config.web_chat_enabled = False
                                    mock_runtime.config.web_chat_host = "127.0.0.1"
                                    mock_runtime.config.web_chat_port = 8765
                                    mock_runtime.config.chrome_cdp_enabled = True
                                    mock_runtime.config.browse_backend = "playwright_local"
                                    mock_runtime.daemon.run_loop = AsyncMock()
                                    mock_build.return_value = mock_runtime

                                    await run()

                                    mock_managed_chrome_cls.assert_not_called()

    async def test_run_marks_chrome_capability_degraded_when_managed_chrome_fails(self) -> None:
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
                        with patch("claw_v2.lifecycle.WebTransport") as mock_web_transport_cls:
                            mock_web_transport = AsyncMock()
                            mock_web_transport_cls.return_value = mock_web_transport
                            with patch("claw_v2.lifecycle.ManagedChrome") as mock_managed_chrome_cls:
                                mock_managed_chrome = MagicMock()
                                mock_managed_chrome.start.side_effect = RuntimeError("boom")
                                mock_managed_chrome_cls.return_value = mock_managed_chrome
                                with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                    mock_runtime = MagicMock()
                                    mock_runtime.config.telegram_bot_token = None
                                    mock_runtime.config.telegram_allowed_user_id = None
                                    mock_runtime.config.openai_api_key = None
                                    mock_runtime.config.web_chat_enabled = False
                                    mock_runtime.config.web_chat_host = "127.0.0.1"
                                    mock_runtime.config.web_chat_port = 8765
                                    mock_runtime.config.chrome_cdp_enabled = True
                                    mock_runtime.config.browse_backend = "chrome_cdp"
                                    mock_runtime.config.claw_chrome_port = 9250
                                    mock_runtime.daemon.run_loop = AsyncMock()
                                    mock_runtime.bot = MagicMock()
                                    mock_build.return_value = mock_runtime

                                    await run()

                                    mock_runtime.bot.set_capability_status.assert_any_call(
                                        "chrome_cdp",
                                        available=False,
                                        reason="Chrome no pudo iniciar en el puerto 9250; la navegación autenticada queda temporalmente desactivada.",
                                    )

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
                        with patch("claw_v2.lifecycle.WebTransport") as mock_web_transport_cls:
                            mock_web_transport = AsyncMock()
                            mock_web_transport_cls.return_value = mock_web_transport
                            with patch("claw_v2.lifecycle.ManagedChrome") as mock_managed_chrome_cls:
                                with patch("claw_v2.lifecycle.build_runtime") as mock_build:
                                    mock_runtime = MagicMock()
                                    mock_runtime.config.telegram_bot_token = None
                                    mock_runtime.config.telegram_allowed_user_id = None
                                    mock_runtime.config.openai_api_key = None
                                    mock_runtime.config.web_chat_enabled = False
                                    mock_runtime.config.web_chat_host = "127.0.0.1"
                                    mock_runtime.config.web_chat_port = 8765
                                    mock_runtime.config.chrome_cdp_enabled = True
                                    mock_runtime.config.browse_backend = "browserbase_cdp"
                                    mock_runtime.daemon.run_loop = AsyncMock()
                                    mock_build.return_value = mock_runtime

                                    await run()

                                    mock_managed_chrome_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
