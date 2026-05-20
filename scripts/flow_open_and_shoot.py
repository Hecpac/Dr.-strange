"""Open Flow tab in existing Chrome (CDP @ 9250) and screenshot the result.

Reuses Hector's authenticated Chrome session. Saves screenshot to
artifacts/flow_state_<ts>.png and prints the page URL + title.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.request
import sys

import websocket


CDP_HTTP = "http://localhost:9250"
TARGET_URL = "https://labs.google/fx/tools/flow"


def http_get(path: str) -> list | dict:
    with urllib.request.urlopen(CDP_HTTP + path, timeout=5) as r:
        return json.loads(r.read())


def http_put(path: str) -> list | dict:
    req = urllib.request.Request(CDP_HTTP + path, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def find_or_create_page() -> dict:
    tabs = http_get("/json/list")
    for t in tabs:
        if t.get("type") != "page":
            continue
        url = t.get("url", "")
        if "labs.google/fx/tools/flow" in url:
            return t
    # No existing flow tab — open new one
    return http_put(f"/json/new?{TARGET_URL}")


def send(ws, msg_id: int, method: str, params: dict | None = None) -> dict:
    payload = {"id": msg_id, "method": method}
    if params:
        payload["params"] = params
    ws.send(json.dumps(payload))
    while True:
        raw = ws.recv()
        data = json.loads(raw)
        if data.get("id") == msg_id:
            return data


def main() -> int:
    tab = find_or_create_page()
    ws_url = tab["webSocketDebuggerUrl"]
    print(f"tab_id={tab['id']} url={tab.get('url','')[:80]}")
    ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "DOM.enable")
        # Navigate (idempotent — if already on flow, this just reloads/lands)
        send(ws, 3, "Page.navigate", {"url": TARGET_URL})
        # Wait for load
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                ws.settimeout(2.0)
                raw = ws.recv()
                evt = json.loads(raw)
                if evt.get("method") == "Page.loadEventFired":
                    break
            except Exception:
                pass
        ws.settimeout(15)
        # Extra settle for SPA
        time.sleep(4)
        info = send(ws, 10, "Runtime.evaluate", {
            "expression": "JSON.stringify({url: location.href, title: document.title, bodyLen: document.body ? document.body.innerText.length : 0, hasConsentNext: !!Array.from(document.querySelectorAll('button')).find(b => /siguiente|next|continue/i.test(b.innerText))})",
            "returnByValue": True,
        })
        print("info:", info.get("result", {}).get("result", {}).get("value", ""))
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png"})
        b64 = shot.get("result", {}).get("data", "")
        if not b64:
            print("ERROR no screenshot data", file=sys.stderr)
            return 1
        ts = int(time.time())
        out_path = f"/Users/hector/Projects/Dr.-strange/artifacts/flow_state_{ts}.png"
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"screenshot={out_path}")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
