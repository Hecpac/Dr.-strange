from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class VoiceUnavailableError(RuntimeError):
    """Raised when voice services cannot be used (missing API key)."""


def _build_client(api_key: str | None = None):
    """Build AsyncOpenAI client. Raises VoiceUnavailableError if no key."""
    if not api_key:
        raise VoiceUnavailableError("OPENAI_API_KEY is required for voice services.")
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)


async def _transcribe_local(audio_path: Path) -> str:
    """Transcribe using local whisper CLI (Homebrew). Fallback when API unavailable."""
    whisper_bin = shutil.which("whisper")
    if not whisper_bin:
        raise RuntimeError("Local whisper CLI not found in PATH")
    out_dir = tempfile.mkdtemp(prefix="claw-whisper-")
    try:
        proc = await asyncio.create_subprocess_exec(
            whisper_bin, str(audio_path),
            "--model", "base",
            "--language", "es",
            "--output_format", "txt",
            "--output_dir", out_dir,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"whisper CLI exited with code {proc.returncode}")
        txt_files = list(Path(out_dir).glob("*.txt"))
        if not txt_files:
            raise RuntimeError("whisper produced no output")
        return txt_files[0].read_text().strip()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


async def transcribe(audio_path: Path, *, api_key: str | None = None) -> str:
    """OGG/MP3/WAV → text. Requires API key; falls back to local whisper only after API failure."""
    if not api_key:
        raise VoiceUnavailableError("OPENAI_API_KEY is required for voice transcription.")

    try:
        client = _build_client(api_key)
        with open(audio_path, "rb") as audio_file:
            response = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
            )
        return response.text
    except VoiceUnavailableError:
        raise
    except Exception:
        logger.warning("OpenAI Whisper API failed, falling back to local", exc_info=True)

    return await _transcribe_local(audio_path)


async def extract_audio(video_path: Path) -> Path:
    """Extract audio from video file to OGG using ffmpeg. Caller cleans up."""
    out = video_path.with_suffix(".ogg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "libopus", str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"ffmpeg timed out processing {video_path}")
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg failed to extract audio from {video_path}")
    return out


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


MAX_TTS_CHARS = 4096

_EDGE_VOICE_MAP: dict[str, str] = {
    "alloy": "es-MX-DaliaNeural",
    "echo": "es-MX-JorgeNeural",
    "fable": "es-ES-ElviraNeural",
    "onyx": "es-MX-JorgeNeural",
    "nova": "es-MX-DaliaNeural",
    "shimmer": "es-ES-ElviraNeural",
}


async def _synthesize_edge(text: str, *, voice: str = "nova") -> Path:
    """Text → MP3 via Edge-TTS (free, no API key). Caller cleans up."""
    import edge_tts

    edge_voice = _EDGE_VOICE_MAP.get(voice, "es-MX-DaliaNeural")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    communicate = edge_tts.Communicate(text, edge_voice)
    await communicate.save(tmp.name)
    return Path(tmp.name)


XAI_TTS_URL = "https://api.x.ai/v1/tts"
XAI_DEFAULT_VOICE = "rex"
XAI_DEFAULT_LANGUAGE = "es-MX"


async def _synthesize_xai(
    text: str,
    *,
    api_key: str,
    voice_id: str = XAI_DEFAULT_VOICE,
    language: str = XAI_DEFAULT_LANGUAGE,
) -> Path:
    """Text → MP3 via xAI Grok TTS. Caller cleans up.

    Endpoint: https://docs.x.ai/developers/model-capabilities/audio/voice
    """
    import httpx

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            XAI_TTS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"text": text, "voice_id": voice_id, "language": language},
        )
    resp.raise_for_status()
    if not resp.content:
        raise RuntimeError("xAI TTS returned empty body")
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.write(resp.content)
    tmp.close()
    return Path(tmp.name)


async def _mp3_to_ogg(mp3_path: Path) -> Path:
    """Convert MP3 to OGG Opus for Telegram voice notes."""
    ogg_path = mp3_path.with_suffix(".ogg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(mp3_path),
        "-acodec", "libopus", "-b:a", "48k", str(ogg_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffmpeg MP3→OGG conversion timed out")
    if proc.returncode != 0 or not ogg_path.exists():
        raise RuntimeError("ffmpeg MP3→OGG conversion failed")
    return ogg_path


async def synthesize_voice_note(
    text: str,
    *,
    api_key: str | None = None,
    voice: str = "nova",
    xai_api_key: str | None = None,
    xai_voice: str = XAI_DEFAULT_VOICE,
    xai_language: str = XAI_DEFAULT_LANGUAGE,
) -> Path:
    """Text → OGG Opus voice note for Telegram.

    Backend priority: xAI Grok TTS (Claw's default voice) → OpenAI TTS → Edge-TTS.
    xAI is preferred when XAI_API_KEY is available because it supports built-in
    and custom voice IDs through the same TTS endpoint.
    """
    truncated = text[:MAX_TTS_CHARS]
    mp3_path: Path | None = None

    if xai_api_key:
        try:
            mp3_path = await _synthesize_xai(
                truncated,
                api_key=xai_api_key,
                voice_id=xai_voice,
                language=xai_language,
            )
        except Exception:
            logger.warning("xAI TTS failed, falling back to OpenAI/Edge", exc_info=True)

    if mp3_path is None and api_key:
        try:
            mp3_path = await synthesize(truncated, api_key=api_key, voice=voice)
        except Exception:
            logger.warning("OpenAI TTS failed, falling back to Edge-TTS", exc_info=True)

    if mp3_path is None:
        mp3_path = await _synthesize_edge(truncated, voice=voice)
    try:
        ogg_path = await _mp3_to_ogg(mp3_path)
    finally:
        mp3_path.unlink(missing_ok=True)
    return ogg_path
