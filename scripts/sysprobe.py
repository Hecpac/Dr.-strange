import subprocess, json

def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except Exception as e:
        return f"ERR: {e}"

out = {}
out['uptime'] = run(['/usr/bin/uptime']).strip()
out['vm_stat'] = run(['/usr/bin/vm_stat'])
out['df_root'] = run(['/bin/df', '-h', '/'])
out['sysctl_mem_bytes'] = run(['/usr/sbin/sysctl', '-n', 'hw.memsize']).strip()
out['sysctl_cpu'] = run(['/usr/sbin/sysctl', '-n', 'hw.ncpu']).strip()
out['sysctl_model'] = run(['/usr/sbin/sysctl', '-n', 'hw.model']).strip()
out['boottime'] = run(['/usr/sbin/sysctl', '-n', 'kern.boottime']).strip()
out['memory_pressure'] = run(['/usr/bin/memory_pressure'])
out['swap'] = run(['/usr/sbin/sysctl', 'vm.swapusage']).strip()

ps_all = run(['/bin/ps', '-axo', 'pid,pmem,pcpu,rss,comm'])
lines = ps_all.splitlines()
# Sort by rss column
def rss_of(line):
    parts = line.split()
    try: return int(parts[3])
    except: return 0
ranked = sorted(lines[1:], key=rss_of, reverse=True)
out['top25_by_rss'] = ranked[:25]

claude_lines = [l for l in lines if 'claude' in l.lower()]
node_lines = [l for l in lines if 'node' in l.lower()]
electron_lines = [l for l in lines if 'electron' in l.lower()]
chrome_lines = [l for l in lines if 'chrome' in l.lower()]
out['claude_processes'] = claude_lines
out['node_count'] = len(node_lines)
out['electron_count'] = len(electron_lines)
out['chrome_count'] = len(chrome_lines)
out['total_processes'] = len(lines) - 1

print(json.dumps(out, indent=2))
