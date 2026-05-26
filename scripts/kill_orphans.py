import subprocess, json, time

def run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return {'rc': r.returncode, 'stdout': r.stdout, 'stderr': r.stderr}
    except Exception as e:
        return {'rc': -1, 'stdout': '', 'stderr': str(e)}

targets = [74930, 14319]
report = {'before': {}, 'kill': {}, 'after': {}}

# Verify before
for pid in targets:
    r = run(['/bin/ps', '-p', str(pid), '-o', 'pid,pmem,pcpu,rss,etime,comm'])
    report['before'][pid] = r['stdout'].strip()

# Send SIGTERM first
for pid in targets:
    r = run(['/bin/kill', '-TERM', str(pid)])
    report['kill'][f'TERM_{pid}'] = r

time.sleep(2)

# Check survivors, escalate to KILL
for pid in targets:
    r = run(['/bin/ps', '-p', str(pid), '-o', 'pid,comm'])
    if str(pid) in r['stdout']:
        k = run(['/bin/kill', '-KILL', str(pid)])
        report['kill'][f'KILL_{pid}'] = k

time.sleep(1)

# Verify after
for pid in targets:
    r = run(['/bin/ps', '-p', str(pid), '-o', 'pid,pmem,pcpu,rss,etime,comm'])
    report['after'][pid] = r['stdout'].strip() if r['rc'] == 0 else f"NOT FOUND (rc={r['rc']})"

# Snapshot memory now
vm = run(['/usr/bin/vm_stat'])
report['vm_stat_after'] = vm['stdout']

print(json.dumps(report, indent=2))
