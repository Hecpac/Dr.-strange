"""Verify NotebookLM cuaderno da8973d5 still exists + check status of Deep Research."""
import json, time, os
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
HOME_URL = "https://notebooklm.google.com/"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/verify_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # First load notebook directly
    page = ctx.new_page()
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s1 = shot(page, "01_notebook_direct")
    step('notebook_direct', url=page.url, title=page.title(), screenshot=s1)

    # Look for research state text
    state_text = ""
    for sel in ['text=Planificando', 'text=Investigando', 'text=Listo', 'text=Importar',
                'mat-dialog-container', '[class*="research"]', '[class*="source"]']:
        try:
            count = page.locator(sel).count()
            if count > 0:
                state_text += f" | {sel}={count}"
        except Exception:
            pass
    step('state_markers', text=state_text)

    # Body text scan for status keywords
    try:
        body = page.locator('body').inner_text(timeout=5000)
        snippets = []
        for kw in ['Planificando', 'Investigando', 'Listo', 'Importar', 'investigación', 'Untitled', 'fuentes']:
            if kw.lower() in body.lower():
                idx = body.lower().find(kw.lower())
                snippets.append({kw: body[max(0,idx-30):idx+100]})
        step('body_snippets', snippets=snippets[:10])
    except Exception as e:
        step('body_scan_err', err=str(e))

    # Now also load home to see if the notebook appears in the list
    page2 = ctx.new_page()
    page2.set_viewport_size({'width': 1280, 'height': 900})
    page2.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    s2 = shot(page2, "02_home")
    step('home_loaded', url=page2.url, title=page2.title(), screenshot=s2)

    # Look for notebook titles
    try:
        body2 = page2.locator('body').inner_text(timeout=5000)
        if 'Untitled' in body2 or 'agent' in body2.lower() or 'skill' in body2.lower():
            step('home_has_match', untitled='Untitled' in body2, agent='agent' in body2.lower(), skill='skill' in body2.lower())
        # Sample first 800 chars
        step('home_text_sample', text=body2[:800])
    except Exception as e:
        step('home_body_err', err=str(e))

    report['ok'] = True
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
