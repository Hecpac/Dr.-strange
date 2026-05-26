"""V2: Fetch READMEs from steipete + OpenClaw org repos that matter."""
import urllib.request, json, os

OUT_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/steipete_stack"
os.makedirs(OUT_DIR, exist_ok=True)

def fetch(url, accept=None):
    headers = {'User-Agent': 'Mozilla/5.0'}
    if accept: headers['Accept'] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return f"ERR:{e}"

# Priority repos (steipete personal)
TARGETS = [
    ('steipete', 'agent-rules', 'main'),
    ('steipete', 'agent-scripts', 'main'),
    ('steipete', 'CodexBar', 'main'),
    ('steipete', 'birdclaw', 'main'),
    ('steipete', 'claude-code-mcp', 'main'),
    ('steipete', 'brabble', 'main'),
    ('steipete', 'deepsec', 'main'),
    ('steipete', 'bslog', 'main'),
    ('steipete', 'vox', 'main'),
]

# Also try OpenClaw org for crabbox/peekaboo
print("=== Listing @OpenClaw org repos ===")
data = fetch("https://api.github.com/orgs/OpenClaw/repos?per_page=30",
             accept='application/vnd.github+json')
if not data.startswith('ERR'):
    try:
        org_repos = json.loads(data)
        print(f"OpenClaw has {len(org_repos)} repos")
        for r in org_repos[:20]:
            print(f"  - {r.get('name')}: {r.get('description','')} ({r.get('stargazers_count')}⭐)")
        # Add crabbox / peekaboo to targets if found
        for r in org_repos:
            name = r.get('name', '').lower()
            if 'crabbox' in name or 'peekaboo' in name:
                TARGETS.append(('OpenClaw', r['name'], r['default_branch']))
    except Exception as e:
        print(f"parse err: {e}")
else:
    print(f"OpenClaw err: {data}")

# Fetch README for each target
print("\n\n=== Fetching READMEs ===")
for owner, repo, branch in TARGETS:
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
    content = fetch(url)
    if content.startswith('ERR') or len(content) < 50:
        # Try master
        url2 = f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md"
        content = fetch(url2)
    if not content.startswith('ERR') and len(content) > 50:
        out = f"{OUT_DIR}/{owner}__{repo}_README.md"
        with open(out, 'w') as f:
            f.write(content)
        print(f"  ✅ {owner}/{repo}: {len(content)} bytes")
    else:
        print(f"  ❌ {owner}/{repo}: {content[:80]}")
