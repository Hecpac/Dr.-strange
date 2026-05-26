import subprocess, json, time

def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'rc': -1, 'stdout': '', 'stderr': str(e)}

report = {}
report['open'] = run(['/usr/bin/open', '-a', 'Codex 2'])
time.sleep(3)
ps = run(['/bin/ps', '-axo', 'pid,pmem,rss,comm'])['stdout']
report['codex_processes'] = [l for l in ps.splitlines() if 'Codex 2' in l or '/Codex' in l]
report['count'] = len(report['codex_processes'])
print(json.dumps(report, indent=2))
