#!/usr/bin/env python3
"""Quick health check for Claw."""
import urllib.request, json, sys

try:
    r = urllib.request.urlopen("http://localhost:8765", timeout=3)
    print(f"Web transport: HTTP {r.status}")
except Exception as e:
    print(f"Web transport: DOWN ({e})")

# Check if Telegram bot is polling
import subprocess
out = subprocess.check_output(["ps", "aux"], text=True)
tg_running = any("telegram" in line.lower() or "bot" in line.lower() for line in out.splitlines() if "claw" in line.lower())
print(f"Telegram bot in process tree: {tg_running}")
