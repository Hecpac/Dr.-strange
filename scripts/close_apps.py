import subprocess, json, time

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'rc': -1, 'stdout': '', 'stderr': str(e)}

report = {}

# Quit gracefully via osascript
for app in ['Codex', 'Claude']:
    r = run(['/usr/bin/osascript', '-e', f'tell application "{app}" to quit'])
    report[f'quit_{app}'] = r

time.sleep(3)

# Verify by name
ps_out = run(['/bin/ps', '-axo', 'pid,pmem,rss,comm'])['stdout']
codex_left = [l for l in ps_out.splitlines() if 'Codex 2' in l or '/Codex' in l]
claude_app_left = [l for l in ps_out.splitlines() if 'Claude.app' in l]
report['after_codex_processes'] = codex_left
report['after_claude_app_processes'] = claude_app_left

# Force-kill stragglers (Codex 2.app + Claude.app helpers only — never touch /opt/homebrew/bin/claude CLI or our daemon)
escalated = []
for line in codex_left + claude_app_left:
    parts = line.split()
    if not parts: continue
    try: pid = int(parts[0])
    except: continue
    # safety: skip the homebrew claude CLI and our python daemon
    full = ' '.join(parts)
    if '/opt/homebrew/bin/claude' in full or 'Dr.-strange/.venv' in full:
        continue
    k = run(['/bin/kill', '-TERM', str(pid)])
    escalated.append({'pid': pid, 'comm': full[-80:], 'kill': k})

if escalated:
    time.sleep(2)
report['escalated_term'] = escalated

# Final verify
ps_out2 = run(['/bin/ps', '-axo', 'pid,pmem,rss,comm'])['stdout']
codex_final = [l for l in ps_out2.splitlines() if 'Codex 2' in l or '/Codex' in l]
claude_app_final = [l for l in ps_out2.splitlines() if 'Claude.app' in l]
report['final_codex'] = codex_final
report['final_claude_app'] = claude_app_final

# Memory snapshot
vm = run(['/usr/bin/vm_stat'])['stdout']
report['vm_stat'] = vm

# Also check active sessions of CLI claude still alive
cli_claude = [l for l in ps_out2.splitlines() if '/opt/homebrew/bin/claude' in l or l.strip().endswith(' claude')]
report['cli_claude_still_alive'] = cli_claude

print(json.dumps(report, indent=2))
