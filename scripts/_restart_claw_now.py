#!/usr/bin/env python3
"""Kill zombie on port 8765 and restart Claw."""
import subprocess, signal, os, sys, time

# Find and kill whatever is on port 8765
try:
    out = subprocess.check_output(["lsof", "-ti:8765"], text=True).strip()
    if out:
        for pid in out.split("\n"):
            pid = pid.strip()
            if pid:
                print(f"Killing PID {pid} on port 8765")
                os.kill(int(pid), signal.SIGKILL)
        time.sleep(1)
        print("Port 8765 freed.")
    else:
        print("Port 8765 is free.")
except subprocess.CalledProcessError:
    print("Port 8765 is free.")

# Restart Claw
restart_script = os.path.join(os.path.dirname(__file__), "restart.sh")
if os.path.exists(restart_script):
    print(f"Running {restart_script}...")
    os.execvp("bash", ["bash", restart_script])
else:
    # Direct launch
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("Starting Claw directly...")
    subprocess.Popen(
        [sys.executable, "-m", "claw_v2.main"],
        cwd=root,
        start_new_session=True,
    )
    print("Claw launched.")
