#!/usr/bin/env python3
"""Verify Telegram bot token and send a test message."""
import os, json, urllib.request, urllib.error

# Load env
env_path = os.path.expanduser("~/.claw/env")
token = None
user_id = None
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line:
                k, v = line.split("=", 1)
                v = v.strip("'\"")
                if k == "TELEGRAM_BOT_TOKEN":
                    token = v
                elif k == "TELEGRAM_ALLOWED_USER_ID":
                    user_id = v

if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in ~/.claw/env")
    exit(1)

print(f"Token: {token[:10]}...{token[-5:]}")
print(f"User ID: {user_id}")

# Test getMe
try:
    url = f"https://api.telegram.org/bot{token}/getMe"
    r = urllib.request.urlopen(url, timeout=10)
    data = json.loads(r.read())
    print(f"Bot: @{data['result'].get('username', '?')} — OK")
except urllib.error.HTTPError as e:
    print(f"getMe FAILED: HTTP {e.code} — {e.read().decode()[:200]}")
    exit(1)

# Test getUpdates to see if polling is active
try:
    url = f"https://api.telegram.org/bot{token}/getWebhookInfo"
    r = urllib.request.urlopen(url, timeout=10)
    data = json.loads(r.read())
    info = data.get("result", {})
    webhook_url = info.get("url", "")
    pending = info.get("pending_update_count", 0)
    print(f"Webhook URL: '{webhook_url}' (empty = polling mode)")
    print(f"Pending updates: {pending}")
    if info.get("last_error_message"):
        print(f"Last error: {info['last_error_message']}")
except Exception as e:
    print(f"getWebhookInfo error: {e}")

# Send test message
if user_id:
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": int(user_id), "text": "Test desde diagnóstico — Claw está activo."}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        r = urllib.request.urlopen(req, timeout=10)
        print("Test message sent — check Telegram!")
    except Exception as e:
        print(f"sendMessage FAILED: {e}")
