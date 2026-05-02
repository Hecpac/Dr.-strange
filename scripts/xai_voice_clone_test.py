"""Test xAI text-to-speech (and optional voice cloning) end-to-end.

Usage:
    XAI_API_KEY=... python scripts/xai_voice_clone_test.py [--voice rex] [--text "..."]
    XAI_API_KEY=... python scripts/xai_voice_clone_test.py --reference path/to/sample.wav

Endpoint references:
    https://docs.x.ai/developers/rest-api-reference/inference/voice
    https://docs.x.ai/developers/model-capabilities/audio/custom-voices
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import pathlib
import sys
import time

import requests

XAI_BASE = "https://api.x.ai/v1"
DEFAULT_TEXT_ES = (
    "Hola, soy Claw. Esta es una prueba de la voz xAI integrada en mi pipeline. "
    "Si me escuchas con buena fidelidad, podemos usar esta voz por defecto."
)
BUILTIN_VOICES = ["eve", "ara", "rex", "sal", "leo"]


def require_api_key() -> str:
    key = os.environ.get("XAI_API_KEY")
    if not key:
        sys.exit("ERROR: set XAI_API_KEY in environment")
    return key


def create_custom_voice(api_key: str, reference_path: pathlib.Path, name: str, language: str) -> str:
    """Optional: upload a reference audio to clone a voice. Returns voice_id."""
    if not reference_path.exists():
        sys.exit(f"ERROR: reference audio not found at {reference_path}")
    content_type = mimetypes.guess_type(reference_path.name)[0] or "application/octet-stream"
    with reference_path.open("rb") as fh:
        resp = requests.post(
            f"{XAI_BASE}/custom-voices",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (reference_path.name, fh, content_type)},
            data={"name": name, "language": language, "use_case": "conversational"},
            timeout=120,
        )
    resp.raise_for_status()
    payload = resp.json()
    voice_id = payload.get("id") or payload.get("voice_id")
    if not voice_id:
        sys.exit(f"ERROR: API did not return a voice_id. Payload: {payload}")
    return voice_id


def synthesize_tts(api_key: str, voice_id: str, text: str, language: str, out_path: pathlib.Path) -> None:
    resp = requests.post(
        f"{XAI_BASE}/tts",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"text": text, "voice_id": voice_id, "language": language},
        timeout=120,
    )
    resp.raise_for_status()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(resp.content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reference",
        type=pathlib.Path,
        help="Optional reference audio file. If provided, clones a new voice first.",
    )
    parser.add_argument(
        "--voice",
        default="rex",
        help=f"Built-in voice id or custom voice id. Built-ins: {', '.join(BUILTIN_VOICES)}",
    )
    parser.add_argument(
        "--language",
        default="es-MX",
        help="BCP-47 language code for TTS/custom voice metadata (e.g., en, es-MX, auto).",
    )
    parser.add_argument(
        "--name",
        default=f"hector-{int(time.time())}",
        help="Display name for cloned voice (only used with --reference)",
    )
    parser.add_argument(
        "--text",
        default=DEFAULT_TEXT_ES,
        help="Text to synthesize",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default=pathlib.Path("artifacts/xai_voice_test.mp3"),
        help="Output MP3 path",
    )
    args = parser.parse_args()

    api_key = require_api_key()
    voice_id = args.voice

    if args.reference:
        print(f"[1/2] Cloning voice from {args.reference} as '{args.name}'...")
        voice_id = create_custom_voice(api_key, args.reference, args.name, args.language)
        print(f"      voice_id = {voice_id}")

    print(f"[synth] voice_id={voice_id} language={args.language} → {args.out}")
    synthesize_tts(api_key, voice_id, args.text, args.language, args.out)

    size_kb = args.out.stat().st_size / 1024
    print(f"OK  voice_id={voice_id}  output={args.out} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
