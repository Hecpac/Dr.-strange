#!/usr/bin/env python3
"""Send landing prototype HTML + hero image to Telegram."""
import os, sys, json, urllib.request
from pathlib import Path

dotenv = Path(__file__).resolve().parent.parent / ".env"
env = {}
for line in dotenv.read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

token = env.get("TELEGRAM_BOT_TOKEN", "")
chat_id = env.get("TELEGRAM_ALLOWED_USER_ID", "")
if not token or not chat_id:
    print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_ID in .env")
    sys.exit(1)

def send_document(filepath, caption=""):
    boundary = "----PythonFormBoundary7MA4YWxkTrZu0gW"
    data = Path(filepath).read_bytes()
    filename = Path(filepath).name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())
    return result.get("ok", False)

def send_photo(filepath, caption=""):
    boundary = "----PythonFormBoundary7MA4YWxkTrZu0gW"
    data = Path(filepath).read_bytes()
    filename = Path(filepath).name
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    resp = urllib.request.urlopen(req, timeout=60)
    result = json.loads(resp.read())
    return result.get("ok", False)

base = Path(__file__).resolve().parent.parent / "captures"

# Send hero image first
hero = base / "hero-desert-house.png"
if hero.exists():
    ok = send_photo(str(hero), "🏠 Hero image — Midjourney Upscale (desert house)")
    print(f"Hero image: {'✅ sent' if ok else '❌ failed'}")

# Send HTML prototype
html = base / "pachano-design" / "landing-prototype.html"
if html.exists():
    ok = send_document(str(html), "🌐 Pachano Design — Landing Page Prototype")
    print(f"HTML prototype: {'✅ sent' if ok else '❌ failed'}")
