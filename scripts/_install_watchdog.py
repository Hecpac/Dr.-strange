#!/usr/bin/env python3
"""Bootstrap and enable the claw-watchdog LaunchAgent, then restart Claw."""
import subprocess, os

uid = str(os.getuid())
domain = f"gui/{uid}"
svc_watchdog = f"{domain}/com.pachano.claw-watchdog"
svc_claw = f"{domain}/com.pachano.claw"
plist = os.path.expanduser("~/Library/LaunchAgents/com.pachano.claw-watchdog.plist")

def run(cmd, ignore_error=False):
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip())
    if r.returncode != 0 and not ignore_error:
        print(f"  (exit {r.returncode})")
    return r.returncode

# Step 2: Bootstrap + enable + kickstart watchdog
run(["launchctl", "bootout", svc_watchdog], ignore_error=True)
run(["launchctl", "bootstrap", domain, plist])
run(["launchctl", "enable", svc_watchdog])
run(["launchctl", "kickstart", svc_watchdog])

# Step 3: Restart Claw
run(["launchctl", "kickstart", "-k", svc_claw])

# Verify
print("\n--- Verification ---")
run(["launchctl", "print", svc_watchdog])
