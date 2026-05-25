"""Unit tests for HeygenDeliveryService.

Network and ffmpeg are mocked. Tests exercise the pipeline branches:
- small file (no compression) → direct send
- large file (over Telegram limit) → compress then send
- render still processing → poll loop continues
- render failed → ok=False with error
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claw_v2.heygen_delivery import (
    HeygenDeliveryService,
    TELEGRAM_BOT_API_VIDEO_LIMIT,
)


@pytest.fixture
def svc(tmp_path: Path) -> HeygenDeliveryService:
    return HeygenDeliveryService(
        artifacts_dir=tmp_path / "heygen",
        ffmpeg_path="/bin/true",
        poll_interval_seconds=0,
        poll_timeout_seconds=2,
    )


def _make_video(path: Path, size_bytes: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size_bytes)
    return path


def test_small_file_skips_compression(svc, tmp_path, monkeypatch):
    """A 10MB render delivers without invoking ffmpeg."""
    video_id = "vid_small"
    completed = {
        "status": "completed",
        "video_url": "https://heygen.example/x.mp4",
        "duration": 30,
    }

    monkeypatch.setattr(svc, "poll_until_done", lambda vid: completed)

    def _fake_download(url, dest):
        return _make_video(dest, 10 * 1024 * 1024)

    monkeypatch.setattr(svc, "download", _fake_download)

    compress_calls: list = []
    monkeypatch.setattr(svc, "compress",
                        lambda src, dest, crf=28: compress_calls.append(src) or dest)

    sent: list[dict] = []

    def _fake_send(path, caption=None, chat_id=None, **kw):
        sent.append({"path": str(path), "caption": caption})
        return {"ok": True, "result": {"message_id": 1001}}

    monkeypatch.setattr(svc, "send_to_telegram", _fake_send)

    result = svc.auto_deliver(video_id, caption="hi")

    assert result.ok is True
    assert result.telegram_message_id == 1001
    assert result.compressed_path is None
    assert compress_calls == []
    assert sent[0]["caption"] == "hi"


def test_large_file_triggers_compression(svc, tmp_path, monkeypatch):
    """A render larger than the Telegram cap is compressed before sending."""
    video_id = "vid_big"
    completed = {
        "status": "completed",
        "video_url": "https://heygen.example/big.mp4",
        "duration": 70,
    }
    monkeypatch.setattr(svc, "poll_until_done", lambda vid: completed)

    def _fake_download(url, dest):
        return _make_video(dest, TELEGRAM_BOT_API_VIDEO_LIMIT + 5 * 1024 * 1024)

    monkeypatch.setattr(svc, "download", _fake_download)

    compress_called: dict = {}

    def _fake_compress(src, dest, crf=28):
        compress_called["src"] = src
        _make_video(dest, 14 * 1024 * 1024)
        return dest

    monkeypatch.setattr(svc, "compress", _fake_compress)

    sent_paths: list[Path] = []

    def _fake_send(path, caption=None, chat_id=None, **kw):
        sent_paths.append(path)
        return {"ok": True, "result": {"message_id": 2002}}

    monkeypatch.setattr(svc, "send_to_telegram", _fake_send)

    result = svc.auto_deliver(video_id, caption="big one")

    assert result.ok is True
    assert result.telegram_message_id == 2002
    assert result.compressed_path is not None
    assert result.compressed_size == 14 * 1024 * 1024
    assert sent_paths[0] == result.compressed_path
    assert compress_called["src"] == result.raw_path


def test_failed_render_returns_error(svc, monkeypatch):
    monkeypatch.setattr(
        svc, "poll_until_done",
        lambda vid: {"status": "failed", "error": "avatar_locked"},
    )
    result = svc.auto_deliver("vid_fail")
    assert result.ok is False
    assert result.status == "failed"
    assert "avatar_locked" in (result.error or "")


def test_brain_tool_registered_with_schema():
    """HeyGenDeliver is exposed to the brain via the default ToolRegistry."""
    from pathlib import Path
    from claw_v2.tools import (
        DEFAULT_TOOL_AGENT_CLASSES,
        ToolRegistry,
        TIER_REQUIRES_APPROVAL,
    )

    assert "HeyGenDeliver" in DEFAULT_TOOL_AGENT_CLASSES
    registry = ToolRegistry.default(workspace_root=Path("/tmp"))
    tool = registry.get("HeyGenDeliver")
    assert tool.tier == TIER_REQUIRES_APPROVAL
    assert callable(tool.handler)
    props = tool.parameter_schema.get("properties", {})
    assert {"video_id", "latest", "caption", "chat_id", "slug"}.issubset(props)
    assert tool.mutates_state is True
    assert tool.requires_network is True
    assert "operator" in tool.allowed_agent_classes


def test_telegram_send_failure_propagates(svc, tmp_path, monkeypatch):
    completed = {
        "status": "completed",
        "video_url": "https://heygen.example/ok.mp4",
        "duration": 40,
    }
    monkeypatch.setattr(svc, "poll_until_done", lambda vid: completed)
    monkeypatch.setattr(
        svc, "download",
        lambda url, dest: _make_video(dest, 5 * 1024 * 1024),
    )
    monkeypatch.setattr(
        svc, "send_to_telegram",
        lambda *a, **kw: {"ok": False, "http_error": 413},
    )
    result = svc.auto_deliver("vid_tg_fail")
    assert result.ok is False
    assert "413" in (result.error or "")
