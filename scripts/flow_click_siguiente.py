"""Click 'Siguiente' on Flow consent modal via existing Chrome CDP."""
from __future__ import annotations

import base64
import json
import time
import urllib.request

import websocket

CDP_HTTP = "http://localhost:9250"


def http_get(path: str):
    with urllib.request.urlopen(CDP_HTTP + path, timeout=5) as r:
        return json.loads(r.read())


def send(ws, msg_id: int, method: str, params: dict | None = None):
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
    tabs = http_get("/json/list")
    tab = None
    for t in tabs:
        if t.get("type") == "page" and "labs.google/fx" in t.get("url", "") and "flow" in t.get("url", ""):
            tab = t
            break
    if not tab:
        print("ERR: no flow tab")
        return 1
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        # Find and click the Siguiente button via JS
        result = send(ws, 2, "Runtime.evaluate", {
            "expression": """
            (() => {
              const btns = Array.from(document.querySelectorAll('button'));
              const target = btns.find(b => /siguiente/i.test(b.innerText.trim()));
              if (!target) return JSON.stringify({clicked: false, reason: 'no_button', buttons: btns.map(b=>b.innerText.trim()).slice(0,20)});
              const r = target.getBoundingClientRect();
              target.scrollIntoView();
              target.click();
              return JSON.stringify({clicked: true, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), text: target.innerText.trim()});
            })()
            """,
            "returnByValue": True,
        })
        val = result.get("result", {}).get("result", {}).get("value", "")
        print("click_result:", val)
        # Wait for SPA transition
        time.sleep(5)
        post_info = send(ws, 3, "Runtime.evaluate", {
            "expression": "JSON.stringify({url: location.href, title: document.title, bodyLen: document.body.innerText.length, snippet: document.body.innerText.slice(0,400)})",
            "returnByValue": True,
        })
        print("post:", post_info.get("result", {}).get("result", {}).get("value", ""))
        shot = send(ws, 4, "Page.captureScreenshot", {"format": "png"})
        b64 = shot.get("result", {}).get("data", "")
        ts = int(time.time())
        out = f"/Users/hector/Projects/Dr.-strange/artifacts/flow_after_siguiente_{ts}.png"
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"screenshot={out}")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
