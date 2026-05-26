"""Empirical test: can Peekaboo see/interact with LinkedIn elements inside Chrome?"""
import subprocess, json, time

PEEKABOO = "/opt/homebrew/bin/peekaboo"

def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'err': str(e)}

# Step 1: list apps to verify Chrome detected
print("=== Step 1: list apps (filter Chrome) ===")
out = run([PEEKABOO, 'list', '--item-type', 'running_applications', '--json'])
if out.get('rc') == 0:
    try:
        data = json.loads(out['stdout'])
        # try to detect structure
        apps = data.get('data', {}).get('applications', data.get('applications', []))
        chrome = [a for a in (apps or []) if 'chrome' in (a.get('name', '') if isinstance(a, dict) else str(a)).lower()]
        print(json.dumps(chrome, indent=2) if chrome else "  Chrome not found in list")
    except Exception as e:
        print(f"  Parse err: {e}")
        print(f"  stdout sample: {out['stdout'][:500]}")
else:
    print(f"  rc={out.get('rc')} stderr={out.get('stderr', '')[:300]}")

# Step 2: inspect_ui on Chrome to see AX tree
print("\n=== Step 2: inspect_ui --app 'Google Chrome' (AX tree, may include LinkedIn content) ===")
out = run([PEEKABOO, 'inspect_ui', '--app', 'Google Chrome', '--max-elements', '50', '--json'], timeout=30)
print(f"rc={out.get('rc')}")
if out.get('rc') == 0:
    try:
        data = json.loads(out['stdout'])
        # Show structure summary
        print(f"stdout length: {len(out['stdout'])}")
        # Sample first 2000 chars of parsed JSON
        print(json.dumps(data, indent=2)[:3500])
    except Exception:
        print(out['stdout'][:3000])
else:
    print(f"stderr: {out.get('stderr', '')[:500]}")

# Step 3: see (screenshot + element map) on Chrome
print("\n=== Step 3: see --app 'Google Chrome' --annotate ===")
out_path = "/tmp/peekaboo_chrome_see.png"
out = run([PEEKABOO, 'see', '--app', 'Google Chrome', '--path', out_path, '--annotate', '--max-elements', '40', '--json'], timeout=30)
print(f"rc={out.get('rc')}")
if out.get('rc') == 0:
    try:
        data = json.loads(out['stdout'])
        # Extract snapshot_id + element count
        snap = data.get('data', {}).get('snapshot_id', data.get('snapshot_id', 'n/a'))
        elements = data.get('data', {}).get('elements', data.get('elements', []))
        print(f"snapshot_id: {snap}")
        print(f"elements: {len(elements) if isinstance(elements, list) else 'n/a'}")
        if isinstance(elements, list):
            # Show first 15 elements (id + label preview)
            for e in elements[:15]:
                print(f"  - {e.get('id', '?')}: role={e.get('role','?')} label={(e.get('label') or e.get('title') or '')[:60]}")
    except Exception as e:
        print(f"parse err: {e}")
        print(out['stdout'][:2000])
else:
    print(f"stderr: {out.get('stderr', '')[:500]}")

import os
if os.path.exists(out_path):
    print(f"\nScreenshot saved: {out_path} ({os.path.getsize(out_path)} bytes)")
