import subprocess, json, time, os

def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'rc': -1, 'stdout': '', 'stderr': str(e)}

ts = int(time.time())
out_path = f"/Users/hector/Projects/Dr.-strange/artifacts/screenshots/desktop_{ts}.png"
os.makedirs(os.path.dirname(out_path), exist_ok=True)

# -x silence shutter, -C include cursor
report = {}
report['capture'] = run(['/usr/sbin/screencapture', '-x', '-C', out_path])
if os.path.exists(out_path):
    report['exists'] = True
    report['size_bytes'] = os.path.getsize(out_path)
    report['path'] = out_path
else:
    report['exists'] = False
print(json.dumps(report, indent=2))
