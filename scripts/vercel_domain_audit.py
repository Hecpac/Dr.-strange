"""Deep audit: DNS verification + domain config status."""
import json
import urllib.request
import urllib.error
import subprocess
from pathlib import Path

auth = json.loads((Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json").read_text())
token = auth["token"]
TEAM_ID = "team_6gHC5S3mVmLqGY1eth4qYXie"

ALL_DOMAINS = [
    "pachanodesign.com",
    "premiumhome.design", "www.premiumhome.design",
    "sinpetca.com", "www.sinpetca.com",
    "tcinsurancetx.com", "www.tcinsurancetx.com",
]

PROJECT_IDS = {
    "hector-services": "prj_I40veWjZ8a7WW93Oi88a4uk6dK9z",
    "phd": "prj_pp67BMfMsztDOHsfEx2xjV0O3WUj",
    "sinpetca": "prj_wlI6O8APoW1oL65JB4qX9gzf6wUh",
    "tc-insurance": "prj_L0e3fX8VczNJPM0vif2UyFg6qnUw",
}

def api_get(path):
    url = f"https://api.vercel.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:500]}

def dig(domain, rtype="A"):
    r = subprocess.run(["dig", "+short", domain, rtype], capture_output=True, text=True, timeout=10)
    return r.stdout.strip()

print("=" * 70)
print("DNS + VERCEL DOMAIN VERIFICATION AUDIT")
print("=" * 70)

for domain in ALL_DOMAINS:
    print(f"\n--- {domain} ---")

    # DNS records
    a = dig(domain, "A")
    cname = dig(domain, "CNAME")
    ns = dig(domain.split(".")[-2] + "." + domain.split(".")[-1] if not domain.startswith("www") else domain.replace("www.", ""), "NS")
    print(f"  A:     {a or '(none)'}")
    print(f"  CNAME: {cname or '(none)'}")
    if not domain.startswith("www"):
        print(f"  NS:    {ns or '(none)'}")

    # Vercel domain config
    d = api_get(f"/v5/domains/{domain}?teamId={TEAM_ID}")
    if "error" not in d:
        print(f"  Vercel: verified={d.get('verified')} cdnEnabled={d.get('cdnEnabled')}")
        if d.get("verification"):
            for v in d["verification"]:
                print(f"    verification: type={v.get('type')} domain={v.get('domain')} value={v.get('value')}")
    else:
        print(f"  Vercel: {d}")

# Check domain config within each project
print("\n\n" + "=" * 70)
print("PROJECT DOMAIN STATUS")
print("=" * 70)

for pname, pid in PROJECT_IDS.items():
    print(f"\n--- {pname} ---")
    data = api_get(f"/v9/projects/{pname}/domains?teamId={TEAM_ID}")
    for d in data.get("domains", []):
        verified = d.get("verified", "?")
        print(f"  {d['name']}: verified={verified} redirect={d.get('redirect')} gitBranch={d.get('gitBranch')}")
