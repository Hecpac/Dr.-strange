"""Pick 'Entrada de blog' report type + verify podcast generation."""
import json, time, os
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/fin_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # Find existing notebook tab if any
    page = None
    for p_existing in ctx.pages:
        if 'notebook/da8973d5' in p_existing.url:
            page = p_existing
            step('reused_existing_tab', url=p_existing.url)
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

    # Click "Entrada de blog" in the open modal
    clicked = False
    for sel in [
        'button:has-text("Entrada de blog")',
        '[role="button"]:has-text("Entrada de blog")',
        'div:has-text("Entrada de blog")',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click(timeout=4000)
                clicked = True
                step('clicked_entrada_blog', selector=sel)
                break
        except Exception:
            continue
    if not clicked:
        step('blog_button_not_visible')

    time.sleep(3)
    s1 = shot(page, "01_after_blog_click")
    step('after_blog_click', screenshot=s1)

    # Now the modal may show suggestion chips or input. Look for a "Generar"/"Crear" submit button.
    for sel in [
        'button:has-text("Generar")',
        'button:has-text("Crear")',
        'button:has-text("Generate")',
        'button[aria-label*="enerar"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000) and el.is_enabled(timeout=1500):
                el.click(timeout=4000)
                step('generate_blog_submitted', selector=sel)
                break
        except Exception:
            continue

    time.sleep(3)
    s2 = shot(page, "02_after_submit")
    step('after_submit', screenshot=s2)

    # Body text scan for both generation states
    try:
        body = page.locator('body').inner_text(timeout=5000)
        markers = {}
        for kw in ['Generando', 'Cargando', 'Resumen en audio', 'minutos', 'Audio', 'Informe', 'Entrada de blog', 'cargando']:
            cnt = body.lower().count(kw.lower())
            if cnt: markers[kw] = cnt
        step('generation_markers', markers=markers)
        step('body_sample', text=body[-1500:])
    except Exception as e:
        step('body_err', err=str(e))

    s3 = shot(page, "03_final")
    step('final', url=page.url, screenshot=s3)
    report['ok'] = True
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
