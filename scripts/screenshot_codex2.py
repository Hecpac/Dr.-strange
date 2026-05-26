import subprocess, json, time, os

def run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'rc': -1, 'stdout': '', 'stderr': str(e)}

report = {}

# Activate Codex 2 and try opening a new window via Cmd+N
report['activate'] = run(['/usr/bin/osascript', '-e', 'tell application "Codex" to activate'])
time.sleep(1.0)

# Try unminimizing any minimized windows from Dock, and Cmd+N for a new one
unminimize = '''
tell application "System Events"
    tell process "Codex"
        set frontmost to true
        try
            set wins to every window
            repeat with w in wins
                try
                    if value of attribute "AXMinimized" of w is true then
                        set value of attribute "AXMinimized" of w to false
                    end if
                end try
            end repeat
        end try
        try
            keystroke "n" using command down
        end try
    end tell
end tell
'''
report['unminimize_or_new'] = run(['/usr/bin/osascript', '-e', unminimize])
time.sleep(1.5)

# Re-check windows
script = '''
tell application "System Events"
    tell process "Codex"
        set ws to {}
        try
            repeat with w in (every window)
                set end of ws to (name of w) & " | pos=" & (position of w as string) & " | size=" & (size of w as string)
            end repeat
        end try
        return ws
    end tell
end tell
'''
report['windows_after'] = run(['/usr/bin/osascript', '-e', script])

# Re-activate (Cmd+N may have shifted focus)
run(['/usr/bin/osascript', '-e', 'tell application "Codex" to activate'])
time.sleep(0.7)

ts = int(time.time())
out = f"/Users/hector/Projects/Dr.-strange/artifacts/screenshots/codex_{ts}.png"
os.makedirs(os.path.dirname(out), exist_ok=True)
report['capture'] = run(['/usr/sbin/screencapture', '-x', '-C', out])
if os.path.exists(out):
    report['exists'] = True
    report['size_bytes'] = os.path.getsize(out)
    report['path'] = out

# Also check current Space / which app is frontmost
frontmost = run(['/usr/bin/osascript', '-e', 'tell application "System Events" to name of first process whose frontmost is true'])
report['frontmost_app'] = frontmost

print(json.dumps(report, indent=2))
