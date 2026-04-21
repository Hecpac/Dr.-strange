#!/usr/bin/env python3
"""Take screenshot of Midjourney tab via CDP."""
import subprocess, sys, json, os

CDP_SCRIPT = os.path.join(os.path.dirname(__file__), "_cdp_mj_status.py")
result = subprocess.run(
    [sys.executable, CDP_SCRIPT],
    capture_output=True, text=True
)
print(result.stdout[:3000] if result.stdout else "no stdout")
if result.stderr:
    print("ERR:", result.stderr[:500])
