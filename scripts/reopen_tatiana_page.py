"""Reopen Tatiana's page (TIC Insurance) via Chrome CDP and capture state.

Tries the new domain first; falls back to the old if needed.
Returns: DNS/HTTP status for both, plus screenshot + title of the loaded page.
"""
from __future__ import annotations

import base64
import json
import socket
import sys
import time
import urllib.request

import requests
import websocket


CDP_HTTP = "http://localhost:9250"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/heygen"
NEW_DOMAIN = "https://ticinsurancetx.com"
OLD_DOMAIN = "https://tcinsurancetx.com"


def http_get(p):
    with urllib.request.urlopen(CDP_HTTP + p, timeout=5) as r:
        return json.loads(r.read())


def http_put(p):
    req = urllib.request.Request(CDP_HTTP + p, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def find_or_open(url, host_substr):
    tabs = http_get("/json/list")
    for t in tabs:
        if t.get("type") == "page" and host_substr in (t.get("url") or ""):
            return t, False
    return http_put(f"/json/new?{url}"), True


def send(ws, mid, method, params=None):
    payload = {"id": mid, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == mid:
            return d


def dns_check(host):
    try:
        return {"host": host, "ips": socket.gethostbyname_ex(host)[2]}
    except Exception as e:
        return {"host": host, "error": str(e)[:140]}


def http_check(url):
    try:
        r = requests.get(url, timeout=10, allow_redirects=True)
        return {"url": url, "status": r.status_code, "final": r.url, "title_hint": r.text[:200].replace("\n", " ")[:200]}
    except Exception as e:
        return {"url": url, "error": str(e)[:200]}


def navigate_and_shot(target_url, host_substr, label):
    import os
    os.makedirs(ART, exist_ok=True)
    tab, opened_new = find_or_open(target_url, host_substr)
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        # Bring to front
        send(ws, 3, "Page.bringToFront")
        # Navigate (force reload to bypass cached DNS error)
        send(ws, 4, "Page.navigate", {"url": target_url})
        # Wait for load
        deadline = time.time() + 25
        loaded = False
        while time.time() < deadline:
            try:
                ws.settimeout(2.0)
                evt = json.loads(ws.recv())
                if evt.get("method") == "Page.loadEventFired":
                    loaded = True
                    break
            except Exception:
                pass
        ws.settimeout(15)
        time.sleep(4)
        info = send(ws, 10, "Runtime.evaluate", {
            "expression": "JSON.stringify({u:location.href,t:document.title,h1:(document.querySelector('h1')||{}).innerText||'',isError: /chrome-error|chromewebdata/.test(location.href) || /This site can.t be reached|ERR_NAME_NOT_RESOLVED/.test(document.body && document.body.innerText || '')})",
            "returnByValue": True,
        })
        val = info.get("result", {}).get("result", {}).get("value", "")
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png"})
        b64 = shot.get("result", {}).get("data", "")
        path = f"{ART}/reopen_{label}_{int(time.time())}.png"
        if b64:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
        return {"target": target_url, "tab_id": tab["id"], "opened_new": opened_new, "loaded_event": loaded, "page": val, "screenshot": path}
    finally:
        ws.close()


def main():
    print(json.dumps({"dns": [dns_check("ticinsurancetx.com"), dns_check("www.ticinsurancetx.com"), dns_check("tcinsurancetx.com")]}, indent=2))
    print(json.dumps({"http": [http_check(NEW_DOMAIN), http_check(OLD_DOMAIN)]}, indent=2))
    # Open new domain first
    r1 = navigate_and_shot(NEW_DOMAIN, "ticinsurancetx.com", "new")
    print("new_domain_result:")
    print(json.dumps(r1, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
