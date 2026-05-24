"""Inspect current state of HeyGen Add-a-look page after submission."""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

import websocket


CDP_HTTP = "http://localhost:9250"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/heygen"


def http_get(p):
    with urllib.request.urlopen(CDP_HTTP + p, timeout=5) as r:
        return json.loads(r.read())


def find_tab():
    for t in http_get("/json/list"):
        if t.get("type") == "page" and "app.heygen.com/avatar" in (t.get("url") or ""):
            return t
    raise SystemExit("no_tab")


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
    tab = find_tab()
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        time.sleep(1)
        # Inspect all textareas and their visible content + headings
        js = r"""
        (() => {
          const inputs = Array.from(document.querySelectorAll('textarea, [contenteditable=\"true\"], input'));
          const summary = inputs.map(el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return {
              tag: el.tagName,
              type: el.type || '',
              placeholder: el.placeholder || el.getAttribute('aria-placeholder') || '',
              value: (el.value || el.innerText || '').slice(0, 200),
              rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
              visible: r.width > 5 && r.height > 5 && s.visibility !== 'hidden' && s.display !== 'none',
            };
          }).filter(x => x.visible);
          // Also count generating tiles or completed look thumbnails
          const tiles = Array.from(document.querySelectorAll('img, [role=\"img\"], [class*=\"thumbnail\"], [class*=\"look\"]')).length;
          // Find any text containing 'Generating' or 'looks'
          const genText = document.body.innerText.match(/Generating [^\n]{0,80}/) || [];
          return JSON.stringify({inputs: summary, tile_count: tiles, gen: Array.from(genText), title: document.title, url: location.href});
        })()
        """
        r = send(ws, 10, "Runtime.evaluate", {"expression": js, "returnByValue": True})
        print(r.get("result", {}).get("result", {}).get("value", ""))
        # screenshot
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png"})
        b64 = shot.get("result", {}).get("data", "")
        path = f"{ART}/look_state_{int(time.time())}.png"
        if b64:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"screenshot={path}")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
