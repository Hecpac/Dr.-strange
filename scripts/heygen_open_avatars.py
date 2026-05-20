"""Open HeyGen avatars page via Chrome CDP (existing logged-in session) and screenshot."""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

import websocket

CDP_HTTP = "http://localhost:9250"
TARGET = "https://app.heygen.com/avatars"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/heygen"


def http_get(path: str):
    with urllib.request.urlopen(CDP_HTTP + path, timeout=5) as r:
        return json.loads(r.read())


def http_put(path: str):
    req = urllib.request.Request(CDP_HTTP + path, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def find_or_open():
    tabs = http_get("/json/list")
    for t in tabs:
        if t.get("type") == "page" and "heygen.com" in (t.get("url") or ""):
            return t
    return http_put(f"/json/new?{TARGET}")


def send(ws, msg_id, method, params=None):
    payload = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        data = json.loads(ws.recv())
        if data.get("id") == msg_id:
            return data


def main() -> int:
    import os
    os.makedirs(ART, exist_ok=True)
    tab = find_or_open()
    print(f"tab_id={tab['id']} url={(tab.get('url') or '')[:120]}")
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        send(ws, 3, "Page.navigate", {"url": TARGET})
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                ws.settimeout(2.0)
                evt = json.loads(ws.recv())
                if evt.get("method") == "Page.loadEventFired":
                    break
            except Exception:
                pass
        ws.settimeout(15)
        time.sleep(5)
        info = send(ws, 10, "Runtime.evaluate", {
            "expression": (
                "JSON.stringify({"
                "url: location.href,"
                "title: document.title,"
                "h1s: Array.from(document.querySelectorAll('h1,h2')).slice(0,8).map(e=>e.innerText.trim()),"
                "buttons: Array.from(document.querySelectorAll('button')).slice(0,30)"
                ".map(b=>(b.innerText||b.getAttribute('aria-label')||'').trim()).filter(Boolean)"
                "})"
            ),
            "returnByValue": True,
        })
        val = info.get("result", {}).get("result", {}).get("value", "")
        print("page_info:", val[:1500])
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png"})
        b64 = shot.get("result", {}).get("data", "")
        if not b64:
            print("ERROR no screenshot")
            return 1
        ts = int(time.time())
        path = f"{ART}/avatars_landing_{ts}.png"
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"screenshot={path}")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
