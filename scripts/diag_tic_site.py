"""Diagnose why TIC Insurance domain(s) are unreachable.

Checks: DNS (Python socket), HTTP via requests, status codes, IP, headers.
"""
from __future__ import annotations

import json
import socket
import sys

import requests

DOMAINS = [
    "ticinsurancetx.com",
    "www.ticinsurancetx.com",
    "tcinsurancetx.com",
    "www.tcinsurancetx.com",
]


def diag(host: str):
    out = {"host": host}
    # DNS A
    try:
        out["ipv4"] = socket.gethostbyname_ex(host)[2]
    except Exception as e:
        out["dns_error"] = str(e)
    # HTTPS
    try:
        r = requests.get(f"https://{host}", timeout=10, allow_redirects=True)
        out["https_status"] = r.status_code
        out["https_url"] = r.url
        out["https_server"] = r.headers.get("server", "")
        out["https_x_powered"] = r.headers.get("x-powered-by", "")
        out["https_vercel_id"] = r.headers.get("x-vercel-id", "")
        out["https_cf_ray"] = r.headers.get("cf-ray", "")
        out["https_title"] = ""
        if r.text:
            import re
            m = re.search(r"<title[^>]*>([^<]+)</title>", r.text, re.I)
            if m:
                out["https_title"] = m.group(1).strip()[:120]
    except Exception as e:
        out["https_error"] = str(e)[:200]
    # HTTP fallback
    try:
        r = requests.get(f"http://{host}", timeout=8, allow_redirects=False)
        out["http_status"] = r.status_code
        out["http_location"] = r.headers.get("location", "")
    except Exception as e:
        out["http_error"] = str(e)[:160]
    return out


def main():
    results = [diag(h) for h in DOMAINS]
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
