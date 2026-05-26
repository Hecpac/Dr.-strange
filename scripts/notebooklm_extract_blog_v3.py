"""Force-click the Blog Post card in Studio panel by coordinate + extract."""
import json, time, os
from playwright.sync_api import sync_playwright

NOTEBOOK_URL = "https://notebooklm.google.com/notebook/da8973d5-546c-4c92-ba74-10c4daf80846"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/notebooklm"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/ex3_{ts}_{label}.png"
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

    # Use JS to find the card element (not span) and click center of bounding box
    js = """
    () => {
        const candidates = Array.from(document.querySelectorAll('div, mat-card, article'));
        for (const el of candidates) {
            const txt = (el.textContent || '').trim();
            // Looking for the wrapper containing "Beyond the Prompt" + "Blog Post" but small enough to be the card
            if (txt.includes('Beyond the Prompt') && txt.includes('Blog Post') && txt.length < 250) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 100 && rect.height > 30) {
                    return {
                        tag: el.tagName,
                        cls: el.className,
                        x: rect.x + rect.width/2,
                        y: rect.y + rect.height/2,
                        w: rect.width,
                        h: rect.height,
                        text_len: txt.length
                    };
                }
            }
        }
        return null;
    }
    """
    info = page.evaluate(js)
    step('card_search', info=info)

    if info:
        try:
            page.mouse.click(info['x'], info['y'])
            step('coord_click', x=info['x'], y=info['y'])
        except Exception as e:
            step('coord_click_err', err=str(e))

    time.sleep(4)
    s1 = shot(page, "01_after_coord_click")
    step('after_click', screenshot=s1)

    # Now look for the opened note content. It may be a fullscreen / modal / replaces panel
    js_extract = """
    () => {
        // Search for any large container that has the blog title + lots of content
        const candidates = Array.from(document.querySelectorAll('div, article, mat-dialog-container'));
        let best = null;
        for (const el of candidates) {
            const txt = (el.textContent || '').trim();
            if (txt.startsWith('Beyond the Prompt') || (txt.includes('Beyond the Prompt') && txt.length > 1500)) {
                if (!best || txt.length > best.txt.length) {
                    best = { tag: el.tagName, cls: el.className.toString().slice(0,60), txt: txt };
                }
            }
        }
        return best ? { tag: best.tag, cls: best.cls, length: best.txt.length, text: best.txt.slice(0, 30000) } : null;
    }
    """
    result = page.evaluate(js_extract)
    step('extract_result', meta={'tag': result.get('tag') if result else None,
                                 'cls': result.get('cls') if result else None,
                                 'length': result.get('length') if result else 0})

    s2 = shot(page, "02_final")
    step('final', screenshot=s2)

    extracted = result.get('text', '') if result else ''
    if extracted and len(extracted) > 1000:
        out_md = f"{ART}/blog_beyond_v3_{ts}.md"
        with open(out_md, 'w', encoding='utf-8') as f:
            f.write(extracted)
        report['markdown_path'] = out_md
        report['ok'] = True
    else:
        report['ok'] = False
        report['reason'] = 'extracted_too_short'

    print(json.dumps({k:v for k,v in report.items() if k != 'extracted_full'}, indent=2, ensure_ascii=False))
    print("\n\n=== EXTRACTED ===\n")
    print(extracted[:20000] if extracted else '(empty)')

except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    print(json.dumps(report, indent=2, ensure_ascii=False))
