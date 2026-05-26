"""Just click the visible Post button — composer is already filled."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/post_{ts}_{label}.png"
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
        if 'linkedin.com/feed' in p_existing.url:
            page = p_existing
            break

    if not page:
        step('no_feed_tab', fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    s0 = shot(page, "00_before")
    step('before', url=page.url, screenshot=s0)

    # Find Post button — try various strategies
    js_find_post = """
    () => {
        // Find the share composer modal first
        const modal = document.querySelector('.share-box-modal, [class*="share-box"][class*="modal"], .artdeco-modal, [role="dialog"]');
        const root = modal || document.body;
        const all = Array.from(root.querySelectorAll('button'));
        const candidates = [];
        for (const b of all) {
            if (b.disabled) continue;
            const t = (b.innerText || b.textContent || '').trim();
            const rect = b.getBoundingClientRect();
            if (rect.width < 30 || rect.height < 20) continue;
            // Look for "Post" specifically — not "Post to Anyone" or longer
            if (t === 'Post' || t === 'Publicar') {
                candidates.push({
                    text: t,
                    x: rect.x + rect.width/2,
                    y: rect.y + rect.height/2,
                    w: rect.width,
                    h: rect.height,
                    cls: b.className.toString().slice(0, 80),
                    disabled: b.disabled,
                });
            }
        }
        return candidates;
    }
    """
    candidates = page.evaluate(js_find_post)
    step('post_candidates', candidates=candidates)

    if not candidates:
        # Broader fallback — any button with "Post" text
        js_broad = """
        () => {
            const all = Array.from(document.querySelectorAll('button'));
            const out = [];
            for (const b of all) {
                const t = (b.innerText || '').trim();
                const rect = b.getBoundingClientRect();
                if (t.toLowerCase().includes('post') && !b.disabled && rect.width > 40 && rect.width < 200 && rect.height > 25 && rect.height < 60) {
                    out.push({text: t, x: rect.x + rect.width/2, y: rect.y + rect.height/2, w: rect.width, h: rect.height});
                }
            }
            return out.slice(0, 5);
        }
        """
        candidates = page.evaluate(js_broad)
        step('post_broad_candidates', candidates=candidates)

    if candidates:
        # Click the last one (most likely the modal's primary Post button at bottom-right)
        c = candidates[-1]
        page.mouse.click(c['x'], c['y'])
        step('coord_click_post', x=c['x'], y=c['y'], text=c.get('text'))
    else:
        s = shot(page, "01_no_post_btn")
        step('no_post_button_found', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(2)

    time.sleep(8)
    s1 = shot(page, "02_after_post")
    step('after_post', url=page.url, screenshot=s1)

    # Verify success
    try:
        modal_still = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
        body = page.locator('body').inner_text(timeout=4000)
        toast_signals = {}
        for kw in ['Post successful', 'View post', 'Your post is now visible', 'visible to', 'Publicación exitosa', 'Tu publicación']:
            if kw.lower() in body.lower():
                toast_signals[kw] = True
        step('verify', modal_still=modal_still, toast_signals=toast_signals)
    except Exception as e:
        step('verify_err', err=str(e))

    # Try to find the published post link
    try:
        # Look for "Hector Pachano" + recency markers in feed
        body = page.locator('body').inner_text(timeout=4000)
        if 'I built an AI agent' in body or 'session amnesia' in body:
            step('post_visible_in_feed')
    except Exception:
        pass

    s2 = shot(page, "03_final_feed")
    step('final_feed', url=page.url, screenshot=s2)
    report['ok'] = True
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
