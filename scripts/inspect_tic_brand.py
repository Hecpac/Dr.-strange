"""Open Tatiana's TIC Insurance site via Chrome CDP and capture brand context.

Captures: viewport screenshot, page title, dominant text content, h1/h2,
detected brand colors from CSS variables and computed background colors.
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request

import websocket


CDP_HTTP = "http://localhost:9250"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/heygen"
TARGETS = [
    "https://www.ticinsurancetx.com/",
    "https://ticinsurancetx.com/",
    "https://www.tcinsurancetx.com/",
    "https://tcinsurancetx.com/",
]


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


def first_working_url():
    # Try HEAD-style by opening and checking title
    return TARGETS[0]


def main():
    import os
    os.makedirs(ART, exist_ok=True)

    target = first_working_url()
    tab = http_put(f"/json/new?{target}")
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        # Wait for load
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

        info_js = r"""
(() => {
  const computed = getComputedStyle(document.body);
  const bg = computed.backgroundColor;
  const fg = computed.color;
  const fontFamily = computed.fontFamily;
  const headings = Array.from(document.querySelectorAll('h1,h2,h3')).slice(0,12).map(h => ({tag: h.tagName, text: h.innerText.trim().slice(0,200)}));
  const ctas = Array.from(document.querySelectorAll('a,button')).filter(el => el.innerText && el.innerText.trim().length < 40).slice(0,20).map(el => el.innerText.trim());
  // Sample colors used across visible elements
  const samples = [];
  for (const sel of ['header', 'nav', 'main section:first-of-type', 'footer', 'a.btn, button.btn, .btn-primary, [class*="primary"]', '[class*="hero"]', '[class*="cta"]']) {
    const el = document.querySelector(sel);
    if (el) {
      const cs = getComputedStyle(el);
      samples.push({sel, bg: cs.backgroundColor, color: cs.color, borderColor: cs.borderColor});
    }
  }
  // Look for logo image
  const logos = Array.from(document.querySelectorAll('img')).filter(i => /logo/i.test((i.alt||'') + ' ' + (i.src||''))).slice(0,4).map(i => i.src);
  return JSON.stringify({
    url: location.href, title: document.title,
    body_bg: bg, body_fg: fg, font: fontFamily,
    headings, ctas, samples, logos
  });
})()
"""
        r = send(ws, 10, "Runtime.evaluate", {"expression": info_js, "returnByValue": True})
        val = r.get("result", {}).get("result", {}).get("value", "")
        print("BRAND_INFO:")
        print(val)
        shot = send(ws, 11, "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        b64 = shot.get("result", {}).get("data", "")
        ts = int(time.time())
        path = f"{ART}/tic_brand_{ts}.png"
        if b64:
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"screenshot={path}")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
