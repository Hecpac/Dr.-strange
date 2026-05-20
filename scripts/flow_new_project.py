"""Click Nuevo proyecto in Flow and screenshot the editor."""
from __future__ import annotations
import base64, json, time, urllib.request
import websocket

CDP_HTTP="http://localhost:9250"

def http_get(p):
    with urllib.request.urlopen(CDP_HTTP+p, timeout=5) as r:
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
    if not tab:
        print("no flow tab"); return 1
    ws=websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws,1,"Page.enable")
        send(ws,2,"Emulation.setDeviceMetricsOverride",{"width":1440,"height":900,"deviceScaleFactor":1,"mobile":False})
        time.sleep(1)
        click=send(ws,3,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const btns = Array.from(document.querySelectorAll('button'));
              const target = btns.find(b => /nuevo proyecto|new project/i.test(b.innerText.trim()));
              if (!target) return JSON.stringify({clicked:false, reason:'no_button'});
              target.scrollIntoView({block:'center'});
              target.click();
              return JSON.stringify({clicked:true, text: target.innerText.trim()});
            })()
            """, "returnByValue": True})
        print("click:", click.get("result",{}).get("result",{}).get("value",""))
        # Wait for editor SPA route
        time.sleep(8)
        info=send(ws,4,"Runtime.evaluate",{
            "expression":"JSON.stringify({url:location.href, title:document.title, bodyLen:document.body.innerText.length, snippet: document.body.innerText.slice(0,800)})",
            "returnByValue": True})
        print("post:", info.get("result",{}).get("result",{}).get("value",""))
        # Also enumerate prominent controls
        ctrls=send(ws,5,"Runtime.evaluate",{
            "expression":"""
            (() => {
              const items = Array.from(document.querySelectorAll('button, [role=button], textarea, input, select')).map(el => {
                const r = el.getBoundingClientRect();
                if (r.width<1||r.height<1) return null;
                const lbl = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.type || '').trim().slice(0,80);
                if (!lbl) return null;
                return {t: el.tagName, lbl, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
              }).filter(Boolean);
              return JSON.stringify({count: items.length, items: items.slice(0,60)});
            })()
            """, "returnByValue": True})
        print("ctrls:", ctrls.get("result",{}).get("result",{}).get("value",""))
        shot=send(ws,6,"Page.captureScreenshot",{"format":"png"})
        ts=int(time.time())
        out=f"/Users/hector/Projects/Dr.-strange/artifacts/flow_editor_{ts}.png"
        with open(out,"wb") as f: f.write(base64.b64decode(shot.get("result",{}).get("data","")))
        print("screenshot=",out)
        return 0
    finally:
        ws.close()

if __name__=="__main__":
    raise SystemExit(main())
