"""Open the working Tatiana page (tcinsurancetx.com) via CDP and screenshot."""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

import websocket


CDP_HTTP = "http://localhost:9250"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/heygen"
TARGET = "https://tcinsurancetx.com/"


def http_get(p):
    with urllib.request.urlopen(CDP_HTTP + p, timeout=5) as r:
        return json.loads(r.read())


def http_put(p):
    req = urllib.request.Request(CDP_HTTP + p, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def send(ws, mid, method, params=None):
    payload = {"id": mid, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        d = json.loads(ws.recv())
        if d.get("id") == mid:
            return d


def main():
    import os
    os.makedirs(ART, exist_ok=True)
    # Reuse the existing error tab pointing to tic*; navigate it to the working tc* domain.
    tab = None
    for t in http_get("/json/list"):
        if t.get("type") == "page":
            u = t.get("url") or ""
            if "tcinsurancetx.com" in u or "ticinsurancetx.com" in u or "chrome-error" in u:
                tab = t
                break
    if tab is None:
        tab = http_put(f"/json/new?{TARGET}")
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        send(ws, 3, "Page.bringToFront")
        send(ws, 4, "Page.navigate", {"url": TARGET})
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
        time.sleep(4)
        info = send(ws, 10, "Runtime.evaluate", {
            "expression": "JSON.stringify({u:location.href,t:document.title,h1s:Array.from(document.querySelectorAll('h1,h2')).slice(0,5).map(e=>e.innerText.trim()),lang:document.documentElement.lang})",
            "returnByValue": True,
        })
        print("page_info:", info.get("result", {}).get("result", {}).get("value", ""))
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        b64 = shot.get("result", {}).get("data", "")
        path = f"{ART}/tatiana_page_live_{int(time.time())}.png"
        if b64:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"screenshot={path}")
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
