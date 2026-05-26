import subprocess, json, time, os

def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'rc': -1, 'stdout': '', 'stderr': str(e)}

report = {}

# Activate Codex 2 (bring to front)
report['activate'] = run(['/usr/bin/osascript', '-e', 'tell application "Codex" to activate'])
time.sleep(1.2)

# Try finding Codex window ids via accessibility
script = '''
tell application "System Events"
    set ws to {}
    try
        set procName to "Codex"
        set codexProc to first process whose name is procName
        repeat with w in (every window of codexProc)
            set end of ws to (name of w) & " | " & (position of w as string) & " | " & (size of w as string)
        end repeat
    end try
    return ws
end tell
'''
report['windows'] = run(['/usr/bin/osascript', '-e', script])

ts = int(time.time())
out = f"/Users/hector/Projects/Dr.-strange/artifacts/screenshots/codex_{ts}.png"
os.makedirs(os.path.dirname(out), exist_ok=True)

# Full screen capture with Codex in front
report['capture'] = run(['/usr/sbin/screencapture', '-x', '-C', out])
if os.path.exists(out):
    report['exists'] = True
    report['size_bytes'] = os.path.getsize(out)
    report['path'] = out
else:
    report['exists'] = False

print(json.dumps(report, indent=2))
