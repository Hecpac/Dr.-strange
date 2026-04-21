"""
Vercel Domain Watchdog — auto-heals domain aliases and notifies via Telegram.
Runs every 5 minutes via launchd.
Uses Vercel REST API directly (no CLI dependency).
"""

import json
import urllib.request
import urllib.error
import os
from pathlib import Path
from datetime import datetime

DOMAINS = {
    "pachanodesign.com": {"project": "prj_I40veWjZ8a7WW93Oi88a4uk6dK9z", "www": False},
    "premiumhome.design": {"project": "prj_pp67BMfMsztDOHsfEx2xjV0O3WUj", "www": True},
    "sinpetca.com": {"project": "prj_wlI6O8APoW1oL65JB4qX9gzf6wUh", "www": True},
    "tcinsurancetx.com": {"project": "prj_L0e3fX8VczNJPM0vif2UyFg6qnUw", "www": True},
}

TEAM_ID = "team_6gHC5S3mVmLqGY1eth4qYXie"

env_path = Path(__file__).parent.parent / ".env"
env_vars = {}
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env_vars[k.strip()] = v.strip()

TG_TOKEN = env_vars.get("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TG_CHAT = env_vars.get("TELEGRAM_ALLOWED_USER_ID", os.environ.get("TELEGRAM_ALLOWED_USER_ID", ""))

AUTH_JSON = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"


def get_vercel_token() -> str:
    if AUTH_JSON.exists():
        data = json.loads(AUTH_JSON.read_text())
        return data.get("token", "")
    return os.environ.get("VERCEL_TOKEN", "")


def vercel_api(method: str, path: str, body: dict = None) -> dict:
    token = get_vercel_token()
    if not token:
        return {}
    sep = "&" if "?" in path else "?"
    url = f"https://api.vercel.com{path}{sep}teamId={TEAM_ID}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


def check_domain(domain: str) -> int:
    try:
        req = urllib.request.Request(f"https://{domain}", method="HEAD")
        req.add_header("User-Agent", "VercelWatchdog/1.0")
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def get_latest_deployment(project: str) -> tuple:
    try:
        data = vercel_api("GET", f"/v6/deployments?projectId={project}&target=production&state=READY&limit=1")
        deployments = data.get("deployments", [])
        if deployments:
            return deployments[0].get("uid", ""), deployments[0].get("url", "")
    except Exception:
        pass
    return "", ""


def restore_alias(deployment_uid: str, domain: str) -> bool:
    try:
        vercel_api("POST", f"/v2/deployments/{deployment_uid}/aliases", {"alias": domain})
        return True
    except Exception:
        return False


def send_telegram(message: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": TG_CHAT, "text": message, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    down = []
    restored = []
    failed = []

    for domain, config in DOMAINS.items():
        status = check_domain(domain)
        if config["www"]:
            www_status = check_domain(f"www.{domain}")
        else:
            www_status = 200

        if 200 <= status < 400 and 200 <= www_status < 400:
            continue

        down.append(domain)

        deploy_uid, deploy_url = get_latest_deployment(config["project"])
        if not deploy_uid:
            failed.append(f"{domain} (no deployment found)")
            continue

        ok = restore_alias(deploy_uid, domain)
        if config["www"]:
            ok2 = restore_alias(deploy_uid, f"www.{domain}")
            ok = ok and ok2

        if ok:
            restored.append(domain)
        else:
            failed.append(domain)

    if not down:
        return

    lines = [f"🔧 *Vercel Watchdog* — {now}"]
    if restored:
        lines.append(f"\n✅ *Restaurados:* {', '.join(restored)}")
    if failed:
        lines.append(f"\n❌ *Fallaron:* {', '.join(failed)}")
    lines.append(f"\n_Dominios caídos detectados: {', '.join(down)}_")

    msg = "\n".join(lines)
    send_telegram(msg)
    print(msg)


if __name__ == "__main__":
    main()
