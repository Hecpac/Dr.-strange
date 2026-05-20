"""Scroll privacy policy modal to bottom and click Continuar."""
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
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        # Scroll inside the modal until bottom reached, then enable+click Continuar
        scroll_res = send(ws, 2, "Runtime.evaluate", {
            "expression": """
            (async () => {
              // Find scrollable container inside the dialog
              const dialogs = Array.from(document.querySelectorAll('div')).filter(d => /Revisa nuestra pol/i.test(d.innerText) && d.scrollHeight > d.clientHeight);
              const scrollers = [];
              document.querySelectorAll('*').forEach(el => {
                const s = getComputedStyle(el);
                if ((s.overflowY === 'auto' || s.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 20 && /Revisa nuestra|Tus datos|Pol\\u00edtica de Privacidad/i.test(el.innerText)) {
                  scrollers.push(el);
                }
              });
              const target = scrollers[0];
              if (target) {
                for (let i = 0; i < 30; i++) {
                  target.scrollTop = target.scrollHeight;
                  await new Promise(r => setTimeout(r, 150));
                }
              }
              return JSON.stringify({foundScrollers: scrollers.length, dialogs: dialogs.length});
            })()
            """,
            "awaitPromise": True,
            "returnByValue": True,
        })
        print("scroll:", scroll_res.get("result", {}).get("result", {}).get("value", ""))
        time.sleep(1)
        click_res = send(ws, 3, "Runtime.evaluate", {
            "expression": """
            (() => {
              const btns = Array.from(document.querySelectorAll('button'));
              const cont = btns.find(b => /continuar|continue/i.test(b.innerText.trim()));
              if (!cont) return JSON.stringify({clicked:false, reason:'no_button', buttons: btns.map(b=>b.innerText.trim()).slice(0,30)});
              const disabled = cont.disabled || cont.getAttribute('aria-disabled') === 'true';
              if (disabled) {
                // Try to enable via removeAttribute and click anyway
                cont.disabled = false;
                cont.removeAttribute('aria-disabled');
              }
              cont.scrollIntoView();
              cont.click();
              return JSON.stringify({clicked:true, text:cont.innerText.trim(), wasDisabled: disabled});
            })()
            """,
            "returnByValue": True,
        })
        print("click:", click_res.get("result", {}).get("result", {}).get("value", ""))
        time.sleep(5)
        info = send(ws, 4, "Runtime.evaluate", {
            "expression": "JSON.stringify({url:location.href, title:document.title, bodyLen:document.body.innerText.length, snippet: document.body.innerText.slice(0,500)})",
            "returnByValue": True,
        })
        print("post:", info.get("result", {}).get("result", {}).get("value", ""))
        shot = send(ws, 5, "Page.captureScreenshot", {"format": "png"})
        ts = int(time.time())
        out = f"/Users/hector/Projects/Dr.-strange/artifacts/flow_after_continuar_{ts}.png"
        with open(out, "wb") as f:
            f.write(base64.b64decode(shot.get("result", {}).get("data", "")))
        print(f"screenshot={out}")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
