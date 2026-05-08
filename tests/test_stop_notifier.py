"""Tests for stop_notifier."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from claw_v2.stop_notifier import StopNotifier, build_stop_notifier


def _wait_for_threads():
    # Give daemon threads a moment to call the mocked transport.
    for _ in range(50):
        time.sleep(0.02)


def test_stop_notifier_skips_short_tasks_by_default():
    notifier = StopNotifier(token="t", default_chat_id="c")
    with patch("urllib.request.urlopen") as urlopen:
        sent = notifier.notify_completion(
            task_id="t-1",
            kind="quick_reply",
            status="succeeded",
            summary="ok",
            duration_sec=5.0,
        )
        assert sent is False
        _wait_for_threads()
        assert urlopen.call_count == 0


def test_stop_notifier_emits_long_tasks():
    notifier = StopNotifier(token="t", default_chat_id="c", long_running_sec=10.0)
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        sent = notifier.notify_completion(
            task_id="t-2",
            kind="autonomous",
            status="succeeded",
            summary="rendered video",
            duration_sec=120.0,
        )
        assert sent is True
        _wait_for_threads()
        assert urlopen.call_count == 1


def test_stop_notifier_force_overrides_gate():
    notifier = StopNotifier(token="t", default_chat_id="c")
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        sent = notifier.notify_completion(
            task_id="t-3",
            kind="critical",
            status="failed",
            summary="boom",
            duration_sec=2.0,
            force=True,
        )
        assert sent is True
        _wait_for_threads()
        assert urlopen.call_count == 1


def test_stop_notifier_dedupes_repeats():
    notifier = StopNotifier(token="t", default_chat_id="c", long_running_sec=1.0)
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.return_value.__enter__.return_value.read.return_value = b"ok"
        first = notifier.notify_completion(
            task_id="t-4",
            kind="autonomous",
            status="succeeded",
            summary="x",
            duration_sec=10.0,
        )
        second = notifier.notify_completion(
            task_id="t-4",
            kind="autonomous",
            status="succeeded",
            summary="x",
            duration_sec=10.0,
        )
        assert first is True
        assert second is False
        _wait_for_threads()
        assert urlopen.call_count == 1


def test_stop_notifier_disabled_short_circuit():
    notifier = StopNotifier(token="t", default_chat_id="c", enabled=False)
    with patch("urllib.request.urlopen") as urlopen:
        sent = notifier.notify_completion(
            task_id="t-5",
            kind="autonomous",
            status="succeeded",
            summary="x",
            duration_sec=600.0,
        )
        assert sent is False
        _wait_for_threads()
        assert urlopen.call_count == 0


def test_stop_notifier_no_token_short_circuit():
    notifier = StopNotifier(token="", default_chat_id="c")
    with patch("urllib.request.urlopen") as urlopen:
        sent = notifier.notify_completion(
            task_id="t-6",
            kind="autonomous",
            status="succeeded",
            summary="x",
            duration_sec=600.0,
            force=True,
        )
        assert sent is False
        _wait_for_threads()
        assert urlopen.call_count == 0


def test_stop_notifier_swallows_network_failure():
    notifier = StopNotifier(token="t", default_chat_id="c", long_running_sec=1.0)
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        sent = notifier.notify_completion(
            task_id="t-7",
            kind="autonomous",
            status="succeeded",
            summary="x",
            duration_sec=10.0,
        )
        # Send is queued (returns True); the actual network failure is swallowed
        # by the daemon thread without raising.
        assert sent is True
        _wait_for_threads()


def test_format_message_includes_emoji_and_duration():
    msg = StopNotifier._format_message(
        kind="autonomous_task",
        status="succeeded",
        summary="rendered the promo video",
        duration_sec=180.0,
    )
    assert msg.startswith("✅")
    assert "autonomous task" in msg
    assert "succeeded" in msg
    assert "3.0m" in msg
    assert "rendered the promo video" in msg


def test_format_message_failed_emoji():
    msg = StopNotifier._format_message(
        kind="render",
        status="failed",
        summary="image_to_video timeout",
        duration_sec=45.0,
    )
    assert msg.startswith("❌")
    assert "45s" in msg


def test_format_message_blocked_emoji():
    msg = StopNotifier._format_message(
        kind="approval_pending",
        status="blocked",
        summary="needs human",
        duration_sec=None,
    )
    assert msg.startswith("⚠️")
    assert "blocked" in msg


def test_build_stop_notifier_disabled_when_no_token():
    config = MagicMock()
    config.telegram_bot_token = ""
    config.telegram_allowed_user_id = "123"
    notifier = build_stop_notifier(config=config)
    assert notifier is None


def test_build_stop_notifier_disabled_when_no_chat():
    config = MagicMock()
    config.telegram_bot_token = "abc"
    config.telegram_allowed_user_id = ""
    notifier = build_stop_notifier(config=config)
    assert notifier is None


def test_build_stop_notifier_returns_instance_when_configured():
    config = MagicMock()
    config.telegram_bot_token = "abc"
    config.telegram_allowed_user_id = "123"
    config.stop_notifier_enabled = True
    notifier = build_stop_notifier(config=config)
    assert isinstance(notifier, StopNotifier)
    assert notifier.token == "abc"
    assert notifier.default_chat_id == "123"
    assert notifier.enabled is True


def test_build_stop_notifier_respects_disabled_flag():
    config = MagicMock()
    config.telegram_bot_token = "abc"
    config.telegram_allowed_user_id = "123"
    notifier = build_stop_notifier(config=config, enabled=False)
    assert isinstance(notifier, StopNotifier)
    assert notifier.enabled is False
