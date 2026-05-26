"""Trigger NotebookLM podcast (Resumen en audio) + report (Informes) on cuaderno da8973d5."""
import json, time, os
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/pr_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

def click_tile(page, names, label_key):
    """Try multiple selector patterns to click a Studio panel tile."""
    selectors = []
    for n in names:
        selectors.extend([
            f'button:has-text("{n}")',
            f'[role="button"]:has-text("{n}")',
            f'div[role="button"]:has-text("{n}")',
            f'mat-card:has-text("{n}")',
            f'text="{n}"',
        ])
    for sel in selectors:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1500):
                        el.click(timeout=4000)
                        step(f'clicked_{label_key}', selector=sel, index=i)
                        return True
                except Exception:
                    continue
        except Exception:
            continue
    step(f'no_match_{label_key}', tried_selectors=selectors[:6])
    return False

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_viewport_size({'width': 1400, 'height': 950})
    page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

    # Dismiss any modal / promo banner
    for sel in ['button[aria-label="Cerrar"]', 'button[aria-label="Close"]', 'button:has-text("Más tarde")']:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                step('dismissed_modal', selector=sel)
                break
        except Exception:
            continue
    time.sleep(1)
    s1 = shot(page, "01_landed")
    step('landed', url=page.url, screenshot=s1)

    # 1) Click "Resumen en audio" / "Generar" en panel Studio
    audio_clicked = click_tile(page,
        names=["Resumen en audio", "Generar audio", "Audio summary", "Audio overview", "Crear audio"],
        label_key="audio_primary")

    # If first tile click opened a generate sub-button, click "Generar"
    time.sleep(2)
    for sel in ['button:has-text("Generar")', 'button:has-text("Generate")', 'button[aria-label*="enerar"]']:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000) and el.is_enabled(timeout=1500):
                el.click(timeout=4000)
                step('generate_audio_clicked', selector=sel)
                break
        except Exception:
            continue
    time.sleep(2)
    s2 = shot(page, "02_after_audio")
    step('after_audio_trigger', screenshot=s2)

    # 2) Click "Informes" / Reports tile en panel Studio
    report_clicked = click_tile(page,
        names=["Informes", "Informe", "Reports", "Crear informe"],
        label_key="reports_primary")

    time.sleep(2)
    # If tile opened report-type selection, default to executive briefing / a sensible first option
    for sel in [
        'button:has-text("Resumen ejecutivo")',
        'button:has-text("Informe ejecutivo")',
        'button:has-text("Generar")',
        '[role="option"]:has-text("ejecutivo")',
        '[role="menuitem"]:has-text("ejecutivo")',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click(timeout=4000)
                step('generate_report_subselect', selector=sel)
                break
        except Exception:
            continue
    time.sleep(2)
    s3 = shot(page, "03_after_report")
    step('after_report_trigger', screenshot=s3)

    # Final verification: scan body text for generating-state hints
    try:
        body = page.locator('body').inner_text(timeout=5000)
        flags = {}
        for kw in ['Generando', 'Cargando', 'Cargand', 'Procesando', 'Resumen en audio', 'Informes', 'Generating', 'minutes', 'minutos']:
            flags[kw] = kw.lower() in body.lower()
        step('body_flags', flags=flags)
        step('body_sample', text=body[:1200])
    except Exception as e:
        step('body_err', err=str(e))

    s4 = shot(page, "04_final")
    step('final_state', screenshot=s4)

    report['ok'] = True
    report['audio_clicked'] = audio_clicked
    report['report_clicked'] = report_clicked
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
