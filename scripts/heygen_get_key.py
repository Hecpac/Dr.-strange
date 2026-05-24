import subprocess
import sys

r = subprocess.run(
    ["security", "find-generic-password", "-a", "heygen", "-s", "HEYGEN_API_KEY", "-w"],
    capture_output=True,
    text=True,
)
if r.returncode != 0:
    sys.stderr.write(f"err: {r.stderr}\n")
    sys.exit(r.returncode)
key = r.stdout.strip()
# Print only length + suffix so we never leak the key
print(f"len={len(key)} suffix=...{key[-4:]}")
