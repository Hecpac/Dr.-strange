"""Kill process on port 8765 and restart Claw."""
import subprocess, os, signal, time

def kill_port(port: int) -> None:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5
        )
        pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
        for pid in pids:
            print(f"Killing PID {pid} on port {port}")
            os.kill(pid, signal.SIGKILL)
        if pids:
            time.sleep(2)
    except Exception as e:
        print(f"kill_port error: {e}")

def kill_claw() -> None:
    try:
        result = subprocess.run(
            ["pkill", "-9", "-f", "claw_v2.main"],
            capture_output=True, text=True, timeout=5
        )
        print(f"pkill claw_v2.main: rc={result.returncode}")
        time.sleep(2)
    except Exception as e:
        print(f"kill_claw error: {e}")

def start_claw() -> None:
    claw_dir = "/Users/hector/Projects/Dr.-strange"
    venv_python = os.path.join(claw_dir, ".venv", "bin", "python")
    log_path = os.path.join(claw_dir, "logs", "claw.log")
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [venv_python, "-m", "claw_v2.main"],
            cwd=claw_dir,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path = os.path.expanduser("~/.claw/claw.pid")
    os.makedirs(os.path.dirname(pid_path), exist_ok=True)
    with open(pid_path, "w") as f:
        f.write(str(proc.pid))
    print(f"Claw started (pid {proc.pid})")

if __name__ == "__main__":
    kill_port(8765)
    kill_claw()
    start_claw()
