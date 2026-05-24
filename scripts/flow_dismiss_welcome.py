"""Aggressively dismiss the Welcome overlay."""
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
    try:
        send(ws,1,"Page.enable")
        send(ws,2,"Emulation.setDeviceMetricsOverride",{"width":1440,"height":900,"deviceScaleFactor":1,"mobile":False})
        time.sleep(1)
        # Try multiple strategies
        result=send(ws,3,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const findAll = sel => Array.from(document.querySelectorAll(sel));
              // Strategy 1: look for any element containing "See what's" text in welcome overlay
              const candidates = findAll('*').filter(el => {
                const t = (el.innerText||'').trim();
                return /^see what'?s? (new|possible)|ver lo que es (nuevo|posible)/i.test(t) && el.offsetParent !== null;
              });
              const log = [];
              for (const c of candidates) {
                // Walk up to find clickable ancestor
                let el = c;
                let depth = 0;
                while (el && depth < 5) {
                  if (el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role') === 'button' || el.onclick) {
                    el.click();
                    log.push({clicked: el.tagName, text: (el.innerText||'').trim().slice(0,50)});
                    break;
                  }
                  el = el.parentElement;
                  depth++;
                }
                if (!log.length) { c.click(); log.push({clicked: c.tagName, text: (c.innerText||'').trim().slice(0,50), fallback: true}); }
              }
              // Strategy 2: also look for close icon
              const closes = findAll('[aria-label*="close" i], [aria-label*="cerrar" i], [aria-label*="dismiss" i]');
              for (const c of closes.slice(0,2)) { c.click(); log.push({closed: c.getAttribute('aria-label')}); }
              return JSON.stringify({log, candidates: candidates.length});
            })()
            """, "returnByValue": True})
        print("dismiss1:", result.get("result",{}).get("result",{}).get("value",""))
        time.sleep(2)
        # Strategy 3: dispatch Escape key
        send(ws,4,"Input.dispatchKeyEvent",{"type":"keyDown","key":"Escape","windowsVirtualKeyCode":27})
        send(ws,5,"Input.dispatchKeyEvent",{"type":"keyUp","key":"Escape","windowsVirtualKeyCode":27})
        time.sleep(1)
        # Check if welcome overlay still present
        check=send(ws,6,"Runtime.evaluate",{
            "expression":"""
            JSON.stringify({
              welcomeVisible: !!Array.from(document.querySelectorAll('*')).find(el => /welcome to google flow/i.test(el.innerText||'') && el.offsetParent !== null),
              bodyLen: document.body.innerText.length,
              snippet: document.body.innerText.slice(0,300)
            })
            """, "returnByValue": True})
        print("check:", check.get("result",{}).get("result",{}).get("value",""))
        shot=send(ws,7,"Page.captureScreenshot",{"format":"png","captureBeyondViewport":False})
        ts=int(time.time())
        out=f"/Users/hector/Projects/Dr.-strange/artifacts/flow_dismiss_{ts}.png"
        with open(out,"wb") as f: f.write(base64.b64decode(shot.get("result",{}).get("data","")))
        print("screenshot=",out)
        return 0
    finally:
        ws.close()

if __name__=="__main__":
    raise SystemExit(main())
