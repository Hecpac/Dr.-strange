"""Click 'Importar' on NotebookLM cuaderno to import the 51 Deep Research sources."""
import json, time, os
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/import_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    page = ctx.new_page()
    page.set_viewport_size({'width': 1280, 'height': 900})
    page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

    # First close any modal that might be open (Agregar fuentes modal)
    closed = False
    for sel in ['button[aria-label="Cerrar"]', 'button[aria-label="Close"]', 'mat-dialog-container button[aria-label*="errar"]']:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click(timeout=3000)
                closed = True
                step('closed_modal', selector=sel)
                break
        except Exception:
            continue
    if not closed:
        # Try pressing Escape
        try:
            page.keyboard.press("Escape")
            step('pressed_escape')
        except Exception:
            pass

    time.sleep(2)
    s1 = shot(page, "01_initial")
    step('initial_state', url=page.url, screenshot=s1)

    # Find and click "Importar" — the primary button in the Fuentes panel
    imported = False
    for sel in [
        'button:has-text("Importar")',
        'button:has-text("+ Importar")',
        '[role="button"]:has-text("Importar")',
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            if count > 0:
                # Click the first visible one
                for i in range(count):
                    el = els.nth(i)
                    try:
                        if el.is_visible(timeout=1500):
                            el.click(timeout=5000)
                            imported = True
                            step('import_clicked', selector=sel, index=i)
                            break
                    except Exception:
                        continue
                if imported:
                    break
        except Exception:
            continue

    if not imported:
        s = shot(page, "02_no_import_button")
        step('import_button_not_found', screenshot=s)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    # Wait for import to process (usually 20-60s for 51 sources)
    for i in range(12):
        time.sleep(5)
        try:
            body = page.locator('body').inner_text(timeout=3000)
            if 'finalizó' not in body.lower() and 'planificando' not in body.lower():
                # Check if sources have been imported by counting "fuentes" count display
                pass
            # Quick check: count source list items
            try:
                source_items = page.locator('[class*="source-list"] [class*="source-item"]').count()
                step(f'check_{i}', source_items=source_items)
            except Exception:
                pass
        except Exception:
            pass
        # Detect terminal state
        try:
            if page.locator('text=fuentes importadas').count() > 0:
                step(f'import_complete_detected_at_{i*5}s')
                break
        except Exception:
            pass

    time.sleep(2)
    s2 = shot(page, "03_after_import")
    step('after_import', url=page.url, screenshot=s2)

    # Sample body text to verify
    try:
        body = page.locator('body').inner_text(timeout=5000)
        step('body_sample', text=body[:1200])
    except Exception as e:
        step('body_err', err=str(e))

    report['ok'] = True
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
