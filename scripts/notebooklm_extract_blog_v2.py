"""Open the Blog Post tile in Studio panel and extract its full content."""
import json, time, os
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/ex2_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    page = None
    for p_existing in ctx.pages:
        if 'notebook/da8973d5' in p_existing.url:
            page = p_existing
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto(NOTEBOOK_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

    # Strategy: locate the Blog Post entry by combining title + "Blog Post" label
    # The tile contains the title AND a "Blog Post" subtitle
    # Use mat-card or any clickable wrapper

    clicked = False

    # First, try the studio panel "open" arrow / item
    # The tile structure usually is: button > div > h-something with title > subtitle "Blog Post"
    js_find_and_click = """
    () => {
        const candidates = Array.from(document.querySelectorAll('*'));
        for (const el of candidates) {
            const txt = (el.textContent || '').trim();
            if (txt.startsWith('Beyond the Prompt') && txt.length < 200 && txt.includes('Blog Post')) {
                // climb to nearest clickable
                let n = el;
                while (n && n.tagName !== 'BUTTON' && (n.getAttribute && n.getAttribute('role') !== 'button')) {
                    n = n.parentElement;
                    if (!n || n.tagName === 'BODY') break;
                }
                if (n && (n.tagName === 'BUTTON' || (n.getAttribute && n.getAttribute('role') === 'button'))) {
                    n.scrollIntoView({block:'center'});
                    n.click();
                    return 'clicked: '+n.tagName+' '+(n.getAttribute('role')||'');
                }
                // fallback: click the element itself
                el.scrollIntoView({block:'center'});
                el.click();
                return 'clicked-fallback: '+el.tagName;
            }
        }
        return 'not-found';
    }
    """
    try:
        result = page.evaluate(js_find_and_click)
        step('js_click_result', result=result)
        if 'clicked' in (result or ''):
            clicked = True
    except Exception as e:
        step('js_click_err', err=str(e))

    time.sleep(4)
    s1 = shot(page, "01_after_blog_tile_click")
    step('after_tile_click', screenshot=s1)

    # Now extract the opened blog content. The content typically renders in a modal
    # or replaces the chat panel
    extracted = ""
    extraction_sources = []

    # Try multiple containers that might hold the rendered blog
    for sel_name, sel in [
        ('article', 'article'),
        ('mat-dialog-container', 'mat-dialog-container'),
        ('.note-editor', '.note-editor'),
        ('[role="article"]', '[role="article"]'),
        ('div[class*="output"]', 'div[class*="output"]'),
        ('div[class*="note"]', 'div[class*="note"]'),
        ('div[class*="report"]', 'div[class*="report"]'),
        ('div[class*="document"]', 'div[class*="document"]'),
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(min(count, 5)):
                el = els.nth(i)
                try:
                    t = el.inner_text(timeout=2500)
                    if t and len(t) > len(extracted) and 'Beyond' in t and len(t) > 400:
                        extracted = t
                        extraction_sources.append({'sel': sel_name, 'i': i, 'len': len(t)})
                except Exception:
                    continue
        except Exception:
            continue

    # Fallback: deep body scan looking for the Beyond the Prompt section + 10k chars
    if len(extracted) < 1000:
        try:
            body = page.locator('body').inner_text(timeout=5000)
            idx = body.find('Beyond the Prompt')
            if idx >= 0:
                # Take a generous slice after the title
                candidate = body[idx:idx+20000]
                if len(candidate) > len(extracted):
                    extracted = candidate
                    extraction_sources.append({'sel': 'body_slice', 'len': len(candidate)})
        except Exception as e:
            step('body_err', err=str(e))

    step('extraction_attempts', sources=extraction_sources[:8], final_len=len(extracted))

    s2 = shot(page, "02_final")
    step('final', screenshot=s2)

    if extracted:
        out_md = f"{ART}/blog_beyond_v2_{ts}.md"
        with open(out_md, 'w', encoding='utf-8') as f:
            f.write(f"# Beyond the Prompt: 5 Surprising Realities of Building Professional Multi-Agent Systems\n\n")
            f.write(f"Notebook: Sistemas Multiagente y Arquitecturas de Memoria en IA\n")
            f.write(f"Notebook ID: da8973d5-546c-4c92-ba74-10c4daf80846\n")
            f.write(f"Extracted at: {time.ctime(ts)}\n\n")
            f.write(f"---\n\n")
            f.write(extracted)
        report['markdown_path'] = out_md
        report['extracted_full'] = extracted

    report['ok'] = True
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

# Print without truncating the full extraction
print(json.dumps({k: v for k, v in report.items() if k != 'extracted_full'}, indent=2, ensure_ascii=False))
print("\n\n=== EXTRACTED FULL ===\n")
print(report.get('extracted_full', '')[:15000])
