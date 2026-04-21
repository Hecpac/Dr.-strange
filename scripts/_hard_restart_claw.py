#!/usr/bin/env python3
"""Hard kill all Claw processes and restart cleanly."""
import subprocess, signal, os, time, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1. Kill ALL claw_v2.main processes
try:
    out = subprocess.check_output(["pgrep", "-f", "claw_v2.main"], text=True).strip()
    for pid in out.split("\n"):
        pid = pid.strip()
        if pid and pid != str(os.getpid()):
            print(f"Killing claw_v2.main PID {pid}")
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
except subprocess.CalledProcessError:
    print("No claw_v2.main processes found.")

# 2. Free port 8765
try:
    out = subprocess.check_output(["lsof", "-ti:8765"], text=True).strip()
    for pid in out.split("\n"):
        pid = pid.strip()
        if pid:
            print(f"Killing port 8765 holder PID {pid}")
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
except subprocess.CalledProcessError:
    pass

time.sleep(2)

# 3. Clean PID files
for p in ["~/.claw/claw.pid", "~/.claw/telegram.pid"]:
    path = os.path.expanduser(p)
    if os.path.exists(path):
        os.remove(path)
        print(f"Removed {path}")

# 4. Restart via restart.sh
restart_sh = os.path.join(ROOT, "scripts", "restart.sh")
print(f"\nRestarting Claw via {restart_sh}...")
result = subprocess.run(["bash", restart_sh], cwd=ROOT, capture_output=True, text=True, timeout=15)
print(result.stdout)
if result.stderr:
    print(f"STDERR: {result.stderr}")

# 5. Wait and verify
time.sleep(3)
import urllib.request
try:
    r = urllib.request.urlopen("http://localhost:8765", timeout=5)
    print(f"\nWeb transport: HTTP {r.status} — OK")
except Exception as e:
    print(f"\nWeb transport: FAILED ({e})")

# Check claw.log for errors
log_path = os.path.join(ROOT, "logs", "claw.log")
if os.path.exists(log_path):
    with open(log_path) as f:
        content = f.read()
    if content.strip():
        print(f"\n--- claw.log ---\n{content[-1500:]}")
    else:
        print("\nclaw.log is empty (good — no errors)")
