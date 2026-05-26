"""Fetch unclebob/swarm-forge repo README + structure to understand the architecture."""
import urllib.request, json, time

URLS = {
    'repo_page': 'https://github.com/unclebob/swarm-forge',
    'readme_raw_main': 'https://raw.githubusercontent.com/unclebob/swarm-forge/main/README.md',
    'readme_raw_master': 'https://raw.githubusercontent.com/unclebob/swarm-forge/master/README.md',
    'api_repo': 'https://api.github.com/repos/unclebob/swarm-forge',
    'api_tree_main': 'https://api.github.com/repos/unclebob/swarm-forge/git/trees/main?recursive=1',
    'api_tree_master': 'https://api.github.com/repos/unclebob/swarm-forge/git/trees/master?recursive=1',
}

results = {}
for name, url in URLS.items():
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/vnd.github+json' if 'api.github' in url else '*/*'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read().decode('utf-8', errors='replace')
            results[name] = {'status': resp.status, 'length': len(data), 'data': data[:25000]}
    except Exception as e:
        results[name] = {'error': str(e)}

# Pretty output README first (most informative)
print("=" * 80)
print("REPO METADATA (api.github.com)")
print("=" * 80)
if 'api_repo' in results and 'data' in results['api_repo']:
    try:
        meta = json.loads(results['api_repo']['data'])
        print(f"  Name: {meta.get('full_name')}")
        print(f"  Description: {meta.get('description')}")
        print(f"  Default branch: {meta.get('default_branch')}")
        print(f"  Created: {meta.get('created_at')}")
        print(f"  Updated: {meta.get('updated_at')}")
        print(f"  Pushed: {meta.get('pushed_at')}")
        print(f"  Stars: {meta.get('stargazers_count')}")
        print(f"  Forks: {meta.get('forks_count')}")
        print(f"  Open issues: {meta.get('open_issues_count')}")
        print(f"  Language: {meta.get('language')}")
        print(f"  License: {(meta.get('license') or {}).get('spdx_id')}")
        print(f"  Topics: {meta.get('topics')}")
    except Exception as e:
        print(f"  parse err: {e}")

print()
print("=" * 80)
print("TREE STRUCTURE")
print("=" * 80)
for tree_name in ['api_tree_main', 'api_tree_master']:
    if tree_name in results and 'data' in results[tree_name]:
        try:
            tree = json.loads(results[tree_name]['data'])
            files = tree.get('tree', [])
            print(f"\n  {tree_name}: {len(files)} entries")
            for f in files[:50]:
                print(f"    [{f.get('type')}] {f.get('path')} ({f.get('size', '-')} bytes)")
            break
        except Exception as e:
            print(f"  parse err: {e}")

print()
print("=" * 80)
print("README CONTENT")
print("=" * 80)
for readme_key in ['readme_raw_main', 'readme_raw_master']:
    if readme_key in results and 'data' in results[readme_key]:
        print(f"\n--- {readme_key} ({results[readme_key].get('length')} bytes) ---\n")
        print(results[readme_key]['data'])
        break
