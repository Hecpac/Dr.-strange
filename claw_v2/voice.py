from __future__ import annotations

import tempfile
from pathlib import Path


class VoiceUnavailableError(RuntimeError):
    """Raised when voice services cannot be used (missing API key)."""


def _build_client(api_key: str | None = None):
    """Build AsyncOpenAI client. Raises VoiceUnavailableError if no key."""
    if not api_key:
        raise VoiceUnavailableError("OPENAI_API_KEY is required for voice services.")
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)


async def transcribe(audio_path: Path, *, api_key: str | None = None) -> str:
    """OGG/MP3/WAV → text via OpenAI Whisper API (whisper-1)."""
    client = _build_client(api_key)
    with open(audio_path, "rb") as audio_file:
        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
    return response.text


async def synthesize(
    text: str,
    *,
    api_key: str | None = None,
    voice: str = "alloy",
) -> Path:
    """Text → MP3 temp file via OpenAI TTS-1 API. Caller cleans up."""
    client = _build_client(api_key)
    response = await client.audio.speech.create(
        model="tts-1",
        voice=voice,
        input=text,
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(response.content)
    tmp.close()
    return Path(tmp.name)
