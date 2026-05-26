"""V3 — correct CLI syntax: list apps, list windows, see --app."""
import subprocess, json, os

PEEKABOO = "/opt/homebrew/bin/peekaboo"

def run(args, timeout=30):
    try:
        r = subprocess.run([PEEKABOO] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return -1, '', str(e)

# 1. list apps
print("=== peekaboo list apps --json ===")
rc, out, err = run(['list', 'apps', '--json'])
print(f"rc={rc}")
if rc == 0:
    try:
        data = json.loads(out)
        apps = data.get('data', {}).get('applications', [])
        chrome_apps = [a for a in apps if 'chrome' in str(a.get('app_name', a.get('name',''))).lower()]
        print(f"Chrome processes:\n{json.dumps(chrome_apps, indent=2)}")
    except Exception as e:
        print(f"parse err: {e}\n{out[:500]}")
else:
    print(err[:500])

# 2. list windows for Chrome
print("\n=== peekaboo list windows --app 'Google Chrome' --json ===")
rc, out, err = run(['list', 'windows', '--app', 'Google Chrome', '--json'])
print(f"rc={rc}")
if rc == 0:
    try:
        data = json.loads(out)
        windows = data.get('data', {}).get('windows', [])
        print(f"Window count: {len(windows)}")
        # Show first 5 windows with title
        for w in windows[:8]:
            print(f"  - {w.get('window_title','?')[:80]}  (id={w.get('window_id','?')}, bundle={w.get('bundle_id','?')})")
    except Exception as e:
        print(f"parse err: {e}\n{out[:500]}")
else:
    print(err[:500])

# 3. see Chrome — capture + element map
print("\n=== peekaboo see --app 'Google Chrome' --json --annotate ===")
out_path = "/tmp/peekaboo_chrome_see_v3.png"
rc, out, err = run(['see', '--app', 'Google Chrome', '--path', out_path, '--annotate', '--json'], timeout=45)
print(f"rc={rc}, output_path={out_path}, exists={os.path.exists(out_path)}, size={os.path.getsize(out_path) if os.path.exists(out_path) else 0}")
if rc == 0 and out:
    try:
        data = json.loads(out)
        # Find snapshot_id and elements
        sub = data.get('data', data)
        snapshot_id = sub.get('snapshot_id') or sub.get('snapshotId')
        elements = sub.get('elements') or sub.get('ui_elements') or []
        print(f"snapshot_id: {snapshot_id}")
        print(f"elements count: {len(elements) if isinstance(elements, list) else 'n/a'}")
        if isinstance(elements, list) and elements:
            print("\nFirst 25 elements:")
            for i, e in enumerate(elements[:25]):
                eid = e.get('id') or e.get('peekaboo_id') or e.get('element_id') or '?'
                role = e.get('role', '?')
                label = (e.get('label') or e.get('title') or e.get('value') or e.get('description') or '')[:80]
                print(f"  [{i}] {eid:<6} role={role:<22} label={label}")
        else:
            # Dump structure for debugging
            print("Structure keys:", list(sub.keys()) if isinstance(sub, dict) else type(sub))
            print(json.dumps(sub, indent=2)[:2000])
    except Exception as e:
        print(f"parse err: {e}\n{out[:2000]}")
else:
    print(f"stderr: {err[:500]}")
    print(f"stdout: {out[:500]}")
