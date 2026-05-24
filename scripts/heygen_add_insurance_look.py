"""Drive HeyGen Photo Avatar IV "Add a look" via Chrome CDP.

Strategy:
- Connect to the open HeyGen avatar-group tab.
- Find the "Add a look" button by visible text.
- Click it.
- Wait for the prompt dialog, find the textarea/input, type the brand-aligned prompt.
- Find and click Generate/Create.
- Screenshot at each waypoint into artifacts/heygen/look_step_N.png.
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

LOOK_PROMPT = (
    "Latina woman insurance agent, mid 30s, long dark hair, warm confident smile, "
    "wearing a tailored cream beige blazer over a navy blouse, seated at a modern "
    "warmly-lit office desk with a subtle Texas / Houston skyline through a soft "
    "window, professional cinematic portrait, brand palette of navy and terracotta "
    "accents, looking directly at camera"
)


def http_get(p):
    with urllib.request.urlopen(CDP_HTTP + p, timeout=5) as r:
        return json.loads(r.read())


def find_heygen_tab():
    tabs = http_get("/json/list")
    for t in tabs:
        if t.get("type") == "page" and "app.heygen.com/avatar" in (t.get("url") or ""):
            return t
    raise SystemExit("no_heygen_avatar_tab_open")


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
    path = f"{ART}/look_step_{name}.png"
    if b64:
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
    return path


def evaluate(ws, mid, expr):
    r = send(ws, mid, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return r.get("result", {}).get("result", {}).get("value")


def main():
    tab = find_heygen_tab()
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
    try:
        send(ws, 1, "Page.enable")
        send(ws, 2, "Runtime.enable")
        send(ws, 3, "DOM.enable")
        time.sleep(2)
        shot(ws, 10, "0_initial")

        # Find Add-a-look tile and click. Use a robust JS search by text+role.
        click_js = r"""
        (() => {
          // Strategy: find any element whose visible text equals or includes "Add a look".
          const all = Array.from(document.querySelectorAll('div, button, a, span'));
          const candidates = all.filter(el => {
            const t = (el.innerText || '').trim();
            return t === 'Add a look' || t === 'Add a Look' || t === 'Add new look';
          });
          if (!candidates.length) return JSON.stringify({ok:false, reason:'no_candidate'});
          // Pick the smallest one (the actual tile/button, not a parent)
          candidates.sort((a,b) => (a.innerText.length - b.innerText.length));
          const target = candidates[0];
          // Walk up to find a clickable parent if needed
          let click_el = target;
          for (let i=0; i<3 && click_el && click_el.tagName !== 'BUTTON' && click_el.tagName !== 'A'; i++) {
            if (click_el.parentElement && (click_el.parentElement.onclick || click_el.parentElement.getAttribute('role') === 'button')) {
              click_el = click_el.parentElement;
              break;
            }
            click_el = click_el.parentElement || click_el;
          }
          const r = click_el.getBoundingClientRect();
          click_el.scrollIntoView({block:'center'});
          click_el.click();
          return JSON.stringify({ok:true, rect:{x:r.x,y:r.y,w:r.width,h:r.height}, tag:click_el.tagName, text:(click_el.innerText||'').slice(0,80)});
        })()
        """
        out = evaluate(ws, 20, click_js)
        print("click_result:", out)
        time.sleep(3)
        shot(ws, 21, "1_after_click")

        # Try to find prompt input. HeyGen typically opens a modal/dialog with a textarea or contenteditable.
        find_input_js = r"""
        (() => {
          // Look for textareas or contenteditables visible
          const els = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input[type="text"]'));
          const visible = els.filter(el => {
            const r = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return r.width > 50 && r.height > 20 && style.visibility !== 'hidden' && style.display !== 'none';
          });
          if (!visible.length) return JSON.stringify({ok:false, reason:'no_input'});
          // Prefer the largest one (probably the prompt area)
          visible.sort((a,b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height));
          const t = visible[0];
          const r = t.getBoundingClientRect();
          return JSON.stringify({ok:true, tag:t.tagName, placeholder:t.placeholder||'', rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
        })()
        """
        out = evaluate(ws, 30, find_input_js)
        print("input_found:", out)

        # Fill the input via Element.value + dispatch input event
        fill_js = (
            "(() => {"
            "const els = Array.from(document.querySelectorAll('textarea, [contenteditable=\"true\"], input[type=\"text\"]'));"
            "const visible = els.filter(el => { const r = el.getBoundingClientRect(); const s=getComputedStyle(el); return r.width>50 && r.height>20 && s.visibility!=='hidden' && s.display!=='none'; });"
            "if(!visible.length) return JSON.stringify({ok:false});"
            "visible.sort((a,b)=>(b.getBoundingClientRect().width*b.getBoundingClientRect().height)-(a.getBoundingClientRect().width*a.getBoundingClientRect().height));"
            "const t = visible[0]; t.focus();"
            f"const txt = {json.dumps(LOOK_PROMPT)};"
            "if (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT') {"
            "  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value') || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');"
            "  setter.set.call(t, txt);"
            "  t.dispatchEvent(new Event('input', {bubbles:true}));"
            "  t.dispatchEvent(new Event('change', {bubbles:true}));"
            "} else {"
            "  t.innerText = txt;"
            "  t.dispatchEvent(new InputEvent('input', {bubbles:true, data:txt}));"
            "}"
            "return JSON.stringify({ok:true, value: t.value || t.innerText});"
            "})()"
        )
        out = evaluate(ws, 40, fill_js)
        print("fill_result:", out[:300] if out else out)
        time.sleep(1)
        shot(ws, 41, "2_after_fill")

        # Click Generate/Create button. Look for buttons by text.
        gen_js = r"""
        (() => {
          const btns = Array.from(document.querySelectorAll('button'));
          const candidates = btns.filter(b => /generate|create|submit|done|next|continue|crear|generar/i.test((b.innerText||'').trim()));
          if (!candidates.length) return JSON.stringify({ok:false, reason:'no_generate_button', sample: btns.slice(0,15).map(b => (b.innerText||'').trim()).filter(Boolean)});
          // Pick the most prominent (largest, primary-styled, last one — typically rightmost CTA)
          candidates.sort((a,b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            return (rb.width*rb.height) - (ra.width*ra.height);
          });
          const t = candidates[0];
          t.scrollIntoView({block:'center'});
          t.click();
          return JSON.stringify({ok:true, text:(t.innerText||'').trim(), all_candidates: candidates.map(b=>(b.innerText||'').trim())});
        })()
        """
        out = evaluate(ws, 50, gen_js)
        print("generate_result:", out)
        time.sleep(5)
        shot(ws, 51, "3_after_generate")
        return 0
    finally:
        ws.close()


if __name__ == "__main__":
    sys.exit(main())
