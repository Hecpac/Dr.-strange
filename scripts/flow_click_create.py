"""End-to-end: switch model to Gemini Omni, upload avatar, set prompt, send.

Uses real CDP Input.dispatchMouseEvent for the model selector (JS .click()
didn't open the menu). Uses DOM.setFileInputFiles for the upload.
"""
from __future__ import annotations
import base64, json, time, urllib.request, os
import websocket

CDP="http://localhost:9250"
AVATAR="/Users/hector/Projects/Dr.-strange/assets/dr_strange_avatar_512.png"
PROMPT_TEXT="Cinematic 5-second hold, slow camera push-in on the subject. Preserve skull and orbital HUD identity intact. Subtle ember glow on HUD ring. Neutral cosmic background."

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

def shot(ws,nid,name):
    s=send(ws,nid,"Page.captureScreenshot",{"format":"png"})
    ts=int(time.time())
    out=f"/Users/hector/Projects/Dr.-strange/artifacts/flow_{name}_{ts}.png"
    with open(out,"wb") as f: f.write(base64.b64decode(s.get("result",{}).get("data","")))
    print(f"screenshot[{name}]={out}")
    return out

def mouse_click(ws,nid_a,nid_b,x,y):
    send(ws,nid_a,"Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1,"buttons":1})
    send(ws,nid_b,"Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1,"buttons":0})

def main():
    if not os.path.exists(AVATAR):
        print("ERR no avatar"); return 1
    tabs=http_get("/json/list")
    tab=next((t for t in tabs if t.get("type")=="page" and "labs.google/fx" in t.get("url","") and "flow" in t.get("url","")), None)
    if not tab: print("no flow tab"); return 1
    ws=websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
    counter=[100]
    def n():
        counter[0]+=1; return counter[0]
    try:
        send(ws,n(),"Page.enable")
        send(ws,n(),"DOM.enable")
        send(ws,n(),"Runtime.enable")
        send(ws,n(),"Emulation.setDeviceMetricsOverride",{"width":1440,"height":900,"deviceScaleFactor":1,"mobile":False})
        time.sleep(1)

        # STEP 1: Open model selector via real mouse coords
        coords=send(ws,n(),"Runtime.evaluate",{
            "expression":"""
            (() => {
              const b = Array.from(document.querySelectorAll('button')).find(b => /nano banana|gemini omni|veo/i.test(b.innerText.trim()));
              if (!b) return JSON.stringify({found:false});
              const r = b.getBoundingClientRect();
              return JSON.stringify({found:true, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), text: b.innerText.trim()});
            })()
            """, "returnByValue": True})
        cv=json.loads(coords.get("result",{}).get("result",{}).get("value","{}"))
        print("model_btn:", cv)
        if not cv.get("found"):
            print("ERR no model button"); return 1
        mouse_click(ws,n(),n(),cv["x"],cv["y"])
        time.sleep(2)
        shot(ws,n(),"model_menu")

        # Enumerate menu items
        menu=send(ws,n(),"Runtime.evaluate",{
            "expression":"""
            (() => {
              const all = Array.from(document.querySelectorAll('[role=menuitem], [role=option], li, button, div'));
              const items = all.filter(el => {
                if (el.offsetParent === null) return false;
                const txt = (el.innerText||'').trim();
                return txt.length > 0 && txt.length < 80 && /omni|veo|nano banana|imagen|video/i.test(txt);
              }).map(el => {
                const r = el.getBoundingClientRect();
                return {tag: el.tagName, txt: el.innerText.trim().slice(0,80), x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)};
              });
              return JSON.stringify(items.slice(0,30));
            })()
            """, "returnByValue": True})
        print("menu:", menu.get("result",{}).get("result",{}).get("value",""))

        # Try clicking Gemini Omni
        omni=send(ws,n(),"Runtime.evaluate",{
            "expression":"""
            (() => {
              const all = Array.from(document.querySelectorAll('*')).filter(el => el.offsetParent && /gemini omni/i.test((el.innerText||'').trim()) && el.innerText.trim().length < 80);
              if (!all.length) return JSON.stringify({found:false});
              all.sort((a,b) => a.innerText.length - b.innerText.length);
              const t = all[0];
              const r = t.getBoundingClientRect();
              return JSON.stringify({found:true, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), tag: t.tagName, txt: t.innerText.trim()});
            })()
            """, "returnByValue": True})
        ov=json.loads(omni.get("result",{}).get("result",{}).get("value","{}"))
        print("omni:", ov)
        if ov.get("found"):
            mouse_click(ws,n(),n(),ov["x"],ov["y"])
            time.sleep(2)
            shot(ws,n(),"after_omni")

        # STEP 2: Upload avatar — find file input directly
        node=send(ws,n(),"DOM.getDocument")
        root_id=node.get("result",{}).get("root",{}).get("nodeId")
        q=send(ws,n(),"DOM.querySelector",{"nodeId":root_id,"selector":"input[type=file]"})
        file_node_id=q.get("result",{}).get("nodeId",0)
        print("file_input_node_id:", file_node_id)
        if not file_node_id:
            # Click + button to surface file chooser, then try again
            addbtn=send(ws,n(),"Runtime.evaluate",{
                "expression":"""
                (() => {
                  const btns = Array.from(document.querySelectorAll('button'));
                  const top = btns.find(b => /añadir archivo multimedia|add media/i.test(b.innerText));
                  if (top) {
                    const r = top.getBoundingClientRect();
                    return JSON.stringify({found:true, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
                  }
                  return JSON.stringify({found:false});
                })()
                """, "returnByValue": True})
            av=json.loads(addbtn.get("result",{}).get("result",{}).get("value","{}"))
            print("add_btn:", av)
            if av.get("found"):
                mouse_click(ws,n(),n(),av["x"],av["y"])
                time.sleep(2)
                node2=send(ws,n(),"DOM.getDocument")
                root2=node2.get("result",{}).get("root",{}).get("nodeId")
                q2=send(ws,n(),"DOM.querySelector",{"nodeId":root2,"selector":"input[type=file]"})
                file_node_id=q2.get("result",{}).get("nodeId",0)
                print("file_node_id_retry:", file_node_id)

        if file_node_id:
            resp=send(ws,n(),"DOM.setFileInputFiles",{"files":[AVATAR],"nodeId":file_node_id})
            print("setFiles:", resp.get("result",{}), "err:", resp.get("error"))
            time.sleep(5)
            shot(ws,n(),"after_upload")
        else:
            shot(ws,n(),"no_file_input")
            print("WARN no file input found")

        # STEP 3: Type prompt into prompt textarea
        focus=send(ws,n(),"Runtime.evaluate",{
            "expression":"""
            (() => {
              const tas = Array.from(document.querySelectorAll('textarea, [contenteditable=true]'));
              const target = tas.find(el => {
                const r = el.getBoundingClientRect();
                return r.y > 700 && r.width > 100;
              }) || tas[tas.length-1];
              if (!target) return JSON.stringify({found:false, count: tas.length});
              target.focus();
              const r = target.getBoundingClientRect();
              return JSON.stringify({found:true, tag:target.tagName, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2)});
            })()
            """, "returnByValue": True})
        fv=json.loads(focus.get("result",{}).get("result",{}).get("value","{}"))
        print("prompt_focus:", fv)
        if fv.get("found"):
            mouse_click(ws,n(),n(),fv["x"],fv["y"])
            time.sleep(0.5)
            send(ws,n(),"Input.insertText",{"text":PROMPT_TEXT})
            time.sleep(1)
            shot(ws,n(),"after_prompt")

        # STEP 4: Click send
        sendbtn=send(ws,n(),"Runtime.evaluate",{
            "expression":"""
            (() => {
              const btns = Array.from(document.querySelectorAll('button'));
              // Find the arrow_forward / Crear send button at bottom right
              let target = null;
              for (const b of btns) {
                const r = b.getBoundingClientRect();
                if (r.y < 700) continue;
                if (/arrow_forward|crear|enviar|send/i.test(b.innerText) || b.getAttribute('aria-label') === 'Crear') {
                  if (!target || r.x > target.getBoundingClientRect().x) target = b;
                }
              }
              if (!target) return JSON.stringify({found:false});
              const r = target.getBoundingClientRect();
              return JSON.stringify({found:true, x: Math.round(r.x+r.width/2), y: Math.round(r.y+r.height/2), text: target.innerText.trim(), disabled: target.disabled});
            })()
            """, "returnByValue": True})
        sv=json.loads(sendbtn.get("result",{}).get("result",{}).get("value","{}"))
        print("send_btn:", sv)
        if sv.get("found") and not sv.get("disabled"):
            mouse_click(ws,n(),n(),sv["x"],sv["y"])
            time.sleep(6)
            shot(ws,n(),"after_send")
        else:
            shot(ws,n(),"send_blocked")
        return 0
    finally:
        ws.close()

if __name__=="__main__":
    raise SystemExit(main())
