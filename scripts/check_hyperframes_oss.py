"""Check if Bin Liu pushed HyperFrames OSS repo (he confirmed 'tmrw' from Friday)."""
import urllib.request, json, time

def fetch(url, headers=None):
    h = {'User-Agent': 'Mozilla/5.0'}
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode('utf-8', errors='replace'), r.status
    except Exception as e:
        return None, str(e)

# Search GitHub for hyperframes repos
print("=== GitHub search for hyperframes ===")
data, st = fetch("https://api.github.com/search/repositories?q=hyperframes+heygen&sort=updated&order=desc",
                 headers={'Accept': 'application/vnd.github+json'})
if data:
    res = json.loads(data)
    items = res.get('items', [])
    print(f"Total found: {res.get('total_count', 0)}")
    for r in items[:8]:
        name = r.get('full_name')
        stars = r.get('stargazers_count')
        pushed = r.get('pushed_at')
        desc = r.get('description', '')[:120] if r.get('description') else ''
        print(f"  - {name} ({stars}⭐, pushed {pushed})")
        print(f"    desc: {desc}")

# Try the npm package name from earlier tweet: `npx hyperframes@latest preview`
print("\n=== Check npm: hyperframes ===")
data, st = fetch("https://registry.npmjs.org/hyperframes")
if data and not data.startswith('ERR'):
    try:
        pkg = json.loads(data)
        print(f"  Package exists: {pkg.get('name')}")
        latest = pkg.get('dist-tags', {}).get('latest', 'n/a')
        print(f"  Latest: {latest}")
        versions = list(pkg.get('versions', {}).keys())
        print(f"  Total versions: {len(versions)}")
        if versions:
            print(f"  First version: {versions[0]}")
            print(f"  Last 3 versions: {versions[-3:]}")
        # Repo link
        repo = pkg.get('repository')
        print(f"  Repository: {repo}")
        # Recent activity
        time_obj = pkg.get('time', {})
        if 'modified' in time_obj:
            print(f"  Last modified: {time_obj['modified']}")
    except Exception as e:
        print(f"parse err: {e}")
else:
    print(f"npm err: {st}")

# Also check Bin Liu's GitHub user repos
print("\n=== Bin Liu github repos (sort updated) ===")
data, st = fetch("https://api.github.com/users/liu8in/repos?sort=updated&per_page=20",
                 headers={'Accept': 'application/vnd.github+json'})
if data and not data.startswith('ERR'):
    try:
        repos = json.loads(data)
        if isinstance(repos, list):
            for r in repos[:10]:
                print(f"  - {r.get('name')} ({r.get('stargazers_count')}⭐, pushed {r.get('pushed_at')})")
        else:
            print(f"  Got non-list: {repos}")
    except Exception as e:
        print(f"parse err: {e}")
else:
    print(f"err: {st}")
