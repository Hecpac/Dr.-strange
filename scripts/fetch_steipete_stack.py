"""Fetch Steipete's relevant repos to do real comparison vs Dr. Strange."""
import urllib.request, json, os, time

OUT_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/steipete_stack"
os.makedirs(OUT_DIR, exist_ok=True)

def fetch(url, accept=None):
    headers = {'User-Agent': 'Mozilla/5.0'}
    if accept: headers['Accept'] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode('utf-8', errors='replace'), r.status
    except Exception as e:
        return None, str(e)

# 1. List Steipete's repos
print("=== Listing @steipete repos (recent, public) ===")
data, st = fetch("https://api.github.com/users/steipete/repos?sort=updated&per_page=40",
                 accept='application/vnd.github+json')
if data:
    repos = json.loads(data)
    print(f"Got {len(repos)} repos")
    # Filter to relevant ones
    interesting = []
    for r in repos:
        name = r.get('name', '')
        desc = r.get('description', '') or ''
        if any(kw in (name + desc).lower() for kw in
               ['crabbox', 'peekaboo', 'repobar', 'codex', 'agent', 'skill', 'claw', 'triage', 'vision']):
            interesting.append({
                'name': name,
                'desc': desc,
                'lang': r.get('language'),
                'stars': r.get('stargazers_count'),
                'pushed': r.get('pushed_at'),
                'default_branch': r.get('default_branch'),
            })
    # Always include the top tools by name match
    forced = ['crabbox', 'peekaboo', 'RepoBar']
    by_name = {r['name'].lower(): r for r in repos}
    for f in forced:
        if f.lower() not in [i['name'].lower() for i in interesting] and f.lower() in by_name:
            r = by_name[f.lower()]
            interesting.append({
                'name': r['name'], 'desc': r.get('description'),
                'lang': r.get('language'), 'stars': r.get('stargazers_count'),
                'pushed': r.get('pushed_at'), 'default_branch': r.get('default_branch'),
            })
    print(json.dumps(interesting, indent=2))
    with open(f"{OUT_DIR}/repos_list.json", 'w') as f:
        json.dump(interesting, f, indent=2)
else:
    print(f"err: {st}")
    interesting = []

# 2. For top relevant repos, fetch README
TARGETS = ['crabbox', 'peekaboo', 'RepoBar']
print("\n\n=== Fetching READMEs ===")
for target_name in TARGETS:
    # Find exact name in repos list
    actual = next((r for r in (repos if data else []) if r['name'].lower() == target_name.lower()), None)
    if not actual:
        print(f"\n{target_name}: NOT FOUND in repos list")
        continue
    branch = actual['default_branch']
    print(f"\n--- {actual['name']} (default branch: {branch}) ---")
    readme_url = f"https://raw.githubusercontent.com/steipete/{actual['name']}/{branch}/README.md"
    content, st = fetch(readme_url)
    if content:
        out_path = f"{OUT_DIR}/{actual['name']}_README.md"
        with open(out_path, 'w') as f:
            f.write(content)
        print(f"  saved {len(content)} bytes -> {out_path}")
    else:
        print(f"  err: {st}")

# 3. Look for autotriage skill — likely in a repo like 'agents' or 'codex-skills' or a gist
# Try common patterns
print("\n\n=== Searching for autotriage skill / agents config ===")
for candidate_repo in ['agents', 'codex-skills', 'skills', 'codex-rules', 'dotcodex', '.codex']:
    actual = next((r for r in (repos if data else []) if r['name'].lower() == candidate_repo.lower()), None)
    if actual:
        print(f"  FOUND candidate: {actual['name']}")

# Get tree for a couple of important repos to understand structure
print("\n\n=== Tree structure for crabbox + peekaboo ===")
for repo_name in ['crabbox', 'peekaboo']:
    actual = next((r for r in (repos if data else []) if r['name'].lower() == repo_name.lower()), None)
    if not actual: continue
    branch = actual['default_branch']
    url = f"https://api.github.com/repos/steipete/{actual['name']}/git/trees/{branch}?recursive=1"
    content, st = fetch(url, accept='application/vnd.github+json')
    if content:
        try:
            tree = json.loads(content)
            files = tree.get('tree', [])
            print(f"\n--- {actual['name']}: {len(files)} entries (first 40) ---")
            for f in files[:40]:
                if f.get('type') == 'blob':
                    print(f"  {f.get('path')} ({f.get('size','-')} bytes)")
        except Exception as e:
            print(f"  parse err: {e}")
    else:
        print(f"  err: {st}")
