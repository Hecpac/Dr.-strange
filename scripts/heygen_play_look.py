"""Click play on one of the newly generated 'Cream Blazer Agent of Houston' looks.

Strategy:
- Connect to existing HeyGen avatar-group tab.
- Find an image with alt containing 'Cream Blazer Agent'.
- Find the nearest play-button (svg/button) inside its tile container, click it.
- If no play button, click the tile to open detail view, then click the top play.
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
    path = f"{ART}/play_{name}_{int(time.time())}.png"
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
        # If we are on add-more-looks page, navigate back to my-avatars
        send(ws, 3, "Page.navigate", {"url": "https://app.heygen.com/avatar/my-avatars?groupId=0f4c93f45ab5417ca579034c7e975ce6&tab=private"})
        time.sleep(5)
        print("initial_shot:", shot(ws, 10, "0_initial"))

        # Find first cream blazer look, hover via JS dispatch then click play if visible
        hover_click_js = r"""
        (() => {
          const imgs = Array.from(document.querySelectorAll('img'));
          const targets = imgs.filter(i => /cream blazer agent/i.test(i.alt || ''));
          if (!targets.length) return JSON.stringify({ok:false, reason:'no_target_image', sample: imgs.slice(0,15).map(i => i.alt).filter(Boolean)});
          const t = targets[0];
          // walk to tile container (a few parents up)
          let tile = t;
          for (let i=0; i<5; i++) { if (!tile.parentElement) break; tile = tile.parentElement; if (tile.querySelector && tile.querySelectorAll('button').length > 0) break; }
          // dispatch mouseenter/over events on the tile
          ['mouseover','mouseenter','mousemove'].forEach(ev => tile.dispatchEvent(new MouseEvent(ev, {bubbles:true, cancelable:true, view:window})));
          // look for a play-shaped button or svg
          const buttons = Array.from(tile.querySelectorAll('button'));
          // Heuristic: a small button near the center/bottom of the tile or one with aria-label "Play"
          let playBtn = buttons.find(b => /play|preview|reproduce/i.test((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')));
          if (!playBtn) {
            const svgs = Array.from(tile.querySelectorAll('svg'));
            const playSvg = svgs.find(s => /play/i.test((s.getAttribute('aria-label') || '') + ' ' + (s.getAttribute('class') || '') + ' ' + (s.innerHTML || '')));
            if (playSvg) playBtn = playSvg.closest('button') || playSvg.parentElement;
          }
          if (!playBtn) {
            // last resort: click the image itself to open detail
            t.click();
            return JSON.stringify({ok:true, action:'clicked_image_to_open_detail', alt: t.alt});
          }
          playBtn.click();
          return JSON.stringify({ok:true, action:'clicked_play_button', label: (playBtn.getAttribute('aria-label')||'') + ' / ' + (playBtn.innerText||'').slice(0,40)});
        })()
        """
        r = send(ws, 20, "Runtime.evaluate", {"expression": hover_click_js, "returnByValue": True})
        print("interaction:", r.get("result", {}).get("result", {}).get("value", ""))
        time.sleep(3)
        print("post_click_shot:", shot(ws, 21, "1_after_play"))

        # Check for active video element to confirm playback
        video_js = r"""
        (() => {
          const vids = Array.from(document.querySelectorAll('video')).filter(v => {
            const r = v.getBoundingClientRect();
            return r.width > 50 && r.height > 50;
          });
          const info = vids.map(v => ({src: (v.currentSrc||v.src||'').slice(0,150), paused:v.paused, duration:v.duration, currentTime:v.currentTime, w:Math.round(v.getBoundingClientRect().width), h:Math.round(v.getBoundingClientRect().height)}));
          return JSON.stringify({count: vids.length, videos: info});
        })()
        """
        r2 = send(ws, 30, "Runtime.evaluate", {"expression": video_js, "returnByValue": True})
        print("video_state:", r2.get("result", {}).get("result", {}).get("value", ""))
        time.sleep(2)
        print("final_shot:", shot(ws, 31, "2_final"))
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
