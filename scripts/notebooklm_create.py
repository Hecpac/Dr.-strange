"""
Create NotebookLM notebook for 'Agent Skills 2026' research via Chrome CDP.
- Opens notebooklm.google.com (uses existing Google session in CDP profile)
- Verifies login state via screenshot
- Creates new notebook
- Activates Deep Research with curated prompt
- Submits and returns notebook URL
"""
import json, time, os
from playwright.sync_api import sync_playwright

ART_DIR = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
os.makedirs(ART_DIR, exist_ok=True)
ts = int(time.time())

DEEP_RESEARCH_PROMPT = (
    "Investiga el ecosistema de 'agent skill files', operating instructions y "
    "configuraciones canónicas que los practitioners de AI engineering están usando "
    "en 2025-2026. Cubre: (1) formatos oficiales — Claude Skills, sub-agents y plugins "
    "de Claude Code, .cursorrules de Cursor, CONVENTIONS.md de Aider, y formatos "
    "equivalentes de OpenAI Codex/Cowork; (2) filosofía del movimiento — el debate "
    "público sobre transparencia y disclosure de AI assistance en aplicaciones, "
    "incluyendo el argumento de Gordon DuQuesnay sobre por qué hiring AI Engineer y "
    "prohibir AI en aplicaciones es contradictorio; (3) base de research — paper SKILL0, "
    "estudios METR sobre time-horizon de agentes, paper Anthropic sobre sycophancy y "
    "pushback, Petri como herramienta de auditoría; (4) prácticas de practitioners — "
    "observaciones de Karpathy sobre LLM coding, Anthropic engineering blog "
    "(Building Effective Agents, multi-agent research system), AI-Native Software "
    "Engineering de Jeff Boggs. Prioriza fuentes primarias verificables: blogs "
    "oficiales de Anthropic/OpenAI, papers en arxiv, posts de practitioners con "
    "evidencia de implementación real. Excluye marketing de agencies y promesas "
    "vendor genéricas."
)

report = {'ts': ts, 'steps': []}

def step(name, **kw):
    entry = {'name': name, **kw}
    report['steps'].append(entry)
    return entry

def shot(page, label):
    path = f"{ART_DIR}/nlm_{ts}_{label}.png"
    try:
        page.screenshot(path=path, full_page=False)
        return path
    except Exception as e:
        return f"shot_err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    step('cdp_connect', ok=True, contexts=len(browser.contexts))

    if not browser.contexts:
        step('no_contexts', fatal=True)
        print(json.dumps(report, indent=2))
        raise SystemExit(1)

    context = browser.contexts[0]
    page = context.new_page()
    page.set_viewport_size({'width': 1280, 'height': 900})

    page.goto("https://notebooklm.google.com/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(3)
    s1 = shot(page, "01_home")
    step('goto_home', url=page.url, title=page.title(), screenshot=s1)

    # Check if we landed on Google sign-in page
    cur_url = page.url
    if 'accounts.google.com' in cur_url or 'signin' in cur_url.lower():
        step('login_required', url=cur_url, blocker=True)
        print(json.dumps(report, indent=2))
        raise SystemExit(2)

    # Try to find "Crear cuaderno nuevo" / "Create new notebook" button
    create_clicked = False
    selectors = [
        'button:has-text("Crear cuaderno nuevo")',
        'button:has-text("Crear nuevo")',
        'button:has-text("Create new notebook")',
        'button:has-text("Create new")',
        'button[aria-label*="rear"]',
        'button[aria-label*="reate"]',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click(timeout=5000)
                create_clicked = True
                step('create_button_clicked', selector=sel)
                break
        except Exception as e:
            continue

    if not create_clicked:
        s = shot(page, "02_no_create_button")
        step('create_button_not_found', screenshot=s, fatal=True)
        # Don't exit; dump report so user sees state
        print(json.dumps(report, indent=2))
        raise SystemExit(3)

    time.sleep(3)
    s2 = shot(page, "03_after_create_click")
    step('after_create_click', url=page.url, screenshot=s2)

    # Wait for modal mat-dialog-container
    try:
        page.locator('mat-dialog-container').first.wait_for(state='visible', timeout=15000)
        step('modal_visible', ok=True)
    except Exception as e:
        s = shot(page, "04_no_modal")
        step('modal_not_visible', err=str(e), screenshot=s)
        print(json.dumps(report, indent=2))
        raise SystemExit(4)

    dialog = page.locator('mat-dialog-container').first

    # Click "Fast Research" chip to open mode selector
    fr_clicked = False
    for sel in [
        'button:has-text("Fast Research")',
        'button:has-text("Investigación rápida")',
    ]:
        try:
            el = dialog.locator(sel).first
            if el.count() > 0:
                el.click(timeout=5000)
                fr_clicked = True
                step('fast_research_chip_clicked', selector=sel)
                break
        except Exception:
            continue

    if not fr_clicked:
        s = shot(page, "05_no_fr_chip")
        step('fast_research_chip_not_found', screenshot=s)
        # Continue anyway — maybe already in deep research mode

    time.sleep(1)

    # Click "Deep Research" in menu
    dr_clicked = False
    for sel in [
        'div[role="menu"] >> text="Deep Research"',
        '[role="option"]:has-text("Deep Research")',
        'text="Deep Research"',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                el.click(timeout=5000)
                dr_clicked = True
                step('deep_research_selected', selector=sel)
                break
        except Exception:
            continue

    time.sleep(2)
    s3 = shot(page, "06_after_dr_select")
    step('after_dr_select', screenshot=s3)

    # Locate the textarea (Deep Research mode uses textarea)
    target_input = None
    for sel in [
        'textarea[placeholder*="investigar"]',
        'textarea[placeholder*="research"]',
        'textarea',
        'input[placeholder*="Buscar fuentes"]',
        'input',
    ]:
        try:
            el = dialog.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                target_input = el
                step('input_located', selector=sel)
                break
        except Exception:
            continue

    if not target_input:
        s = shot(page, "07_no_input")
        step('input_not_found', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2))
        raise SystemExit(5)

    target_input.click()
    time.sleep(0.5)
    page.keyboard.type(DEEP_RESEARCH_PROMPT, delay=8)
    step('prompt_typed', chars=len(DEEP_RESEARCH_PROMPT))
    time.sleep(1)
    s4 = shot(page, "08_prompt_filled")
    step('prompt_filled', screenshot=s4)

    # Submit
    submit_clicked = False
    for sel in [
        'button[aria-label="Enviar"]',
        'button[aria-label="Submit"]',
        'button[type="submit"]',
    ]:
        try:
            el = dialog.locator(sel).first
            if el.count() > 0 and el.is_enabled(timeout=2000):
                el.click(timeout=5000)
                submit_clicked = True
                step('submit_clicked', selector=sel)
                break
        except Exception:
            continue

    if not submit_clicked:
        try:
            page.keyboard.press("Enter")
            step('submit_via_enter', ok=True)
            submit_clicked = True
        except Exception as e:
            step('submit_failed', err=str(e))

    time.sleep(4)
    s5 = shot(page, "09_after_submit")
    step('after_submit', url=page.url, screenshot=s5)

    # Wait briefly for URL change to notebook ID
    for _ in range(8):
        if '/notebook/' in page.url:
            break
        time.sleep(1)
    step('final_url', url=page.url)

    report['ok'] = True

except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2))
