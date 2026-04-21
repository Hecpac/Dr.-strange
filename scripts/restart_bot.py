#!/usr/bin/env python3
"""Kill port 8765 conflict and restart the Claw bot via launchd."""
import subprocess, os, signal, time

# Kill whatever holds port 8765
try:
    out = subprocess.check_output(["lsof", "-ti", ":8765"], text=True).strip()
    for pid in out.splitlines():
        pid = int(pid.strip())
        print(f"Killing PID {pid} on port 8765")
        os.kill(pid, signal.SIGTERM)
    time.sleep(2)
    print("Port 8765 freed")
except subprocess.CalledProcessError:
    print("Port 8765 already free")

# Kill any lingering claw_v2 processes
try:
    out = subprocess.check_output(["pgrep", "-f", "claw_v2.main"], text=True).strip()
    for pid in out.splitlines():
        pid = int(pid.strip())
        if pid != os.getpid():
            print(f"Killing old bot PID {pid}")
            os.kill(pid, signal.SIGTERM)
    time.sleep(1)
except subprocess.CalledProcessError:
    print("No old bot processes")

# Restart via launchd
result = subprocess.run(
    ["launchctl", "kickstart", "-k", "gui/501/com.pachano.claw"],
    capture_output=True, text=True,
)
msg = result.stdout.strip() or result.stderr.strip() or "kickstart sent"
print(f"launchctl: {msg}")

# Verify
time.sleep(3)
try:
    out = subprocess.check_output(["pgrep", "-f", "claw_v2.main"], text=True).strip()
    print(f"Bot running — PID {out.splitlines()[0]}")
except subprocess.CalledProcessError:
    print("WARNING: Bot not detected after restart")
    try:
        with open("/Users/hector/Projects/Dr.-strange/logs/bot.log") as f:
            lines = f.read().strip().splitlines()
            print("Last log lines:")
            for line in lines[-10:]:
                print(f"  {line}")
    except Exception:
        pass
