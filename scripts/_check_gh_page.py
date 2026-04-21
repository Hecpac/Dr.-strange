#!/usr/bin/env python3
"""List Chrome tabs to find the GitHub permissions page."""
import subprocess, json, sys

result = subprocess.run(
    [sys.executable, "-m", "claw_v2.browser_cli", "pages"],
    capture_output=True, text=True, timeout=10,
)
print(result.stdout[:3000])
if result.stderr:
    print("STDERR:", result.stderr[:1000])
