"""Re-send the 2 Flow images as sendDocument (uncompressed) so Hector gets the original PNG bytes."""
from __future__ import annotations
import os, sys, json, urllib.request, mimetypes, uuid
from pathlib import Path

ENV_FILE = Path.home() / ".claw" / "env"
CHAT_ID = "574707975"
IMAGES = [
    ("/Users/hector/Projects/Dr.-strange/artifacts/dr_strange_flow_outputs/01_chrome_skull_blue_hud.png",
     "01_chrome_skull_blue_hud.png — original sin compresión de Telegram."),
    ("/Users/hector/Projects/Dr.-strange/artifacts/dr_strange_flow_outputs/02_stone_skull_milky_way_hud.png",
     "02_stone_skull_milky_way_hud.png — original sin compresión de Telegram."),
]


def load_env() -> dict:
    env = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def send_document(token: str, chat_id: str, path: str, caption: str) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    boundary = f"----formboundary{uuid.uuid4().hex}"
    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path)
    mime = mimetypes.guess_type(path)[0] or "image/png"
    body = bytearray()

    def add_field(name: str, value: str):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(value.encode())
        body.extend(b"\r\n")

    def add_file(name: str, fn: str, content: bytes, mime: str):
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'.encode())
        body.extend(f"Content-Type: {mime}\r\n\r\n".encode())
        body.extend(content)
        body.extend(b"\r\n")

    add_field("chat_id", chat_id)
    add_field("caption", caption)
    add_file("document", filename, data, mime)
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    token = load_env().get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("ERR no token", file=sys.stderr); return 1
    results = []
    for path, caption in IMAGES:
        size = os.path.getsize(path)
        r = send_document(token, CHAT_ID, path, caption)
        ok = bool(r.get("ok"))
        mid = r.get("result", {}).get("message_id") if ok else None
        print(f"{'OK' if ok else 'FAIL'} {path} ({size} bytes) → message_id={mid} | err={r.get('description') or r.get('error')}")
        results.append({"path": path, "size": size, "ok": ok, "message_id": mid})
    print("\n=== SUMMARY ===")
    print(json.dumps(results, indent=2))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
