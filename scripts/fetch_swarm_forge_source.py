"""Fetch the actual source files of swarm-forge — orchestrator + prompts."""
import urllib.request, json, os

OUT_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/swarm_forge"
os.makedirs(OUT_DIR, exist_ok=True)

BASE = "https://raw.githubusercontent.com/unclebob/swarm-forge/main"

FILES = [
    "SwarmForgeInitSpec.md",
    "swarmforge.sh",
    "swarm-window-watchdog.sh",
    "swarm-cleanup.sh",
    "swarmlog.sh",
    "swarm",
    "swarmforge/swarmforge.conf",
    "swarmforge/constitution.prompt",
    "swarmforge/constitution/project.prompt",
    "swarmforge/constitution/engineering.prompt",
    "swarmforge/constitution/workflow.prompt",
    "swarmforge/architect.prompt",
    "swarmforge/coder.prompt",
    "swarmforge/reviewer.prompt",
    "swarmforge/refactorer.prompt",
    "swarmforge/specifier.prompt",
]

results = {}
for path in FILES:
    url = f"{BASE}/{path}"
    out_path = f"{OUT_DIR}/{path.replace('/', '__')}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read().decode('utf-8', errors='replace')
            os.makedirs(os.path.dirname(out_path), exist_ok=True) if '/' in out_path else None
            with open(out_path, 'w') as f:
                f.write(data)
            results[path] = {'bytes': len(data), 'ok': True, 'local': out_path}
    except Exception as e:
        results[path] = {'err': str(e)}

print(json.dumps(results, indent=2))
print(f"\nFetched {sum(1 for r in results.values() if r.get('ok'))}/{len(FILES)} files into {OUT_DIR}")
