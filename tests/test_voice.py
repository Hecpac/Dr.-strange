from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.voice import (
    XAI_DEFAULT_LANGUAGE,
    VoiceUnavailableError,
    synthesize,
    synthesize_voice_note,
    transcribe,
)


class TranscribeTests(unittest.IsolatedAsyncioTestCase):
    async def test_transcribe_calls_whisper_api(self) -> None:
        mock_client = MagicMock()
        mock_client.audio.transcriptions.create = AsyncMock(
            return_value=MagicMock(text="hola mundo")
        )
        with patch("claw_v2.voice._build_client", return_value=mock_client):
            with tempfile.NamedTemporaryFile(suffix=".ogg") as f:
                result = await transcribe(Path(f.name), api_key="test-key")
        self.assertEqual(result, "hola mundo")
        mock_client.audio.transcriptions.create.assert_awaited_once()

    async def test_transcribe_raises_without_api_key(self) -> None:
        with self.assertRaises(VoiceUnavailableError):
            await transcribe(Path("/tmp/test.ogg"))


class SynthesizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthesize_creates_mp3_file(self) -> None:
        mock_response = MagicMock()
        mock_response.content = b"fake-audio-data"
        mock_client = MagicMock()
        mock_client.audio.speech.create = AsyncMock(return_value=mock_response)
        with patch("claw_v2.voice._build_client", return_value=mock_client):
            result = await synthesize("hola", api_key="test-key")
        self.assertTrue(result.exists())
        self.assertEqual(result.suffix, ".mp3")
        self.assertEqual(result.read_bytes(), b"fake-audio-data")
        result.unlink(missing_ok=True)

    async def test_synthesize_raises_without_api_key(self) -> None:
        with self.assertRaises(VoiceUnavailableError):
            await synthesize("hola")

    async def test_synthesize_voice_note_uses_xai_without_openai_key(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3:
            mp3_path = Path(mp3.name)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg:
            ogg_path = Path(ogg.name)

        try:
            with (
                patch("claw_v2.voice._synthesize_xai", new=AsyncMock(return_value=mp3_path)) as xai,
                patch("claw_v2.voice.synthesize", new=AsyncMock()) as openai_tts,
                patch("claw_v2.voice._synthesize_edge", new=AsyncMock()) as edge_tts,
                patch("claw_v2.voice._mp3_to_ogg", new=AsyncMock(return_value=ogg_path)),
            ):
                result = await synthesize_voice_note("hola", xai_api_key="xai-key")
        finally:
            mp3_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)

        self.assertEqual(result, ogg_path)
        xai.assert_awaited_once()
        self.assertEqual(xai.await_args.kwargs["api_key"], "xai-key")
        self.assertEqual(xai.await_args.kwargs["language"], XAI_DEFAULT_LANGUAGE)
        openai_tts.assert_not_awaited()
        edge_tts.assert_not_awaited()

    async def test_synthesize_voice_note_falls_back_to_openai_when_xai_fails(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as mp3:
            mp3_path = Path(mp3.name)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as ogg:
            ogg_path = Path(ogg.name)

        try:
            with (
                patch("claw_v2.voice._synthesize_xai", new=AsyncMock(side_effect=RuntimeError("xai down"))) as xai,
                patch("claw_v2.voice.synthesize", new=AsyncMock(return_value=mp3_path)) as openai_tts,
                patch("claw_v2.voice._synthesize_edge", new=AsyncMock()) as edge_tts,
                patch("claw_v2.voice._mp3_to_ogg", new=AsyncMock(return_value=ogg_path)),
            ):
                result = await synthesize_voice_note("hola", api_key="openai-key", xai_api_key="xai-key")
        finally:
            mp3_path.unlink(missing_ok=True)
            ogg_path.unlink(missing_ok=True)

        self.assertEqual(result, ogg_path)
        xai.assert_awaited_once()
        openai_tts.assert_awaited_once()
        edge_tts.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
