"""Check current Flow project state — count images vs videos, look for any in-progress generation."""
from __future__ import annotations
import base64, json, time, urllib.request
import websocket

CDP="http://localhost:9250"

def http_get(p):
    with urllib.request.urlopen(CDP+p, timeout=5) as r:
        return json.loads(r.read())

def send(ws,i,m,p=None):
    pl={"id":i,"method":m}
    if p: pl["params"]=p
    ws.send(json.dumps(pl))
    while True:
        d=json.loads(ws.recv())
        if d.get("id")==i: return d

def main():
    tabs=http_get("/json/list")
    tab=next((t for t in tabs if t.get("type")=="page" and "labs.google/fx" in t.get("url","") and "flow" in t.get("url","")), None)
    if not tab: print("no flow tab"); return 1
    ws=websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    c=[100]
    def n(): c[0]+=1; return c[0]
    try:
        send(ws,n(),"Page.enable")
        send(ws,n(),"Emulation.setDeviceMetricsOverride",{"width":1440,"height":900,"deviceScaleFactor":1,"mobile":False})
        cur=send(ws,n(),"Runtime.evaluate",{"expression":"location.href","returnByValue":True})
        url=cur.get("result",{}).get("result",{}).get("value","")
        print("url:", url[:120])
        if "/edit/" in url:
            send(ws,n(),"Page.navigate",{"url": url.split("/edit/")[0]})
            time.sleep(4)
        time.sleep(2)
        # Enumerate gallery — count tiles, find any with video metadata or "Vídeo" label overlay
        inv=send(ws,n(),"Runtime.evaluate",{
            "expression":"""
            (() => {
              const imgs = Array.from(document.querySelectorAll('img')).filter(i => /media\\.getMediaUrlRedirect/.test(i.src));
              const tiles = imgs.map(i => {
                const id = (i.src.match(/name=([^&]+)/) || [])[1] || '';
                // Walk up to tile container to look for duration text overlay
                let parent = i.parentElement;
                let overlay = '';
                for (let d = 0; d < 5 && parent; d++) {
                  const t = (parent.innerText||'').trim();
                  const m = t.match(/Vídeo|V[ií]deo|\\d{1,2}:\\d{2}|generando|loading/i);
                  if (m && !overlay) overlay = t.slice(0,80);
                  parent = parent.parentElement;
                }
                const r = i.getBoundingClientRect();
                return {id: id.slice(0,40), overlay, w: Math.round(r.width), h: Math.round(r.height)};
              });
              const videos = Array.from(document.querySelectorAll('video')).map(v => ({src: v.src.slice(0,200), dur: v.duration}));
              const body = document.body.innerText;
              const inProgress = /generando|en cola|loading|\\d+%/i.test(body);
              return JSON.stringify({tileCount: tiles.length, tiles, videoElements: videos.length, inProgress, bodyExcerpt: body.slice(0,400)});
            })()
            """, "returnByValue": True})
        print("state:", inv.get("result",{}).get("result",{}).get("value",""))
        s=send(ws,n(),"Page.captureScreenshot",{"format":"png"})
        out=f"/Users/hector/Projects/Dr.-strange/artifacts/flow_status_check_{int(time.time())}.png"
        with open(out,"wb") as f: f.write(base64.b64decode(s.get("result",{}).get("data","")))
        print("shot=",out)
        return 0
    finally:
        ws.close()

if __name__=="__main__":
    raise SystemExit(main())
