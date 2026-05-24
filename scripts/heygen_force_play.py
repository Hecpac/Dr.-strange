"""Force-play the preview video on the detail view and capture frames."""
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


def shot(ws, mid, name):
    import os
    os.makedirs(ART, exist_ok=True)
    r = send(ws, mid, "Page.captureScreenshot", {"format": "png"})
    b64 = r.get("result", {}).get("data", "")
    path = f"{ART}/playback_{name}_{int(time.time())}.png"
    if b64:
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
    return path


def main():
    tab = find_tab()
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        time.sleep(1)
        # Play any visible video; unmute as a courtesy but keep volume low
        play_js = r"""
        (async () => {
          const vids = Array.from(document.querySelectorAll('video')).filter(v => {
            const r = v.getBoundingClientRect();
            return r.width > 50 && r.height > 50;
          });
          if (!vids.length) return JSON.stringify({ok:false, reason:'no_video'});
          const v = vids[0];
          v.muted = false;
          v.volume = 0.6;
          try {
            await v.play();
            return JSON.stringify({ok:true, src:v.currentSrc.slice(0,150), duration:v.duration, paused:v.paused, muted:v.muted, vol:v.volume});
          } catch (e) {
            // Some browsers refuse autoplay-with-sound; retry muted
            v.muted = true;
            try { await v.play(); } catch (e2) {}
            return JSON.stringify({ok:false, err:String(e), muted_after_retry:v.muted, paused_after:v.paused});
          }
        })()
        """
        r = send(ws, 10, "Runtime.evaluate", {"expression": play_js, "awaitPromise": True, "returnByValue": True})
        print("play_result:", r.get("result", {}).get("result", {}).get("value", ""))
        time.sleep(2)
        print(shot(ws, 11, "playing_2s"))
        time.sleep(3)
        # Probe again
        probe_js = r"""
        (() => {
          const vids = Array.from(document.querySelectorAll('video')).filter(v => v.getBoundingClientRect().width > 50);
          if (!vids.length) return '{}';
          const v = vids[0];
          return JSON.stringify({paused: v.paused, currentTime: v.currentTime, duration: v.duration, ended: v.ended, muted: v.muted});
        })()
        """
        r2 = send(ws, 20, "Runtime.evaluate", {"expression": probe_js, "returnByValue": True})
        print("probe_5s:", r2.get("result", {}).get("result", {}).get("value", ""))
        print(shot(ws, 21, "playing_5s"))
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
