from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from claw_v2.voice import VoiceUnavailableError, transcribe, synthesize


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


if __name__ == "__main__":
    unittest.main()
