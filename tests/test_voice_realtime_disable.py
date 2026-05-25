"""Tests for voice.py Realtime TTS 24h disable on beta-shape (P0 hotfix D).

The OpenAI Realtime API was returning WS 4000 invalid_request_error
beta_api_shape_disabled for every voice note on 2026-05-24. Each call
paid a websocket round-trip before falling back to batch — disable the
backend for 24h so the bot stops re-burning that round-trip.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from claw_v2.voice import (
    REALTIME_DISABLE_DURATION_SECONDS,
    disable_realtime_tts,
    is_realtime_beta_shape_error,
    is_realtime_tts_disabled,
    realtime_tts_disabled_until,
    set_realtime_disable_state_path,
    synthesize_voice_note,
)


class _StateIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = Path(tempfile.mkdtemp())
        self.state_path = self.state_dir / "realtime_state.json"
        set_realtime_disable_state_path(self.state_path)
        self.addCleanup(lambda: set_realtime_disable_state_path(None))


class BetaShapeDetectionTests(unittest.TestCase):
    def test_detects_beta_api_shape_disabled_phrase(self) -> None:
        self.assertTrue(
            is_realtime_beta_shape_error(RuntimeError("invalid_request_error.beta_api_shape_disabled"))
        )

    def test_detects_ws_4000_invalid_request_error(self) -> None:
        self.assertTrue(
            is_realtime_beta_shape_error(
                RuntimeError("received 4000 (private use) invalid_request_error.something")
            )
        )

    def test_unrelated_errors_are_ignored(self) -> None:
        self.assertFalse(is_realtime_beta_shape_error(RuntimeError("timeout waiting for audio")))
        self.assertFalse(is_realtime_beta_shape_error(RuntimeError("rate limit exceeded")))


class RealtimeBetaShapeDisablesFor24hTests(_StateIsolated):
    def test_realtime_tts_beta_shape_disabled_disables_for_24h(self) -> None:
        self.assertFalse(is_realtime_tts_disabled())

        before = time.time()
        until = disable_realtime_tts(reason="beta_api_shape_disabled")
        after = time.time()

        self.assertTrue(is_realtime_tts_disabled())
        disabled_until = realtime_tts_disabled_until()
        self.assertIsNotNone(disabled_until)
        # Allow ±2s drift between disable() return and the persisted value.
        self.assertAlmostEqual(disabled_until, until, delta=2.0)
        # 24h ± small drift.
        self.assertGreaterEqual(disabled_until, before + REALTIME_DISABLE_DURATION_SECONDS - 2)
        self.assertLessEqual(disabled_until, after + REALTIME_DISABLE_DURATION_SECONDS + 2)


class RealtimeBetaShapeAutoDisableOnErrorTests(_StateIsolated):
    def test_beta_shape_error_in_synthesis_triggers_24h_disable(self) -> None:
        events: list[tuple[str, dict]] = []

        async def fake_realtime(*_, **__):
            raise RuntimeError("received 4000 (private use) invalid_request_error.beta_api_shape_disabled")

        async def fake_batch_chain(*_, **__):
            tmp = Path(tempfile.mkstemp(suffix=".ogg")[1])
            tmp.write_bytes(b"OggS")
            return tmp

        with (
            patch("claw_v2.voice._synthesize_realtime", side_effect=fake_realtime),
            patch("claw_v2.voice._synthesize_xai", side_effect=AsyncMock(side_effect=NotImplementedError)),
            patch("claw_v2.voice.synthesize", side_effect=AsyncMock(side_effect=NotImplementedError)),
            patch("claw_v2.voice._synthesize_edge", new=AsyncMock(side_effect=fake_batch_chain)),
            patch("claw_v2.voice._mp3_to_ogg", new=AsyncMock(side_effect=lambda p: p)),
        ):
            asyncio.run(
                synthesize_voice_note(
                    "hola",
                    api_key="sk-fake",
                    prefer_realtime=True,
                    observe=lambda et, payload: events.append((et, payload)),
                )
            )

        self.assertTrue(is_realtime_tts_disabled())
        event_types = [name for name, _ in events]
        self.assertIn("realtime_tts_disabled_beta_shape", event_types)


class VoiceNoteUsesBatchWhenRealtimeDisabledTests(_StateIsolated):
    def test_voice_note_uses_batch_when_realtime_disabled(self) -> None:
        disable_realtime_tts(reason="beta_api_shape_disabled")

        realtime_calls = 0
        batch_calls = 0

        async def fake_realtime(*_, **__):
            nonlocal realtime_calls
            realtime_calls += 1
            return Path("should-not-be-used.wav")

        async def fake_edge(text: str, *, voice: str = "nova"):
            nonlocal batch_calls
            batch_calls += 1
            tmp = Path(tempfile.mkstemp(suffix=".mp3")[1])
            tmp.write_bytes(b"FAKEMP3")
            return tmp

        async def fake_mp3_to_ogg(mp3_path: Path) -> Path:
            ogg = Path(tempfile.mkstemp(suffix=".ogg")[1])
            ogg.write_bytes(b"OggS")
            return ogg

        with (
            patch("claw_v2.voice._synthesize_realtime", side_effect=fake_realtime),
            patch("claw_v2.voice.synthesize", side_effect=AsyncMock(side_effect=RuntimeError("openai down"))),
            patch("claw_v2.voice._synthesize_edge", new=AsyncMock(side_effect=fake_edge)),
            patch("claw_v2.voice._mp3_to_ogg", new=AsyncMock(side_effect=fake_mp3_to_ogg)),
        ):
            result = asyncio.run(
                synthesize_voice_note(
                    "hola",
                    api_key="sk-fake",
                    prefer_realtime=True,
                )
            )

        self.assertEqual(realtime_calls, 0)
        self.assertEqual(batch_calls, 1)
        self.assertTrue(result.exists())


if __name__ == "__main__":
    unittest.main()
