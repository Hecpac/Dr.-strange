"""Capture full output of `peekaboo learn` - docs for AI agents."""
import subprocess, os, time

PEEKABOO = "/opt/homebrew/bin/peekaboo"
OUT = f"/Users/hector/Projects/Dr.-strange/artifacts/steipete_stack/peekaboo_learn_{int(time.time())}.md"

r = subprocess.run([PEEKABOO, 'learn'], capture_output=True, text=True, timeout=30)
print(f"rc={r.returncode}, stdout_len={len(r.stdout)}, stderr_len={len(r.stderr)}")

# Save full output
with open(OUT, 'w') as f:
    if r.stdout: f.write(r.stdout)
    if r.stderr: f.write("\n\n=== STDERR ===\n" + r.stderr)
print(f"Saved: {OUT}")

# Print to console
print("\n=== OUTPUT ===\n")
print(r.stdout)
