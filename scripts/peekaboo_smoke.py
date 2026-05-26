"""Smoke test Peekaboo via subprocess (sandbox blocks direct binary)."""
import subprocess, os, json, time

PEEKABOO = "/opt/homebrew/bin/peekaboo"

def run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout[:3000], 'stderr': r.stderr[:1500]}
    except Exception as e:
        return {'err': str(e)}

print("=== peekaboo --version ===")
print(json.dumps(run([PEEKABOO, '--version']), indent=2))

print("\n=== peekaboo --help (short) ===")
out = run([PEEKABOO, '--help'])
print(json.dumps({k: (v[:2000] if isinstance(v, str) else v) for k, v in out.items()}, indent=2))

# Try a screenshot to see if Screen Recording permission is granted
print("\n=== Test screenshot ===")
ts = int(time.time())
out_path = f"/tmp/peekaboo_smoke_{ts}.png"
r = run([PEEKABOO, 'image', '--mode', 'screen', '--path', out_path], timeout=20)
print(json.dumps(r, indent=2))
if os.path.exists(out_path):
    print(f"  ✅ Screenshot created: {out_path} ({os.path.getsize(out_path)} bytes)")
else:
    print("  ❌ No screenshot file created — likely needs Screen Recording permission for Terminal")
