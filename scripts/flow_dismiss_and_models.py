"""Dismiss Welcome overlay and open model selector to enumerate options."""
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
        # Dismiss welcome overlay
        dismiss=send(ws,3,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const btns = Array.from(document.querySelectorAll('button, a, [role=button]'));
              const target = btns.find(b => /see what'?s? possible|ver lo que es posible|explorar|comenzar|empezar|cerrar/i.test((b.innerText||b.getAttribute('aria-label')||'').trim()));
              if (!target) return JSON.stringify({clicked:false, btns: btns.map(b=>(b.innerText||b.getAttribute('aria-label')||'').trim()).slice(0,30)});
              target.click();
              return JSON.stringify({clicked:true, text:(target.innerText||target.getAttribute('aria-label')||'').trim()});
            })()
            """, "returnByValue": True})
        print("dismiss:", dismiss.get("result",{}).get("result",{}).get("value",""))
        time.sleep(2)
        # Open model selector — the "🍌 Nano Banana 2" button at ~898,843
        click_model=send(ws,4,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const btns = Array.from(document.querySelectorAll('button'));
              const target = btns.find(b => /nano banana|gemini omni|veo|modelo/i.test(b.innerText.trim()));
              if (!target) return JSON.stringify({clicked:false, reason:'no_model_btn'});
              target.click();
              return JSON.stringify({clicked:true, text: target.innerText.trim()});
            })()
            """, "returnByValue": True})
        print("model_open:", click_model.get("result",{}).get("result",{}).get("value",""))
        time.sleep(2)
        # Enumerate menu items
        opts=send(ws,5,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const items = Array.from(document.querySelectorAll('[role=menuitem], [role=option], li, button'));
              const rel = items.map(el => {
                const r = el.getBoundingClientRect();
                if (r.width<1||r.height<1) return null;
                const txt = (el.innerText||el.getAttribute('aria-label')||'').trim().slice(0,100);
                if (!txt) return null;
                if (!/nano banana|gemini|veo|omni|image|video|imagen|modelo/i.test(txt)) return null;
                return {t:el.tagName, role:el.getAttribute('role')||'', txt, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
              }).filter(Boolean);
              return JSON.stringify({count: rel.length, items: rel.slice(0,30)});
            })()
            """, "returnByValue": True})
        print("opts:", opts.get("result",{}).get("result",{}).get("value",""))
        shot=send(ws,6,"Page.captureScreenshot",{"format":"png"})
        ts=int(time.time())
        out=f"/Users/hector/Projects/Dr.-strange/artifacts/flow_models_{ts}.png"
        with open(out,"wb") as f: f.write(base64.b64decode(shot.get("result",{}).get("data","")))
        print("screenshot=",out)
        return 0
    finally:
        ws.close()

if __name__=="__main__":
    raise SystemExit(main())
