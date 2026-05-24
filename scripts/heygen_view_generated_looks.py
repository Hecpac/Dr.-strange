"""Click View to open generated-looks panel and capture the result."""
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
        # Find and click "View" button (right side)
        click_js = r"""
        (() => {
          const btns = Array.from(document.querySelectorAll('button, a, div'));
          const candidates = btns.filter(b => {
            const t = (b.innerText||'').trim();
            return /^view$/i.test(t) || /click to view/i.test(t) || /done.*view/i.test(t);
          });
          if (!candidates.length) return JSON.stringify({ok:false, sample:Array.from(document.querySelectorAll('button')).slice(0,30).map(b=>(b.innerText||'').trim()).filter(Boolean)});
          // Pick smallest (innermost View label)
          candidates.sort((a,b) => (a.innerText.length - b.innerText.length));
          const target = candidates[0];
          let click_el = target;
          for (let i=0; i<3 && click_el && click_el.tagName !== 'BUTTON' && click_el.tagName !== 'A'; i++) {
            click_el = click_el.parentElement || click_el;
            if (click_el.tagName === 'BUTTON' || click_el.tagName === 'A') break;
          }
          click_el.scrollIntoView({block:'center'});
          click_el.click();
          return JSON.stringify({ok:true, text:(click_el.innerText||'').slice(0,80), tag:click_el.tagName});
        })()
        """
        r = send(ws, 10, "Runtime.evaluate", {"expression": click_js, "returnByValue": True})
        print("view_click:", r.get("result", {}).get("result", {}).get("value", ""))
        time.sleep(4)
        # Screenshot
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        b64 = shot.get("result", {}).get("data", "")
        path = f"{ART}/generated_looks_view_{int(time.time())}.png"
        if b64:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"screenshot={path}")
        # Also inspect any img alts/captions to confirm they're our generated ones
        info_js = r"""
        (() => {
          // Look for the generated-looks panel: usually a modal or sidebar with images
          const imgs = Array.from(document.querySelectorAll('img')).filter(i => {
            const r = i.getBoundingClientRect();
            return r.width > 100 && r.height > 100;
          });
          const top = imgs.slice(0, 12).map(i => ({src: i.src.slice(0,120), alt: i.alt || '', x: Math.round(i.getBoundingClientRect().x), y: Math.round(i.getBoundingClientRect().y), w: Math.round(i.getBoundingClientRect().width)}));
          // Get any modal title
          const modalTitle = (document.querySelector('[role=\"dialog\"] h1, [role=\"dialog\"] h2, [class*=\"modal\"] h2, [class*=\"drawer\"] h2') || {}).innerText || '';
          return JSON.stringify({top_images: top, modal_title: modalTitle.slice(0,200)});
        })()
        """
        r2 = send(ws, 12, "Runtime.evaluate", {"expression": info_js, "returnByValue": True})
        print("info:", r2.get("result", {}).get("result", {}).get("value", ""))
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
