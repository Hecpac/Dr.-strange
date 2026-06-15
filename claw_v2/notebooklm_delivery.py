"""NotebookLM → Telegram file delivery.

Permanent runtime capability for pushing a finished NotebookLM artifact
(audio overview .m4a, blog/report export) to Hector's chat. This is the
"entrega real del archivo" half that the orchestration completion handler
was missing: the durable monitor used to only notify with text + an
evidence URI; now it can deliver the file itself.

Audio goes via sendAudio, everything else via sendDocument. Audio over the
Telegram Bot API 50MB cap is transcoded to a 96k mp3 first (NotebookLM
audio overviews are ~38MB for 10-15min, so this rarely fires, but a long
overview can exceed the limit).
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_PATH = Path("/Users/hector/Projects/Dr.-strange/.env")
DEFAULT_FFMPEG = "/opt/homebrew/bin/ffmpeg"
TELEGRAM_BOT_API_FILE_LIMIT = 50 * 1024 * 1024  # 50MB hard cap
_COMPRESS_THRESHOLD = 49 * 1024 * 1024
_AUDIO_EXTS = {".m4a", ".mp3", ".ogg", ".oga", ".wav", ".aac", ".flac"}


@dataclass(slots=True)
class FileDeliveryResult:
    ok: bool
    file_path: str
    method: str
    telegram_message_id: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "file_path": self.file_path,
            "method": self.method,
            "telegram_message_id": self.telegram_message_id,
            "error": self.error,
        }


_REPORT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: Georgia, 'Times New Roman', serif; line-height: 1.7;
          max-width: 740px; margin: 0 auto; padding: 32px 20px; color: #1a1a1a; background: #fbfbf9; }}
  h1 {{ font-size: 1.9rem; line-height: 1.25; margin: 0 0 .15em; color: #111; }}
  h2 {{ font-size: 1.3rem; margin: 1.7em 0 .4em; color: #243b53; }}
  p {{ margin: 0 0 1.1em; font-size: 1.06rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.4em 0; font-size: .95rem;
           font-family: -apple-system, system-ui, sans-serif; }}
  th, td {{ border: 1px solid #d6d9de; padding: 8px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f0f3f7; }}
  .meta {{ color: #6b7280; font-size: .9rem; border-bottom: 1px solid #e5e7eb; padding-bottom: 12px;
           margin-bottom: 24px; font-family: -apple-system, sans-serif; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e5e7eb; background: #16181d; }}
    h1 {{ color: #f3f4f6; }} h2 {{ color: #93c5fd; }} .meta {{ color: #9ca3af; border-color: #2a2d34; }}
    th, td {{ border-color: #2a2d34; }} th {{ background: #21242b; }}
  }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">{meta}</div>
{body}
</body>
</html>
"""


def _is_report_heading(text: str) -> bool:
    return (
        len(text) < 95
        and len(text.split()) <= 14
        and not text.endswith(".")
        and not text[:1].isdigit()
    )


def render_report_html(items: list[dict], *, meta: str | None = None) -> tuple[str, str]:
    """Render structured report blocks into (title, self-contained HTML).

    items: list of {"kind": "text", "text": str} | {"kind": "table", "rows": [[..]]}.
    The first text block becomes the <h1> title; short heading-like text blocks
    become <h2>; tables render with the first row as the header.
    """
    import html as _html

    title = "Informe NotebookLM"
    parts: list[str] = []
    first_text_taken = False
    for it in items or []:
        kind = it.get("kind")
        if kind == "text":
            text = str(it.get("text") or "").strip()
            if not text:
                continue
            if not first_text_taken:
                title = text
                first_text_taken = True
                continue
            if _is_report_heading(text):
                parts.append(f"<h2>{_html.escape(text)}</h2>")
            else:
                parts.append(f"<p>{_html.escape(text)}</p>")
        elif kind == "table":
            rows = it.get("rows") or []
            tr_html = []
            for idx, row in enumerate(rows):
                cell_tag = "th" if idx == 0 else "td"
                cells = "".join(f"<{cell_tag}>{_html.escape(str(c))}</{cell_tag}>" for c in row)
                tr_html.append(f"<tr>{cells}</tr>")
            if tr_html:
                parts.append("<table>" + "".join(tr_html) + "</table>")
    meta_line = _html.escape(meta) if meta else "Informe NotebookLM"
    doc = _REPORT_HTML_TEMPLATE.format(
        title=_html.escape(title), meta=meta_line, body="\n".join(parts)
    )
    return title, doc


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


