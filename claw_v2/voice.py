from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# --- P0 hotfix D: Realtime TTS 24h cooldown on beta-shape errors -----------
#
# The OpenAI Realtime API was returning WS 4000 invalid_request_error with
# `beta_api_shape_disabled` on every voice note on 2026-05-24. Each call
# paid a websocket round-trip before falling back to batch. We persist a
# disable record so the cooldown survives daemon restarts.

REALTIME_DISABLE_DURATION_SECONDS = 24 * 60 * 60

_DEFAULT_REALTIME_DISABLE_STATE_PATH = Path.home() / ".claw" / "realtime_tts_state.json"
_realtime_disable_state_path_override: Path | None = None

_REALTIME_BETA_SHAPE_PATTERNS = (
    re.compile(r"beta_api_shape_disabled", re.IGNORECASE),
    re.compile(r"\b4000\b.*invalid_request_error", re.IGNORECASE),
    re.compile(r"invalid_request_error[^\n]*\b4000\b", re.IGNORECASE),
)


def set_realtime_disable_state_path(path: Path | None) -> None:
    """Test seam — point the disable state file at a tmpdir during unit tests."""
    global _realtime_disable_state_path_override
    _realtime_disable_state_path_override = path


def _realtime_disable_state_path() -> Path:
    return (
        _realtime_disable_state_path_override
        if _realtime_disable_state_path_override is not None
        else _DEFAULT_REALTIME_DISABLE_STATE_PATH
    )


def realtime_tts_disabled_until() -> float | None:
    path = _realtime_disable_state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("disabled_until")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def is_realtime_tts_disabled() -> bool:
    until = realtime_tts_disabled_until()
    return until is not None and until > time.time()


def disable_realtime_tts(
    *,
    reason: str,
    duration_seconds: float = REALTIME_DISABLE_DURATION_SECONDS,
) -> float:
    """Persist a disable record. Returns the disabled_until epoch seconds."""
    path = _realtime_disable_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    until = time.time() + duration_seconds
    payload = {
        "disabled_until": until,
        "reason": reason,
        "set_at": time.time(),
    }
    path.write_text(json.dumps(payload, indent=2))
    logger.warning(
        "Realtime TTS disabled until %s (reason=%s)",
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)),
        reason,
    )
    return until


def is_realtime_beta_shape_error(exc: BaseException) -> bool:
    text = str(exc) if exc is not None else ""
    return any(p.search(text) for p in _REALTIME_BETA_SHAPE_PATTERNS)


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
            whisper_bin,
            str(audio_path),
            "--model",
            "base",
            "--language",
            "es",
            "--output_format",
            "txt",
            "--output_dir",
            out_dir,
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
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "libopus",
        str(out),
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

REALTIME_WS_URL = "wss://api.openai.com/v1/realtime"
REALTIME_DEFAULT_MODEL = "gpt-realtime"
REALTIME_DEFAULT_VOICE = "alloy"
REALTIME_INSTRUCTIONS = (
    "Eres Dr. Strange, agente personal masculino de Hector Pachano. "
    "Hablas español neutro, directo, cercano. Voz firme, segura. "
    "Sin disclaimers, sin saludos largos."
)


async def _synthesize_realtime(
    text: str,
    *,
    api_key: str,
    voice: str = REALTIME_DEFAULT_VOICE,
    model: str = REALTIME_DEFAULT_MODEL,
    timeout: float = 30.0,
) -> Path:
    """Text → WAV (PCM16 24kHz) via OpenAI Realtime API WebSocket. Caller cleans up.

    Uses the same WS path validated in the smoke test: session.update with the
    requested voice, then a single user message + response.create. Captures all
    response.audio.delta frames and writes a WAV.
    """
    import base64
    import json
    import wave

    import websockets

    url = f"{REALTIME_WS_URL}?model={model}"
    headers = [("Authorization", f"Bearer {api_key}"), ("OpenAI-Beta", "realtime=v1")]
    chunks: list[bytes] = []
    async with websockets.connect(url, additional_headers=headers, max_size=20_000_000) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "modalities": ["text", "audio"],
                        "voice": voice,
                        "output_audio_format": "pcm16",
                        "instructions": REALTIME_INSTRUCTIONS,
                    },
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        )
        await ws.send(json.dumps({"type": "response.create"}))
        while True:
            evt = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            etype = evt.get("type")
            if etype == "response.audio.delta":
                chunks.append(base64.b64decode(evt["delta"]))
            elif etype == "response.done":
                break
            elif etype == "error":
                raise RuntimeError(
                    f"realtime error: {evt.get('error', {}).get('message', 'unknown')}"
                )
    if not chunks:
        raise RuntimeError("realtime returned no audio")
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"".join(chunks))
    return Path(tmp.name)


async def _wav_to_ogg(wav_path: Path) -> Path:
    """Convert WAV PCM16 to OGG Opus for Telegram voice notes."""
    ogg_path = wav_path.with_suffix(".ogg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(wav_path),
        "-acodec",
        "libopus",
        "-b:a",
        "48k",
        str(ogg_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("ffmpeg WAV→OGG conversion timed out")
    if proc.returncode != 0 or not ogg_path.exists():
        raise RuntimeError("ffmpeg WAV→OGG conversion failed")
    return ogg_path


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
        "ffmpeg",
        "-y",
        "-i",
        str(mp3_path),
        "-acodec",
        "libopus",
        "-b:a",
        "48k",
        str(ogg_path),
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
    prefer_realtime: bool = False,
    realtime_voice: str = REALTIME_DEFAULT_VOICE,
    realtime_model: str = REALTIME_DEFAULT_MODEL,
    observe: Callable[[str, dict], None] | None = None,
) -> Path:
    """Text → OGG Opus voice note for Telegram.

    Backend priority when prefer_realtime=False:
        xAI Grok TTS → OpenAI TTS-1 → Edge-TTS
    Backend priority when prefer_realtime=True:
        OpenAI Realtime (gpt-realtime, voice alloy) → xAI → OpenAI TTS-1 → Edge-TTS

    ``observe`` receives ("realtime_tts_disabled_beta_shape", {...}) when the
    Realtime backend trips a beta-shape error and the 24h cooldown kicks in.
    """
    truncated = text[:MAX_TTS_CHARS]
    mp3_path: Path | None = None
    wav_path: Path | None = None

    if prefer_realtime and api_key:
        if is_realtime_tts_disabled():
            logger.info("Realtime TTS in cooldown; skipping to batch chain")
        else:
            try:
                wav_path = await _synthesize_realtime(
                    truncated,
                    api_key=api_key,
                    voice=realtime_voice,
                    model=realtime_model,
                )
            except Exception as exc:
                if is_realtime_beta_shape_error(exc):
                    until = disable_realtime_tts(reason="beta_api_shape_disabled")
                    if observe is not None:
                        try:
                            observe(
                                "realtime_tts_disabled_beta_shape",
                                {
                                    "reason": "beta_api_shape_disabled",
                                    "disabled_until": until,
                                    "duration_seconds": REALTIME_DISABLE_DURATION_SECONDS,
                                    "error": str(exc)[:300],
                                },
                            )
                        except Exception:
                            logger.debug("observe callback raised in voice", exc_info=True)
                else:
                    logger.warning(
                        "Realtime TTS failed, falling back to batch chain", exc_info=True
                    )

    if wav_path is not None:
        try:
            ogg_path = await _wav_to_ogg(wav_path)
        finally:
            wav_path.unlink(missing_ok=True)
        return ogg_path

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
