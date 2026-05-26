"""Fetch openclaw core + Peekaboo + AXorcist + the 145-byte agent-rules README."""
import urllib.request, os

OUT_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/steipete_stack"

def fetch(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode('utf-8', errors='replace')
    except Exception as e:
        return f"ERR:{e}"

# Most relevant repos for comparison with Dr. Strange
TARGETS = [
    ('OpenClaw', 'openclaw', 'main'),
    ('OpenClaw', 'AXorcist', 'main'),
    ('OpenClaw', 'mcporter', 'main'),
    ('OpenClaw', 'clawhub', 'main'),
    ('OpenClaw', 'Tachikoma', 'main'),
]

for owner, repo, branch in TARGETS:
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
    content = fetch(url)
    if content.startswith('ERR') or len(content) < 50:
        url2 = f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md"
        content = fetch(url2)
    if not content.startswith('ERR'):
        out = f"{OUT_DIR}/{owner}__{repo}_README.md"
        with open(out, 'w') as f:
            f.write(content)
        print(f"✅ {owner}/{repo}: {len(content)} bytes")
    else:
        print(f"❌ {owner}/{repo}: {content[:80]}")

# Read the 145-byte agent-rules README to see what it points to
print("\n--- agent-rules content ---")
content = fetch("https://raw.githubusercontent.com/steipete/agent-rules/main/README.md")
print(content)