class NotebookLMDeliveryService:
    """Send a local NotebookLM artifact file to Telegram."""

    def __init__(self, ffmpeg_path: str = DEFAULT_FFMPEG) -> None:
        self.ffmpeg_path = ffmpeg_path

    @staticmethod
    def _multipart_body(
        fields: dict[str, str], file_field: str, file_path: Path
    ) -> tuple[bytes, str]:
        boundary = f"----dr-strange-nlm-{uuid.uuid4().hex}"
        crlf = b"\r\n"
        body = bytearray()
        for k, v in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            body += v.encode("utf-8") + crlf
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        body += f"--{boundary}\r\n".encode()
        body += (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        body += file_path.read_bytes() + crlf
        body += f"--{boundary}--\r\n".encode()
        return bytes(body), boundary

    def _compress_audio(self, src: Path) -> Path:
        dest = src.with_name(f"{src.stem}_tg.mp3")
        cmd = [self.ffmpeg_path, "-y", "-i", str(src), "-b:a", "96k", str(dest)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            tail = "\n".join(proc.stderr.splitlines()[-5:])
            raise RuntimeError(f"ffmpeg failed: {tail}")
        return dest

    def send_to_telegram(
        self,
        file_path: Path,
        *,
        chat_id: str | None = None,
        caption: str | None = None,
        title: str | None = None,
    ) -> FileDeliveryResult:
        file_path = Path(file_path)
        is_audio = file_path.suffix.lower() in _AUDIO_EXTS
        method = "sendAudio" if is_audio else "sendDocument"
        file_field = "audio" if is_audio else "document"

        if not file_path.exists():
            return FileDeliveryResult(
                ok=False,
                file_path=str(file_path),
                method=method,
                error="file_not_found",
            )

        env = _load_env()
        token = env.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
        target_chat = (
            chat_id or env.get("TELEGRAM_ALLOWED_USER_ID") or os.getenv("TELEGRAM_ALLOWED_USER_ID")
        )
        if not token or not target_chat:
            return FileDeliveryResult(
                ok=False,
                file_path=str(file_path),
                method=method,
                error="missing_token_or_chat_id",
            )

        send_path = file_path
        if is_audio and file_path.stat().st_size > _COMPRESS_THRESHOLD:
            send_path = self._compress_audio(file_path)

        fields: dict[str, str] = {"chat_id": str(target_chat)}
        if caption:
            fields["caption"] = caption
        if title and is_audio:
            fields["title"] = title

        body, boundary = self._multipart_body(fields, file_field, send_path)
        url = f"https://api.telegram.org/bot{token}/{method}"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

        def _redact(text: str) -> str:
            return text.replace(token, "[REDACTED_TOKEN]") if token else text

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return FileDeliveryResult(
                ok=False,
                file_path=str(send_path),
                method=method,
                error=_redact(f"http_{e.code}: {e.read().decode('utf-8', 'replace')}"),
            )
        except Exception as e:
            return FileDeliveryResult(
                ok=False,
                file_path=str(send_path),
                method=method,
                error=_redact(f"{type(e).__name__}: {e}"),
            )

        ok = bool(payload.get("ok"))
        msg_id = (payload.get("result") or {}).get("message_id") if ok else None
        return FileDeliveryResult(
            ok=ok,
            file_path=str(send_path),
            method=method,
            telegram_message_id=msg_id,
            error=None if ok else _redact(json.dumps(payload)),
        )
