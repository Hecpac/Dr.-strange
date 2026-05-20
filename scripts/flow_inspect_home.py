"""Inspect Flow home: list all buttons/links to find video-from-image entry."""
from __future__ import annotations
import base64, json, time, urllib.request
import websocket

CDP_HTTP = "http://localhost:9250"


def http_get(p):
    with urllib.request.urlopen(CDP_HTTP+p, timeout=5) as r:
        return json.loads(r.read())


def send(ws, i, m, p=None):
    pl={"id":i,"method":m}
    if p: pl["params"]=p
    ws.send(json.dumps(pl))
    while True:
        d=json.loads(ws.recv())
        if d.get("id")==i: return d


def main():
    tabs=http_get("/json/list")
    tab=next((t for t in tabs if t.get("type")=="page" and "labs.google/fx" in t.get("url","") and "flow" in t.get("url","")), None)
    if not tab:
        print("no flow tab"); return 1
    ws=websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
    try:
        send(ws,1,"Page.enable")
        # Set a larger viewport so we see more of the page
        send(ws,2,"Emulation.setDeviceMetricsOverride", {"width":1440,"height":900,"deviceScaleFactor":1,"mobile":False})
        time.sleep(1)
        inv=send(ws,3,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const buttons = Array.from(document.querySelectorAll('button, a, [role=button]')).map(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) return null;
                const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0,80);
                if (!txt) return null;
                return {tag: el.tagName, txt, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
              }).filter(Boolean);
              return JSON.stringify({url: location.href, count: buttons.length, items: buttons.slice(0,80)});
            })()
            """, "returnByValue": True})
        print("inv:", inv.get("result",{}).get("result",{}).get("value",""))
        shot=send(ws,4,"Page.captureScreenshot",{"format":"png"})
        ts=int(time.time())
        out=f"/Users/hector/Projects/Dr.-strange/artifacts/flow_home_{ts}.png"
        with open(out,"wb") as f: f.write(base64.b64decode(shot.get("result",{}).get("data","")))
        print("screenshot=",out)
        return 0
    finally:
        ws.close()


if __name__=="__main__":
    raise SystemExit(main())
