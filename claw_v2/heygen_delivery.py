"""HeyGen → Telegram delivery service.

Permanent runtime capability for: poll an in-progress HeyGen render,
download when complete, transcode for Telegram (Bot API 50MB cap),
and deliver via sendVideo to Hector's chat.

Usage from Python:
    from claw_v2.heygen_delivery import HeygenDeliveryService
    svc = HeygenDeliveryService()
    result = svc.auto_deliver("bf41989e378048a4bda1cf89f5cadc92")

Usage from CLI:
    python -m claw_v2.cli.heygen_deliver bf41989e378048a4bda1cf89f5cadc92 \\
        --caption "Sycophancy v2 — 68s, voice clone"

Why this exists: HeyGen returns raw H.264 ~8-9 Mbps. A 60-90s render is
60-90MB, which exceeds Telegram Bot API's 50MB sendVideo limit. URL-mode
delivery also fails because CloudFront blocks Telegram's user-agent.
The deterministic path is download → ffmpeg CRF 28 → multipart upload.
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARTIFACTS_DIR = Path("/Users/hector/Projects/Dr.-strange/artifacts/heygen")
ENV_PATH = Path("/Users/hector/Projects/Dr.-strange/.env")
DEFAULT_FFMPEG = "/opt/homebrew/bin/ffmpeg"
TELEGRAM_BOT_API_VIDEO_LIMIT = 50 * 1024 * 1024  # 50MB hard cap on sendVideo


@dataclass(slots=True)
class DeliveryResult:
    ok: bool
    video_id: str
    status: str
    raw_path: Path | None = None
    compressed_path: Path | None = None
    raw_size: int | None = None
    compressed_size: int | None = None
    duration_seconds: float | None = None
    telegram_message_id: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "video_id": self.video_id,
            "status": self.status,
            "raw_path": str(self.raw_path) if self.raw_path else None,
            "compressed_path": str(self.compressed_path) if self.compressed_path else None,
            "raw_size": self.raw_size,
            "compressed_size": self.compressed_size,
            "duration_seconds": self.duration_seconds,
            "telegram_message_id": self.telegram_message_id,
            "error": self.error,
        }


def _load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        return out
    for raw in ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _heygen_api_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "HEYGEN_API_KEY", "-w"],
        capture_output=True, text=True, timeout=5,
    )
    key = result.stdout.strip()
    if not key:
        raise RuntimeError("HEYGEN_API_KEY not found in Keychain")
    return key


class HeygenDeliveryService:
    """End-to-end render → telegram delivery for HeyGen videos."""

    def __init__(
        self,
        artifacts_dir: Path = ARTIFACTS_DIR,
        ffmpeg_path: str = DEFAULT_FFMPEG,
        poll_interval_seconds: int = 20,
        poll_timeout_seconds: int = 900,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.ffmpeg_path = ffmpeg_path
        self.poll_interval = poll_interval_seconds
        self.poll_timeout = poll_timeout_seconds

    def status(self, video_id: str) -> dict[str, Any]:
        api_key = _heygen_api_key()
        url = f"https://api.heygen.com/v1/video_status.get?video_id={video_id}"
        req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def poll_until_done(self, video_id: str) -> dict[str, Any]:
        deadline = time.time() + self.poll_timeout
        while time.time() < deadline:
            payload = self.status(video_id)
            data = payload.get("data") or {}
            status = data.get("status")
            if status in ("completed", "failed"):
                return data
            time.sleep(self.poll_interval)
        raise TimeoutError(f"HeyGen render did not complete within {self.poll_timeout}s")

    def download(self, video_url: str, dest: Path) -> Path:
        req = urllib.request.Request(video_url)
        with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        return dest

    def compress(self, src: Path, dest: Path, crf: int = 28) -> Path:
        cmd = [
            self.ffmpeg_path, "-y", "-i", str(src),
            "-vcodec", "libx264", "-crf", str(crf), "-preset", "fast",
            "-acodec", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.splitlines()[-5:])
            raise RuntimeError(f"ffmpeg failed: {tail}")
        return dest

    @staticmethod
    def _multipart_body(
        fields: dict[str, str], file_field: str, file_path: Path
    ) -> tuple[bytes, str]:
        boundary = f"----dr-strange-{uuid.uuid4().hex}"
        crlf = b"\r\n"
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += v.encode("utf-8") + crlf
        ctype = mimetypes.guess_type(str(file_path))[0] or "video/mp4"
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        body += file_path.read_bytes() + crlf
        body += f"--{boundary}--\r\n".encode()
        return bytes(body), boundary

    def send_to_telegram(
        self,
        video_path: Path,
        caption: str | None = None,
        chat_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration: int | None = None,
    ) -> dict[str, Any]:
        env = _load_env()
        token = env.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        target_chat = (
            chat_id
            or env.get("TELEGRAM_ALLOWED_USER_ID")
            or os.getenv("TELEGRAM_ALLOWED_USER_ID")
        )
        if not token or not target_chat:
            return {"ok": False, "error": "missing_token_or_chat_id"}

        fields: dict[str, str] = {"chat_id": target_chat, "supports_streaming": "true"}
        if caption:
            fields["caption"] = caption
        if width:
            fields["width"] = str(width)
        if height:
            fields["height"] = str(height)
        if duration:
            fields["duration"] = str(duration)

        body, boundary = self._multipart_body(fields, "video", video_path)
        url = f"https://api.telegram.org/bot{token}/sendVideo"
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

        def _redact(text: str) -> str:
            return text.replace(token, "[REDACTED_TOKEN]") if token else text

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"ok": False, "http_error": e.code,
                    "body": _redact(e.read().decode("utf-8", errors="replace"))}
        except urllib.error.URLError as e:
            return {"ok": False, "error": "telegram_url_error",
                    "detail": _redact(str(getattr(e, "reason", e)))}
        except Exception as e:
            return {"ok": False, "error": "telegram_unexpected_error",
                    "detail": _redact(f"{type(e).__name__}: {e}")}

    def auto_deliver(
        self,
        video_id: str,
        caption: str | None = None,
        chat_id: str | None = None,
        slug: str | None = None,
    ) -> DeliveryResult:
        """Poll → download → compress (if needed) → send to Telegram."""
        slug = slug or video_id[:12]
        try:
            data = self.poll_until_done(video_id)
        except TimeoutError as e:
            return DeliveryResult(ok=False, video_id=video_id, status="timeout", error=str(e))

        status = data.get("status", "unknown")
        if status != "completed":
            return DeliveryResult(
                ok=False, video_id=video_id, status=status,
                error=str(data.get("error") or "render_did_not_complete"),
            )

        video_url = data.get("video_url")
        duration = data.get("duration")
        if not video_url:
            return DeliveryResult(ok=False, video_id=video_id, status=status,
                                  error="no_video_url_in_status")

        raw_path = self.artifacts_dir / f"{slug}_{video_id}.mp4"
        self.download(video_url, raw_path)
        raw_size = raw_path.stat().st_size

        send_path = raw_path
        compressed_path: Path | None = None
        compressed_size: int | None = None
        if raw_size > TELEGRAM_BOT_API_VIDEO_LIMIT:
            compressed_path = self.artifacts_dir / f"{slug}_{video_id}_compressed.mp4"
            self.compress(raw_path, compressed_path)
            compressed_size = compressed_path.stat().st_size
            send_path = compressed_path

        tg_result = self.send_to_telegram(
            send_path,
            caption=caption,
            chat_id=chat_id,
            duration=int(duration) if duration else None,
        )
        ok = bool(tg_result.get("ok"))
        msg_id = (tg_result.get("result") or {}).get("message_id") if ok else None
        return DeliveryResult(
            ok=ok,
            video_id=video_id,
            status=status,
            raw_path=raw_path,
            compressed_path=compressed_path,
            raw_size=raw_size,
            compressed_size=compressed_size,
            duration_seconds=float(duration) if duration else None,
            telegram_message_id=msg_id,
            error=None if ok else json.dumps(tg_result),
        )
