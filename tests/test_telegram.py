from __future__ import annotations

import asyncio
import base64
import contextlib
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import BadRequest, RetryAfter, TimedOut

import claw_v2.observe as observe_module
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream
from claw_v2.telegram import (
    TelegramTransport,
    _ProgressIndicator,
    _build_image_content_blocks,
    _build_video_content_blocks,
    _polling_lock_path,
    _progress_text,
    _split_message,
)


def _purge_polling_lock(token: str) -> None:
    """Remove any stale polling lock for ``token`` so cross-session PID
    collisions cannot flake the start tests (the lock file persists with
    the prior pytest run's PID and another live process may have reused
    that slot)."""
    try:
        _polling_lock_path(token).unlink(missing_ok=True)
    except OSError:
        pass


class ProgressTextTests(unittest.TestCase):
    def test_progress_text_shows_integer_elapsed(self) -> None:
        assert _progress_text(0) == "⏳ Trabajando… (0s)"
        assert _progress_text(15.9) == "⏳ Trabajando… (15s)"


class ProgressIndicatorTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self) -> AsyncMock:
        bot = AsyncMock()
        bot.send_message.return_value = SimpleNamespace(message_id=4242)
        return bot

    async def test_disabled_never_sends_placeholder(self) -> None:
        bot = self._bot()
        ind = _ProgressIndicator(
            bot,
            1,
            MagicMock(),
            enabled=False,
            threshold_seconds=0.01,
            interval_seconds=0.01,
        )

        async def slow() -> str:
            await asyncio.sleep(0.1)
            return "done"

        task = asyncio.create_task(slow())
        ind.arm(task)
        await task
        await ind.clear()
        bot.send_message.assert_not_called()

    async def test_fast_turn_posts_no_placeholder(self) -> None:
        bot = self._bot()
        ind = _ProgressIndicator(
            bot,
            1,
            MagicMock(),
            enabled=True,
            threshold_seconds=5.0,
            interval_seconds=1.0,
        )

        async def fast() -> str:
            return "quick"

        task = asyncio.create_task(fast())
        ind.arm(task)
        await task
        await ind.clear()
        bot.send_message.assert_not_called()
        bot.delete_message.assert_not_called()

    async def test_slow_turn_posts_then_clears_placeholder(self) -> None:
        bot = self._bot()
        ind = _ProgressIndicator(
            bot,
            99,
            MagicMock(),
            enabled=True,
            threshold_seconds=0.02,
            interval_seconds=0.02,
        )

        async def slow() -> str:
            await asyncio.sleep(0.2)
            return "result"

        task = asyncio.create_task(slow())
        ind.arm(task)
        await task
        await ind.clear()
        bot.send_message.assert_awaited_once()
        bot.delete_message.assert_awaited_once_with(chat_id=99, message_id=4242)

    async def test_send_failure_is_swallowed(self) -> None:
        bot = self._bot()
        bot.send_message.side_effect = RuntimeError("network down")
        ind = _ProgressIndicator(
            bot,
            1,
            MagicMock(),
            enabled=True,
            threshold_seconds=0.02,
            interval_seconds=0.02,
        )

        async def slow() -> str:
            await asyncio.sleep(0.15)
            return "ok"

        task = asyncio.create_task(slow())
        ind.arm(task)
        # Must not raise despite the failing placeholder send.
        await task
        await ind.clear()
        bot.delete_message.assert_not_called()


class SplitMessageTests(unittest.TestCase):
    def test_short_message_unchanged(self) -> None:
        self.assertEqual(_split_message("hello"), ["hello"])

    def test_long_message_split(self) -> None:
        text = "a" * 5000
        parts = _split_message(text, max_len=4096)
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[0]), 4000)
        self.assertEqual(len(parts[1]), 1000)

    def test_empty_message(self) -> None:
        self.assertEqual(_split_message(""), [""])

    def test_split_counts_utf16_units_not_code_points(self) -> None:
        # Telegram counts UTF-16 code units: 4096 non-BMP emojis are 8192
        # units, so a code-point split would hit MESSAGE_TOO_LONG (T3).
        text = "\U0001f600" * 4096
        parts = _split_message(text)
        self.assertEqual("".join(parts), text)
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-16-le")) // 2, 4096)
        self.assertEqual(len(parts), 3)

    def test_split_prefers_newline_boundary(self) -> None:
        text = "x" * 3900 + "\n" + "y" * 500
        parts = _split_message(text)
        self.assertEqual(parts[0], "x" * 3900 + "\n")
        self.assertEqual(parts[1], "y" * 500)
        self.assertEqual("".join(parts), text)


class RateLimitConfigTests(unittest.TestCase):
    """T9: the 10/60s limiter silently dropped the single operator's 11th
    message; the limits are now env-tunable with a 30/60s default."""

    def test_defaults_to_30_per_60s(self) -> None:
        transport = TelegramTransport(bot_service=MagicMock(), token="t")
        self.assertEqual(transport._rate_max, 30)
        self.assertEqual(transport._rate_window, 60.0)

    def test_env_overrides_and_limit_enforced(self) -> None:
        with patch.dict("os.environ", {"TELEGRAM_RATE_MAX": "2", "TELEGRAM_RATE_WINDOW": "30"}):
            transport = TelegramTransport(bot_service=MagicMock(), token="t")
        self.assertEqual(transport._rate_max, 2)
        self.assertEqual(transport._rate_window, 30.0)
        self.assertFalse(transport._is_rate_limited("u"))
        self.assertFalse(transport._is_rate_limited("u"))
        self.assertTrue(transport._is_rate_limited("u"))

    def test_rate_max_is_capped(self) -> None:
        # A huge value must not silently disable rate limiting (review #100).
        with patch.dict("os.environ", {"TELEGRAM_RATE_MAX": "999999"}):
            transport = TelegramTransport(bot_service=MagicMock(), token="t")
        self.assertEqual(transport._rate_max, 120)


class TransportStartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        _purge_polling_lock("test-token")
        self.addAsyncCleanup(self._purge_test_lock)

    async def _purge_test_lock(self) -> None:
        _purge_polling_lock("test-token")

    async def test_start_is_noop_without_token(self) -> None:
        transport = TelegramTransport(bot_service=MagicMock(), token=None)
        await transport.start()
        await transport.stop()

    @patch("claw_v2.telegram.ApplicationBuilder")
    async def test_start_builds_and_polls_with_token(self, mock_builder_cls) -> None:
        mock_app = AsyncMock()
        mock_app.updater = AsyncMock()
        mock_app.add_handler = MagicMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app
        mock_builder_cls.return_value = mock_builder

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        await transport.start()
        mock_builder.connection_pool_size.assert_called_once_with(32)
        mock_builder.get_updates_connection_pool_size.assert_called_once_with(8)
        mock_builder.pool_timeout.assert_called_once_with(30.0)
        mock_builder.get_updates_pool_timeout.assert_called_once_with(30.0)
        mock_app.initialize.assert_awaited_once()
        mock_app.start.assert_awaited_once()
        mock_app.updater.start_polling.assert_awaited_once_with(drop_pending_updates=False)
        await transport.stop()

    @patch("claw_v2.telegram.ApplicationBuilder")
    async def test_start_polling_drops_pending_updates_when_env_set(self, mock_builder_cls) -> None:
        mock_app = AsyncMock()
        mock_app.updater = AsyncMock()
        mock_app.add_handler = MagicMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app
        mock_builder_cls.return_value = mock_builder

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        with patch.dict("os.environ", {"TELEGRAM_DROP_PENDING_UPDATES": "1"}):
            await transport.start()
        mock_app.updater.start_polling.assert_awaited_once_with(drop_pending_updates=True)
        await transport.stop()

    @patch("claw_v2.telegram.ApplicationBuilder")
    async def test_start_does_not_send_startup_message_to_chat(self, mock_builder_cls) -> None:
        """A: startup notification stays in logs, never sent to chat."""
        mock_app = AsyncMock()
        mock_app.updater = AsyncMock()
        mock_app.add_handler = MagicMock()
        mock_app.bot = AsyncMock()
        mock_builder = MagicMock()
        mock_builder.token.return_value = mock_builder
        mock_builder.build.return_value = mock_app
        mock_builder_cls.return_value = mock_builder

        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="test-token",
            allowed_user_id="1234",
        )
        with self.assertLogs("claw_v2.telegram", level="INFO") as captured:
            await transport.start()
        mock_app.bot.send_message.assert_not_called()
        joined = "\n".join(captured.output)
        self.assertIn("online", joined.lower())
        await transport.stop()

    @patch("claw_v2.telegram.ApplicationBuilder")
    @patch("claw_v2.telegram.acquire_polling_lock")
    async def test_start_does_not_overwrite_pid_file_on_lock_conflict(
        self, mock_acquire, mock_builder_cls
    ) -> None:
        """Regression: PollingLockConflict during start() must NOT clobber the
        on-disk PID file with our PID. Otherwise the next launch's watchdog
        reads a lying pidfile and SIGTERMs the wrong process."""
        import os
        from claw_v2.telegram import PollingLockConflict, TelegramTransport

        tmp_pid_file = Path(tempfile.mkstemp(suffix=".pid")[1])
        other_pid = "999999"
        tmp_pid_file.write_text(other_pid)

        mock_acquire.side_effect = PollingLockConflict(123456, Path("/tmp/fake.lock"))

        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        try:
            with patch.object(TelegramTransport, "_PID_FILE", tmp_pid_file):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = SimpleNamespace(stdout="", returncode=0)
                    await transport.start()
            content = tmp_pid_file.read_text().strip() if tmp_pid_file.exists() else ""
            self.assertNotEqual(
                content,
                str(os.getpid()),
                "PID file was overwritten with our PID despite PollingLockConflict",
            )
            self.assertEqual(content, other_pid)
            mock_builder_cls.assert_not_called()
        finally:
            tmp_pid_file.unlink(missing_ok=True)

    async def test_stop_swallows_pool_cleanup_errors_and_emits_observe_event(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(bot_service=bot_service, token="test-token")
        app = MagicMock()
        app.updater.stop = AsyncMock(side_effect=RuntimeError("Pool timeout"))
        app.stop = AsyncMock()
        app.shutdown = AsyncMock()
        transport._app = app

        await transport.stop()

        app.updater.stop.assert_awaited_once()
        app.stop.assert_awaited_once()
        app.shutdown.assert_awaited_once()
        bot_service.observe.emit.assert_called_once()
        self.assertEqual(
            bot_service.observe.emit.call_args.args[0], "telegram_transport_stop_error"
        )
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["error_count"], 1)
        self.assertIn("Pool timeout", payload["errors"][0])

    async def test_set_commands_uses_curated_short_menu(self) -> None:
        transport = TelegramTransport(bot_service=MagicMock(), token="test-token")
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()

        await transport._set_commands()

        transport._app.bot.set_my_commands.assert_awaited_once()
        commands = transport._app.bot.set_my_commands.await_args.args[0]
        names = [command.command for command in commands]
        self.assertEqual(
            names,
            [
                "browse",
                "status",
                "freeze",
                "unfreeze",
                "budget_status",
                "approvals",
                "models",
                "model",
                "jobs",
                "pipeline_status",
                "agents",
                "screen",
                "computer",
                "terminal_list",
                "nlm_list",
                "nlm_create",
                "grill",
                "tdd",
                "improve_arch",
                "playbooks",
                "backtest",
                "effort",
                "verify",
                "focus",
                "voice",
                "design",
                "help",
            ],
        )


class HandleTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_user_silently_dropped(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="999",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.message.reply_text = AsyncMock()
        await transport._handle_text(update, MagicMock())
        update.message.reply_text.assert_not_awaited()

    async def test_authorized_user_gets_response(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited()
        event_names = [call.args[0] for call in bot_service.observe.emit.call_args_list]
        self.assertIn("telegram_outbound_attempt", event_names)
        self.assertIn("telegram_outbound_sent", event_names)
        self.assertIn("telegram_latency", event_names)
        latency_call = next(
            call
            for call in bot_service.observe.emit.call_args_list
            if call.args[0] == "telegram_latency"
        )
        payload = latency_call.kwargs["payload"]
        self.assertEqual(payload["message_kind"], "text")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response_parts"], 1)
        self.assertGreaterEqual(payload["total_ms"], 0.0)

    async def test_database_locked_error_is_not_exposed_to_chat(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.side_effect = sqlite3.OperationalError("database is locked")
        bot_service.observe = MagicMock()
        bot_service.is_voice_mode.return_value = None
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "Abre Claude"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited()
        reply = update.message.reply_text.await_args.args[0]
        self.assertIn("contención de base de datos", reply)
        self.assertNotIn("database is locked", reply)

    async def test_authorized_text_continues_when_chat_action_times_out(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock(side_effect=TimedOut("Timed out"))

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            await transport._handle_text(update, MagicMock())

        update.message.chat.send_action.assert_awaited_once_with("typing")
        update.message.reply_text.assert_awaited()
        event_names = [call.args[0] for call in bot_service.observe.emit.call_args_list]
        self.assertIn("telegram_outbound_sent", event_names)
        latency_payload = next(
            call.kwargs["payload"]
            for call in bot_service.observe.emit.call_args_list
            if call.args[0] == "telegram_latency"
        )
        self.assertEqual(latency_payload["status"], "ok")

    async def test_authorized_text_uses_direct_bot_api_when_reply_times_out(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._text_send_retry_delay = 0.0
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        transport._send_text_direct_bot_api_sync = MagicMock(return_value=77)
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock(side_effect=TimedOut("Timed out"))
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:

            async def fake_to_thread(func, *args, **kwargs):
                return func(*args, **kwargs)

            mock_asyncio.to_thread = fake_to_thread
            await transport._handle_text(update, MagicMock())

        self.assertEqual(update.message.reply_text.await_count, 1)
        transport._app.bot.send_message.assert_not_awaited()
        transport._send_text_direct_bot_api_sync.assert_called_once_with(
            chat_id=1,
            text="response text",
        )
        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        self.assertTrue(
            any(
                name == "telegram_outbound_error"
                and payload["method"] == "reply_text"
                and payload["error_type"] == "TimedOut"
                for name, payload in events
            )
        )
        self.assertTrue(
            any(
                name == "telegram_outbound_sent"
                and payload["method"] == "bot_api_direct_fallback"
                and payload["message_id"] == 77
                for name, payload in events
            )
        )
        latency_payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(latency_payload["status"], "ok")
        self.assertEqual(latency_payload["response_parts"], 1)

    async def test_authorized_text_retries_reply_before_fallback(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._text_send_retries = 3
        transport._text_send_retry_delay = 0.0
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock(
            side_effect=[TimedOut("Timed out"), SimpleNamespace(message_id=10)]
        )
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            await transport._handle_text(update, MagicMock())

        self.assertEqual(update.message.reply_text.await_count, 2)
        transport._app.bot.send_message.assert_not_awaited()
        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        self.assertTrue(
            any(
                name == "telegram_outbound_sent"
                and payload["method"] == "reply_text"
                and payload["attempt"] == 2
                for name, payload in events
            )
        )
        latency_payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(latency_payload["status"], "ok")
        self.assertEqual(latency_payload["response_parts"], 1)

    async def test_authorized_text_marks_send_failed_when_reply_and_fallback_fail(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._text_send_retry_delay = 0.0
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        transport._app.bot.send_message.side_effect = TimedOut("Timed out")
        transport._send_text_direct_bot_api = AsyncMock(return_value=False)
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock(side_effect=TimedOut("Timed out"))
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            await transport._handle_text(update, MagicMock())

        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        fallback_error = [
            payload
            for name, payload in events
            if name == "telegram_outbound_error" and payload["method"] == "send_message_fallback"
        ]
        self.assertEqual(fallback_error[0]["error_type"], "TimedOut")
        latency_payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(latency_payload["status"], "send_failed")
        self.assertEqual(latency_payload["response_parts"], 0)

    async def test_authorized_text_uses_ptb_fallback_when_direct_api_fails(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._text_send_retry_delay = 0.0
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        transport._app.bot.send_message.return_value = SimpleNamespace(message_id=88)
        transport._send_text_direct_bot_api = AsyncMock(return_value=False)
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock(side_effect=TimedOut("Timed out"))
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value="response text")
            mock_asyncio.sleep = AsyncMock()
            await transport._handle_text(update, MagicMock())

        self.assertEqual(update.message.reply_text.await_count, 1)
        transport._send_text_direct_bot_api.assert_awaited_once()
        transport._app.bot.send_message.assert_awaited_once()
        self.assertEqual(transport._app.bot.send_message.await_args.kwargs["chat_id"], 1)
        self.assertEqual(transport._app.bot.send_message.await_args.kwargs["text"], "response text")
        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        self.assertTrue(
            any(
                name == "telegram_outbound_sent"
                and payload["method"] == "send_message_fallback"
                and payload["message_id"] == 88
                for name, payload in events
            )
        )
        latency_payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(latency_payload["status"], "ok")
        self.assertEqual(latency_payload["response_parts"], 1)

    async def test_late_delivery_guard_sends_direct_when_handler_is_cancelled(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        bot_service.is_voice_mode.return_value = None
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._late_delivery_grace_seconds = 0.01
        transport._send_text_direct_bot_api_sync = MagicMock(return_value=99)
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        async def delayed_response(**_kwargs):
            await asyncio.sleep(0.05)
            return "late response text"

        transport._handle_agent_text = delayed_response
        task = asyncio.create_task(transport._handle_text(update, MagicMock()))
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.12)

        update.message.reply_text.assert_not_awaited()
        transport._send_text_direct_bot_api_sync.assert_called_once_with(
            chat_id=1,
            text="late response text",
        )
        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        self.assertTrue(
            any(
                name == "telegram_outbound_sent"
                and payload["method"] == "bot_api_late_delivery"
                and payload["message_id"] == 99
                for name, payload in events
            )
        )
        latency_payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(latency_payload["status"], "late_ok")
        self.assertEqual(latency_payload["response_parts"], 1)

    async def test_authorized_user_gets_no_reply_when_bot_returns_none(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = None
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "haz los fixes"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_not_awaited()
        bot_service.handle_text.assert_called_once()
        self.assertEqual(bot_service.handle_text.call_args.kwargs["runtime_channel"], "telegram")
        bot_service.observe.emit.assert_called_once()
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["message_kind"], "text")
        self.assertEqual(payload["status"], "no_reply")
        self.assertEqual(payload["response_parts"], 0)

    async def test_outbound_text_sanitizes_internal_trace_fallback(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = (
            "La salida del modelo contenía trazas internas de herramientas y la oculté. "
            "Repite la instrucción y la ejecuto limpio."
        )
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value=bot_service.handle_text.return_value)
            await transport._handle_text(update, MagicMock())

        visible = update.message.reply_text.await_args.args[0]
        lowered = visible.lower()
        self.assertNotIn("salida del modelo", lowered)
        self.assertNotIn("trazas internas", lowered)
        self.assertNotIn("herramientas internas", lowered)
        self.assertNotIn("la oculté", lowered)
        self.assertNotIn("respuesta bloqueada", lowered)
        self.assertNotIn("sanitizer", lowered)
        self.assertNotIn("blocked model response", lowered)
        self.assertNotIn("repite la instrucción", lowered)
        event_names = [call.args[0] for call in bot_service.observe.emit.call_args_list]
        self.assertIn("internal_message_suppressed_from_chat", event_names)

    async def test_outbound_text_sanitizes_system_reminder_marker(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "</system-reminder>"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(return_value=bot_service.handle_text.return_value)
            await transport._handle_text(update, MagicMock())

        visible = update.message.reply_text.await_args.args[0]
        self.assertNotIn("system-reminder", visible.lower())
        event_names = [call.args[0] for call in bot_service.observe.emit.call_args_list]
        self.assertIn("internal_message_suppressed_from_chat", event_names)

    async def test_authorized_text_uses_agent_runtime_when_available(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        agent_runtime = MagicMock()
        agent_runtime.handle_text.return_value = SimpleNamespace(
            text="runtime response", session_id="tg-1"
        )
        transport = TelegramTransport(
            bot_service=bot_service,
            agent_runtime=agent_runtime,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited()
        bot_service.handle_text.assert_not_called()
        agent_runtime.handle_text.assert_called_once_with(
            channel="telegram",
            external_user_id="123",
            external_session_id="1",
            session_id="tg-1",
            text="hello",
        )

    async def test_reply_to_text_is_passed_as_context_metadata(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        agent_runtime = MagicMock()
        agent_runtime.handle_text.return_value = SimpleNamespace(
            text="runtime response", session_id="tg-1"
        )
        transport = TelegramTransport(
            bot_service=bot_service,
            agent_runtime=agent_runtime,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "Dame los 2"
        update.message.reply_to_message.text = "Pendientes: 9:16 vertical y 1:1 cuadrado."
        update.message.reply_to_message.caption = None
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        await transport._handle_text(update, MagicMock())

        agent_runtime.handle_text.assert_called_once()
        metadata = agent_runtime.handle_text.call_args.kwargs["metadata"]
        self.assertEqual(
            metadata["reply_context"]["text"],
            "Pendientes: 9:16 vertical y 1:1 cuadrado.",
        )

    async def test_voice_mode_can_use_xai_without_openai_key(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response text"
        bot_service.is_voice_mode.return_value = "nova"
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
            xai_api_key="xai-key",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.reply_voice = AsyncMock()
        update.message.chat.send_action = AsyncMock()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg:
            ogg.write(b"ogg")
            ogg_path = Path(ogg.name)

        with patch(
            "claw_v2.telegram.synthesize_voice_note", new=AsyncMock(return_value=ogg_path)
        ) as tts:
            await transport._handle_text(update, MagicMock())

        update.message.reply_voice.assert_awaited_once()
        update.message.reply_text.assert_not_awaited()
        tts.assert_awaited_once()
        self.assertIsNone(tts.await_args.kwargs["api_key"])
        self.assertEqual(tts.await_args.kwargs["xai_api_key"], "xai-key")

    async def test_send_latest_image_request_bypasses_agent_and_sends_photo(self) -> None:
        db_path = Path(tempfile.mkdtemp()) / "test.db"
        memory = MemoryStore(db_path)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            image_path = Path(tmp.name)
        try:
            memory.store_message(
                "tg-1",
                "assistant",
                f"Resultado generado: `{image_path}`",
            )
            bot_service = MagicMock()
            bot_service.observe = MagicMock()
            bot_service.memory = memory
            transport = TelegramTransport(
                bot_service=bot_service,
                token="t",
                allowed_user_id="123",
            )
            transport._app = MagicMock()
            transport._app.bot = AsyncMock()
            update = MagicMock()
            update.effective_user.id = 123
            update.effective_chat.id = 1
            update.message.text = "Ponla aquí en telegram"
            update.message.reply_text = AsyncMock()
            update.message.chat.send_action = AsyncMock()

            await transport._handle_text(update, MagicMock())

            transport._app.bot.send_photo.assert_awaited_once()
            bot_service.handle_text.assert_not_called()
            self.assertEqual(
                update.message.reply_text.await_args.args[0], "Te la puse aquí en Telegram."
            )
            messages = memory.get_recent_messages("tg-1")
            self.assertEqual(messages[-2]["content"], "Ponla aquí en telegram")
            self.assertIn("Imagen enviada por Telegram", messages[-1]["content"])
        finally:
            image_path.unlink(missing_ok=True)

    async def test_claude_sdk_failures_return_specific_message(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.text = "hello"
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        with patch("claw_v2.telegram.asyncio") as mock_asyncio:
            mock_asyncio.to_thread = AsyncMock(
                side_effect=RuntimeError(
                    "Claude SDK execution failed: Control request timeout: initialize"
                )
            )
            await transport._handle_text(update, MagicMock())

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(
            update.message.reply_text.await_args.args[0],
            "El runtime de Claude falló: Claude SDK execution failed: Control request timeout: initialize",
        )


class HandleVoiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_message_transcribed_and_handled(self) -> None:
        bot_service = MagicMock()
        bot_service.handle_text.return_value = "response to voice"
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
            voice_api_key="test-key",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.voice.file_id = "file123"
        update.message.voice.file_unique_id = "uniq123"
        update.message.voice.file_size = 1024
        update.message.reply_text = AsyncMock()

        mock_file = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch("claw_v2.telegram.transcribe", new_callable=AsyncMock, return_value="hola"):
            with patch("claw_v2.telegram.asyncio") as mock_asyncio:
                mock_asyncio.to_thread = AsyncMock(return_value="response to voice")
                await transport._handle_voice(update, mock_context)

        update.message.reply_text.assert_awaited()

    async def test_voice_without_api_key_replies_error(self) -> None:
        from claw_v2.voice import VoiceUnavailableError

        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.message.voice.file_id = "file123"
        update.message.voice.file_unique_id = "uniq123"
        update.message.voice.file_size = 1024
        update.message.reply_text = AsyncMock()

        mock_file = AsyncMock()
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch(
            "claw_v2.telegram.transcribe",
            new_callable=AsyncMock,
            side_effect=VoiceUnavailableError("no key"),
        ):
            await transport._handle_voice(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        call_args = update.message.reply_text.call_args[0][0]
        self.assertIn("not available", call_args.lower())


class HandleImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_message_downloaded_and_forwarded_as_multimodal(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.caption = "revisa esta foto"
        update.message.photo = [
            MagicMock(file_id="small", file_unique_id="uniq1", file_size=512),
            MagicMock(file_id="large", file_unique_id="uniq1", file_size=4096),
        ]
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        mock_file = AsyncMock()
        mock_file.file_path = "photos/test.png"

        async def download_to_drive(path: str) -> None:
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

        mock_file.download_to_drive.side_effect = download_to_drive
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch(
            "claw_v2.telegram.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=lambda func, *a, **k: (
                func(*a, **k) if func is _build_image_content_blocks else "image response"
            ),
        ) as mock_to_thread:
            await transport._handle_photo(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(update.message.reply_text.await_args.args[0], "image response")
        # T6: the up-to-20MB read+base64 encode must run off the event loop.
        self.assertTrue(
            any(
                call.args and call.args[0] is _build_image_content_blocks
                for call in mock_to_thread.await_args_list
            )
        )
        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        self.assertTrue(
            any(
                name == "telegram_outbound_sent" and payload["message_kind"] == "image"
                for name, payload in events
            )
        )
        payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(payload["message_kind"], "image")
        self.assertEqual(payload["response_parts"], 1)
        _, kwargs = mock_to_thread.await_args
        self.assertEqual(kwargs["user_id"], "123")
        self.assertEqual(kwargs["session_id"], "tg-1")
        self.assertIn("[Imagen adjunta] path:", kwargs["memory_text"])
        self.assertIn("revisa esta foto", kwargs["memory_text"])
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["text"], "revisa esta foto")
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/png")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), b"\x89PNG\r\n\x1a\n")

    async def test_image_document_without_caption_uses_default_prompt(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.caption = None
        update.message.document.file_id = "doc1"
        update.message.document.file_unique_id = "uniq-doc"
        update.message.document.mime_type = "image/jpeg"
        update.message.document.file_size = 2048
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        mock_file = AsyncMock()
        mock_file.file_path = "docs/test.jpg"

        async def download_to_drive(path: str) -> None:
            Path(path).write_bytes(b"\xff\xd8\xff")

        mock_file.download_to_drive.side_effect = download_to_drive
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with patch(
            "claw_v2.telegram.asyncio.to_thread",
            new_callable=AsyncMock,
            side_effect=lambda func, *a, **k: (
                func(*a, **k) if func is _build_image_content_blocks else "doc response"
            ),
        ) as mock_to_thread:
            await transport._handle_image_document(update, mock_context)

        update.message.reply_text.assert_awaited_once()
        self.assertEqual(update.message.reply_text.await_args.args[0], "doc response")
        _, kwargs = mock_to_thread.await_args
        self.assertIn("[Imagen adjunta] path:", kwargs["memory_text"])
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("Telegram", blocks[0]["text"])
        self.assertEqual(blocks[1]["source"]["media_type"], "image/jpeg")

    async def test_handle_text_content_emits_transcript_latency(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()

        with patch(
            "claw_v2.telegram.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value="voice response",
        ):
            await transport._handle_text_content(update, "hola")

        events = [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]
        self.assertTrue(
            any(
                name == "telegram_outbound_sent" and payload["message_kind"] == "transcript"
                for name, payload in events
            )
        )
        payload = next(payload for name, payload in events if name == "telegram_latency")
        self.assertEqual(payload["message_kind"], "transcript")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["response_chars"], len("voice response"))

    async def test_handle_text_content_suppresses_when_response_is_none(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        bot_service.is_voice_mode = MagicMock(return_value=None)
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.reply_text = AsyncMock()
        update.message.reply_voice = AsyncMock()

        with patch("claw_v2.telegram.asyncio.to_thread", new_callable=AsyncMock, return_value=None):
            await transport._handle_text_content(update, "arranca research")

        update.message.reply_text.assert_not_awaited()
        update.message.reply_voice.assert_not_awaited()

        bot_service.observe.emit.assert_called_once()
        payload = bot_service.observe.emit.call_args.kwargs["payload"]
        self.assertEqual(payload["message_kind"], "transcript")
        self.assertEqual(payload["status"], "suppressed")
        self.assertEqual(payload["response_chars"], 0)
        self.assertEqual(payload["response_parts"], 0)


class HandleVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_without_audio_falls_back_to_multimodal_frames(self) -> None:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        update = MagicMock()
        update.effective_user.id = 123
        update.effective_chat.id = 1
        update.message.caption = "revisa este video"
        update.message.video = SimpleNamespace(
            file_id="video-file",
            file_unique_id="video-uniq",
            file_size=4096,
        )
        update.message.video_note = None
        update.message.reply_text = AsyncMock()
        update.message.chat.send_action = AsyncMock()

        mock_file = AsyncMock()

        async def download_to_drive(path: str) -> None:
            Path(path).write_bytes(b"fake-mp4")

        mock_file.download_to_drive.side_effect = download_to_drive
        mock_context = MagicMock()
        mock_context.bot.get_file = AsyncMock(return_value=mock_file)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            frame_path = tmp_root / "frame-01.jpg"
            frame_path.write_bytes(b"\xff\xd8\xff")
            with patch("claw_v2.telegram._VIDEOS_DIR", tmp_root / "videos"):
                with patch(
                    "claw_v2.telegram.extract_audio",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("no audio"),
                ):
                    with patch(
                        "claw_v2.telegram._extract_video_frame_paths",
                        new_callable=AsyncMock,
                        return_value=([frame_path], 38.0),
                    ) as mock_frames:
                        with patch(
                            "claw_v2.telegram.asyncio.to_thread",
                            new_callable=AsyncMock,
                            side_effect=lambda func, *a, **k: (
                                func(*a, **k)
                                if func is _build_video_content_blocks
                                else "video response"
                            ),
                        ) as mock_to_thread:
                            await transport._handle_video(update, mock_context)

        update.message.reply_text.assert_awaited()
        self.assertEqual(update.message.reply_text.await_args.args[0], "video response")
        mock_frames.assert_awaited_once()
        # T6: the frame read+base64 encode must run off the event loop.
        self.assertTrue(
            any(
                call.args and call.args[0] is _build_video_content_blocks
                for call in mock_to_thread.await_args_list
            )
        )
        _, kwargs = mock_to_thread.await_args
        self.assertEqual(kwargs["user_id"], "123")
        self.assertEqual(kwargs["session_id"], "tg-1")
        self.assertIn("[Video adjunto]", kwargs["memory_text"])
        blocks = kwargs["content_blocks"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertIn("revisa este video", blocks[0]["text"])
        self.assertIn("Audio", blocks[0]["text"])
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["media_type"], "image/jpeg")
        self.assertEqual(base64.b64decode(blocks[1]["source"]["data"]), b"\xff\xd8\xff")
        latency_payload = [
            call.kwargs["payload"]
            for call in bot_service.observe.emit.call_args_list
            if call.args[0] == "telegram_latency"
        ][0]
        self.assertEqual(latency_payload["message_kind"], "video")
        self.assertEqual(latency_payload["status"], "ok")


class SendPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_screenshot_sends_photo_to_chat(self) -> None:
        bot_service = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._app = MagicMock()
        mock_bot = AsyncMock()
        transport._app.bot = mock_bot

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            tmp_path = tmp.name

        try:
            await transport.send_photo(chat_id=1, photo_path=tmp_path, caption="screenshot")
            mock_bot.send_photo.assert_awaited_once()
            call_kwargs = mock_bot.send_photo.call_args
            self.assertEqual(call_kwargs.kwargs["chat_id"], 1)
            self.assertEqual(call_kwargs.kwargs["caption"], "screenshot")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    async def test_send_photo_treats_broken_pipe_as_nonfatal(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="123",
        )
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        transport._app.bot.send_photo.side_effect = BrokenPipeError("EPIPE")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            tmp_path = tmp.name

        try:
            sent = await transport.send_photo(chat_id=1, photo_path=tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        self.assertFalse(sent)

    async def test_send_photo_returns_false_on_unexpected_error(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="123",
        )
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        transport._app.bot.send_photo.side_effect = BadRequest("PHOTO_INVALID_DIMENSIONS")

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(b"\x89PNG\r\n\x1a\n")
            tmp_path = tmp.name

        try:
            sent = await transport.send_photo(chat_id=1, photo_path=tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        self.assertFalse(sent)

    async def test_send_text_treats_connection_reset_as_nonfatal(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="123",
        )
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        transport._app.bot.send_message.side_effect = ConnectionResetError("reset")
        transport._send_text_direct_bot_api = AsyncMock(return_value=False)

        delivered = await transport.send_text(chat_id=1, text="hello")

        self.assertFalse(delivered)
        transport._app.bot.send_message.assert_awaited_once()

    async def test_send_text_sanitizes_proactive_internal_details(self) -> None:
        transport = TelegramTransport(
            bot_service=MagicMock(),
            token="t",
            allowed_user_id="123",
        )
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()

        await transport.send_text(
            chat_id=574707975,
            text=(
                "Task brain-tooluse:tg-574707975:1779208007773945000 "
                "failed: runtime lost authoritative backing state"
            ),
        )

        sent_text = transport._app.bot.send_message.call_args.kwargs["text"]
        self.assertNotIn("brain-tooluse", sent_text)
        self.assertNotIn("tg-574707975", sent_text)
        self.assertNotIn("runtime lost authoritative backing state", sent_text)
        self.assertIn("se perdio el estado ejecutable", sent_text)


class ProactiveSendTextTests(unittest.IsolatedAsyncioTestCase):
    """send_text is the proactive path (task-completion notifications,
    observability alerts, NotebookLM): it must retry retryable errors,
    fall back to the direct Bot API, report failure as False, and never
    deliver a later part after an earlier part was lost (T1, 2026-06-12)."""

    def _transport(self) -> tuple[TelegramTransport, MagicMock]:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(
            bot_service=bot_service,
            token="t",
            allowed_user_id="123",
        )
        transport._text_send_retries = 2
        transport._text_send_retry_delay = 0.0
        transport._app = MagicMock()
        transport._app.bot = AsyncMock()
        return transport, bot_service

    @staticmethod
    def _events(bot_service: MagicMock) -> list[tuple[str, dict]]:
        return [
            (call.args[0], call.kwargs["payload"])
            for call in bot_service.observe.emit.call_args_list
        ]

    async def test_retries_flood_control_then_delivers(self) -> None:
        transport, bot_service = self._transport()
        transport._app.bot.send_message.side_effect = [
            RetryAfter(retry_after=0),
            SimpleNamespace(message_id=5),
        ]

        delivered = await transport.send_text(chat_id=1, text="hello")

        self.assertTrue(delivered)
        self.assertEqual(transport._app.bot.send_message.await_count, 2)
        events = self._events(bot_service)
        self.assertTrue(
            any(
                name == "telegram_outbound_sent"
                and payload["message_kind"] == "proactive"
                and payload["attempt"] == 2
                and payload["message_id"] == 5
                for name, payload in events
            )
        )

    async def test_exhausted_part_returns_false_and_skips_remaining_parts(self) -> None:
        transport, bot_service = self._transport()
        transport._app.bot.send_message.side_effect = TimedOut("Timed out")
        transport._send_text_direct_bot_api = AsyncMock(return_value=False)

        delivered = await transport.send_text(chat_id=1, text="a" * 5000)

        self.assertFalse(delivered)
        # Both attempts belong to part 1; part 2 is never attempted.
        sent_texts = {
            call.kwargs["text"] for call in transport._app.bot.send_message.await_args_list
        }
        self.assertEqual(sent_texts, {"a" * 4000})
        transport._send_text_direct_bot_api.assert_awaited_once()
        events = self._events(bot_service)
        self.assertTrue(
            any(
                name == "telegram_outbound_error"
                and payload["message_kind"] == "proactive"
                and payload["error_type"] == "TimedOut"
                for name, payload in events
            )
        )

    async def test_falls_back_to_direct_bot_api_when_ptb_exhausted(self) -> None:
        transport, bot_service = self._transport()
        transport._app.bot.send_message.side_effect = TimedOut("Timed out")
        transport._send_text_direct_bot_api = AsyncMock(return_value=True)

        delivered = await transport.send_text(chat_id=1, text="hello")

        self.assertTrue(delivered)
        transport._send_text_direct_bot_api.assert_awaited_once()
        self.assertEqual(
            transport._send_text_direct_bot_api.await_args.kwargs["message_kind"],
            "proactive",
        )

    async def test_parse_mode_blocks_direct_fallback(self) -> None:
        transport, bot_service = self._transport()
        transport._app.bot.send_message.side_effect = TimedOut("Timed out")
        transport._send_text_direct_bot_api = AsyncMock(return_value=True)

        delivered = await transport.send_text(chat_id=1, text="hello", parse_mode="Markdown")

        self.assertFalse(delivered)
        transport._send_text_direct_bot_api.assert_not_awaited()

    async def test_attempt_telemetry_is_not_awaited_in_send_path(self) -> None:
        """telegram_outbound_attempt rides the single-worker executor without
        an await: under SQLite contention each awaited emit adds up to ~0.3s
        per part to the send path (T5, 2026-06-12). The awaited final
        sent/error emit doubles as the ordering barrier."""
        transport, bot_service = self._transport()
        transport._app.bot.send_message.return_value = SimpleNamespace(message_id=1)
        awaited_types: list[str] = []
        original = transport._aemit_transport_event

        async def spy(event_type: str, payload: dict) -> None:
            awaited_types.append(event_type)
            await original(event_type, payload)

        transport._aemit_transport_event = spy

        delivered = await transport.send_text(chat_id=1, text="hello")

        self.assertTrue(delivered)
        self.assertNotIn("telegram_outbound_attempt", awaited_types)
        emitted = [call.args[0] for call in bot_service.observe.emit.call_args_list]
        self.assertIn("telegram_outbound_attempt", emitted)
        self.assertLess(
            emitted.index("telegram_outbound_attempt"),
            emitted.index("telegram_outbound_sent"),
        )

    async def test_nowait_emit_survives_executor_shutdown_race(self) -> None:
        """executor.submit can raise RuntimeError if the observe executor was
        shut down between the lookup and the submit (stop() racing an in-flight
        send). Telemetry must never crash delivery (gemini review #100)."""
        transport, bot_service = self._transport()
        executor = transport._observe_emit_executor()
        executor.shutdown(wait=True)  # now submit() raises RuntimeError

        # Must not raise, and must keep the audit event via the inline fallback.
        transport._emit_outbound_text_event_nowait(
            "telegram_outbound_attempt",
            session_id="tg-1",
            user_id="1",
            message_kind="proactive",
            method="send_message",
            part_index=1,
            part_count=1,
            part_chars=5,
            attempt=1,
        )
        emitted = [call.args[0] for call in bot_service.observe.emit.call_args_list]
        self.assertIn("telegram_outbound_attempt", emitted)

    async def test_nonretryable_error_does_not_retry(self) -> None:
        transport, bot_service = self._transport()
        transport._app.bot.send_message.side_effect = BadRequest("MESSAGE_TOO_LONG")
        transport._send_text_direct_bot_api = AsyncMock(return_value=False)

        delivered = await transport.send_text(chat_id=1, text="hello")

        self.assertFalse(delivered)
        transport._app.bot.send_message.assert_awaited_once()


class ReplyHtmlWiringTests(unittest.IsolatedAsyncioTestCase):
    """Brain replies render to Telegram HTML, and a parse failure degrades the
    part to plain text instead of losing the message (Fase 1,
    reports/2026-06-13/telegram_rich_messages_mapping.md)."""

    def _transport(self) -> TelegramTransport:
        bot_service = MagicMock()
        bot_service.observe = MagicMock()
        transport = TelegramTransport(bot_service=bot_service, token="t")
        transport._text_send_retry_delay = 0.0
        # Force the flag on so the helper is hermetic to TELEGRAM_REPLY_HTML in
        # the runner's environment.
        transport._reply_html_enabled = True
        return transport

    async def test_markdown_rendered_as_html_with_parse_mode(self) -> None:
        transport = self._transport()
        update = MagicMock()
        update.message.reply_text = AsyncMock()

        sent = await transport._send_reply_text_part(
            update,
            "Esto es **negrita** y `code`",
            session_id="tg-1",
            user_id="1",
            message_kind="text",
            part_index=1,
            part_count=1,
        )

        self.assertTrue(sent)
        update.message.reply_text.assert_awaited_once()
        text_arg = update.message.reply_text.await_args.args[0]
        self.assertIn("<b>negrita</b>", text_arg)
        self.assertIn("<code>code</code>", text_arg)
        self.assertEqual(update.message.reply_text.await_args.kwargs["parse_mode"], "HTML")

    async def test_parse_error_degrades_to_plain_text(self) -> None:
        transport = self._transport()
        # 2 attempts so the same reply_text path can retry as plain after the
        # HTML attempt is rejected.
        transport._text_send_retries = 2
        update = MagicMock()
        update.message.reply_text = AsyncMock(
            side_effect=[
                BadRequest("Can't parse entities: unsupported start tag"),
                SimpleNamespace(message_id=5),
            ]
        )

        sent = await transport._send_reply_text_part(
            update,
            "texto con **negrita**",
            session_id="tg-1",
            user_id="1",
            message_kind="text",
            part_index=1,
            part_count=1,
        )

        self.assertTrue(sent)
        self.assertEqual(update.message.reply_text.await_count, 2)
        first = update.message.reply_text.await_args_list[0]
        second = update.message.reply_text.await_args_list[1]
        self.assertEqual(first.kwargs["parse_mode"], "HTML")
        # Degraded retry: plain text, no parse_mode, no HTML tags.
        self.assertIsNone(second.kwargs["parse_mode"])
        self.assertEqual(second.args[0], "texto con negrita")
        event_names = [call.args[0] for call in transport._bot_service.observe.emit.call_args_list]
        self.assertIn("telegram_outbound_degraded", event_names)

    async def test_html_disabled_sends_plain(self) -> None:
        transport = self._transport()
        transport._reply_html_enabled = False
        update = MagicMock()
        update.message.reply_text = AsyncMock()

        await transport._send_reply_text_part(
            update,
            "deja **esto** crudo",
            session_id="tg-1",
            user_id="1",
            message_kind="text",
            part_index=1,
            part_count=1,
        )

        text_arg = update.message.reply_text.await_args.args[0]
        self.assertEqual(text_arg, "deja **esto** crudo")
        self.assertIsNone(update.message.reply_text.await_args.kwargs["parse_mode"])


class ObserveEmitOffloadTests(unittest.IsolatedAsyncioTestCase):
    """The transport persists diagnostic observe events on the event loop while
    handling Telegram traffic. ObserveStream._persist_event retries
    locked-SQLite writes with a synchronous time.sleep (up to ~0.3s), so doing
    that work inline on the loop thread stutters all Telegram handling during a
    lock storm (2026-06-10 incident). The async emit path must offload the
    blocking write to a worker thread."""

    def _locked_observe_stream(self) -> ObserveStream:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        stream = ObserveStream(Path(tmp.name) / "observe.db")
        # Close the real connection we are about to orphan, then force every
        # persist to take the locked-retry-then-drop branch so the time.sleep
        # in _persist_event actually runs.
        stream._conn.close()
        fake_conn = MagicMock()
        fake_conn.execute.side_effect = sqlite3.OperationalError("database is locked")
        stream._conn = fake_conn
        return stream

    async def test_async_outbound_emit_does_not_sleep_on_loop_thread(self) -> None:
        stream = self._locked_observe_stream()
        bot_service = MagicMock()
        bot_service.observe = stream
        transport = TelegramTransport(bot_service=bot_service, token="t")
        self.addAsyncCleanup(transport.stop)

        loop_thread_id = threading.get_ident()
        sleep_threads: list[int] = []

        def _recording_sleep(_seconds: float) -> None:
            # Record which thread paid the retry sleep; never actually block.
            sleep_threads.append(threading.get_ident())

        update = MagicMock()
        update.message.reply_text = AsyncMock()

        with patch.object(observe_module.time, "sleep", _recording_sleep):
            await transport._send_reply_text_part(
                update,
                "hola",
                session_id="tg-1",
                user_id="1",
                message_kind="text",
                part_index=1,
                part_count=1,
            )

        # The locked-retry sleep must have run (otherwise we never exercised the
        # blocking path and the assertion below would pass vacuously)...
        self.assertTrue(
            sleep_threads,
            "expected ObserveStream locked-retry time.sleep to run",
        )
        # ...but it must never run on the event-loop thread.
        self.assertNotIn(
            loop_thread_id,
            sleep_threads,
            "observe locked-retry time.sleep ran on the event-loop thread",
        )

    async def test_emit_after_stop_runs_inline_without_new_executor(self) -> None:
        bot_service = MagicMock()
        transport = TelegramTransport(bot_service=bot_service, token="t")
        await transport.stop()  # never started: _app stays None, executor closed

        await transport._aemit_transport_event("telegram_outbound_text", {"k": "v"})

        # A shutdown-tail emit must not resurrect an executor nobody will shut
        # down again...
        self.assertIsNone(transport._observe_executor)
        # ...but the audit event itself must still land (inline fallback).
        bot_service.observe.emit.assert_called_once_with(
            "telegram_outbound_text", payload={"k": "v"}
        )

    async def test_async_latency_emit_does_not_sleep_on_loop_thread(self) -> None:
        stream = self._locked_observe_stream()
        bot_service = MagicMock()
        bot_service.observe = stream
        transport = TelegramTransport(bot_service=bot_service, token="t")
        self.addAsyncCleanup(transport.stop)

        loop_thread_id = threading.get_ident()
        sleep_threads: list[int] = []

        def _recording_sleep(_seconds: float) -> None:
            sleep_threads.append(threading.get_ident())

        with patch.object(observe_module.time, "sleep", _recording_sleep):
            await transport._emit_latency(
                session_id="tg-1",
                user_id="1",
                message_kind="text",
                status="ok",
                bot_ms=1.0,
                reply_ms=1.0,
                total_ms=2.0,
                response_parts=1,
                response_chars=4,
            )

        self.assertTrue(sleep_threads, "expected latency emit to reach the retry path")
        self.assertNotIn(
            loop_thread_id,
            sleep_threads,
            "latency emit locked-retry time.sleep ran on the event-loop thread",
        )


if __name__ == "__main__":
    unittest.main()
