"""Open the auto-generated blog post in NotebookLM and extract its full text."""
import json, time, os, re
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/extract_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # Find existing notebook tab
    page = None
    for p_existing in ctx.pages:
        if 'notebook/da8973d5' in p_existing.url:
            page = p_existing
            step('reused_tab', url=p_existing.url)
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

    # Close any addSource modal
    if 'addSource' in page.url:
        for sel in ['button[aria-label="Cerrar"]', 'button[aria-label="Close"]']:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=1500):
                    el.click(timeout=3000)
                    step('closed_addsource_modal', selector=sel)
                    break
            except Exception:
                pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(2)

    # Click the blog post entry in the Studio panel
    title_fragments = [
        "Beyond the Prompt",
        "5 Surprising Realities",
        "Multi-Agent Systems",
    ]
    clicked = False
    for frag in title_fragments:
        for sel in [
            f'text="{frag}"',
            f'[role="button"]:has-text("{frag}")',
            f'button:has-text("{frag}")',
            f'div:has-text("{frag}")',
        ]:
            try:
                els = page.locator(sel)
                count = els.count()
                for i in range(count):
                    el = els.nth(i)
                    try:
                        if el.is_visible(timeout=1500):
                            el.click(timeout=4000)
                            clicked = True
                            step('blog_clicked', selector=sel, index=i)
                            break
                    except Exception:
                        continue
                if clicked: break
            except Exception:
                continue
        if clicked: break

    if not clicked:
        s = shot(page, "01_no_blog_button")
        step('blog_not_found', screenshot=s)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    time.sleep(4)
    s1 = shot(page, "02_blog_opened")
    step('blog_opened', screenshot=s1)

    # Try to expand if needed (some open in side panel, others in modal)
    # Look for fullscreen / expand buttons
    for sel in ['button[aria-label*="ampliar"]', 'button[aria-label*="xpand"]', 'button[aria-label*="ullscreen"]']:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                step('expanded_view', selector=sel)
                time.sleep(2)
                break
        except Exception:
            continue

    # Extract content from the studio output panel
    # NotebookLM renders Notes/reports inside specific containers
    extracted_text = ""
    content_selectors = [
        'div[class*="studio-output"]',
        'div[class*="note-content"]',
        'mat-dialog-container',
        'article',
        '[role="article"]',
        'div[contenteditable]',
        'div[class*="markdown"]',
    ]
    for sel in content_selectors:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    t = el.inner_text(timeout=3000)
                    if t and len(t) > len(extracted_text) and ('Beyond' in t or 'Multi-Agent' in t or 'AGENTS.md' in t):
                        extracted_text = t
                        step(f'extracted_from_{sel}', length=len(t), index=i)
                except Exception:
                    continue
        except Exception:
            continue

    # Fallback: full body
    if not extracted_text or len(extracted_text) < 500:
        try:
            body = page.locator('body').inner_text(timeout=5000)
            # Find the section around "Beyond the Prompt"
            idx = body.find('Beyond the Prompt')
            if idx >= 0:
                extracted_text = body[idx:idx+15000]
                step('extracted_from_body_at_title', length=len(extracted_text))
            else:
                extracted_text = body
                step('extracted_full_body', length=len(body))
        except Exception as e:
            step('body_extract_err', err=str(e))

    s2 = shot(page, "03_after_extract")
    step('after_extract', screenshot=s2, length=len(extracted_text))

    # Save markdown
    if extracted_text:
        out_md = f"{ART}/blog_beyond_the_prompt_{ts}.md"
        with open(out_md, 'w', encoding='utf-8') as f:
            f.write(f"# Blog Post — NotebookLM\n\n")
            f.write(f"Notebook: Sistemas Multiagente y Arquitecturas de Memoria en IA\n")
            f.write(f"Notebook ID: da8973d5-546c-4c92-ba74-10c4daf80846\n")
            f.write(f"Extracted at: {time.ctime(ts)}\n\n")
            f.write(f"---\n\n")
            f.write(extracted_text)
        step('saved_markdown', path=out_md, bytes=os.path.getsize(out_md))
        report['markdown_path'] = out_md
        report['extracted_text'] = extracted_text[:8000]  # cap for output

    report['ok'] = True
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
