#!/usr/bin/env python3
"""Open a URL in Chrome, take a screenshot, compress, send via Telegram."""
import subprocess
import time
import sys
import os
import json
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000/for-builders"
OUT_PNG = "/tmp/phd-screenshot.png"
OUT_JPG = "/tmp/phd-screenshot.jpg"

# Open URL in Chrome
subprocess.run(["osascript", "-e",
    'tell application "Google Chrome"\n'
    '    activate\n'
    '    if (count of windows) = 0 then\n'
    '        make new window\n'
    '    end if\n'
    f'    set URL of active tab of first window to "{URL}"\n'
    'end tell'
])

time.sleep(5)

# Screenshot the full screen
subprocess.run(["screencapture", "-x", OUT_PNG])
time.sleep(1)

# Convert to JPEG and resize to max 1920px wide
subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                 "-Z", "1920", OUT_PNG, "--out", OUT_JPG])

out_file = OUT_JPG if os.path.exists(OUT_JPG) else OUT_PNG
size = os.path.getsize(out_file)
print(f"Image ready: {size} bytes ({out_file})")

# Load env
from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/Projects/Dr.-strange/.env"))
token = os.environ["TELEGRAM_BOT_TOKEN"]
chat_id = os.environ["TELEGRAM_ALLOWED_USER_ID"]

# Use curl for reliable multipart upload
result = subprocess.run([
    "curl", "-s",
    f"https://api.telegram.org/bot{token}/sendPhoto",
    "-F", f"chat_id={chat_id}",
    "-F", "caption=PHD /for-builders hero — CTA buttons preview (pre-deploy)",
    "-F", f"photo=@{out_file}"
], capture_output=True, text=True)

resp = json.loads(result.stdout)
print("Sent!" if resp.get("ok") else f"Error: {resp}")
