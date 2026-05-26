"""Final attempt — use Playwright's get_by_role with exact Post name."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/final_{ts}_{label}.png"
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
        print(json.dumps(report))
        raise SystemExit(1)

    step('on_tab', url=page.url)

    # Inspect ALL buttons inside the modal to understand DOM structure
    js_inspect = """
    () => {
        // Find modal
        const modal = document.querySelector('div[role="dialog"]');
        if (!modal) return {error: 'no modal'};
        const buttons = Array.from(modal.querySelectorAll('button'));
        return buttons.map(b => {
            const rect = b.getBoundingClientRect();
            return {
                text: (b.innerText || '').trim().slice(0, 60),
                aria: b.getAttribute('aria-label') || '',
                type: b.getAttribute('type') || '',
                cls: b.className.toString().slice(0, 100),
                disabled: b.disabled,
                visible: rect.width > 0 && rect.height > 0,
                x: rect.x + rect.width/2,
                y: rect.y + rect.height/2,
                w: rect.width,
                h: rect.height,
            };
        });
    }
    """
    inspect = page.evaluate(js_inspect)
    step('modal_buttons', buttons=inspect[:30] if isinstance(inspect, list) else inspect)

    # Click the LAST enabled visible button with rect (likely the Post button at bottom-right)
    posted = False
    if isinstance(inspect, list):
        # Find candidates: enabled, visible, has "Post" or "Publicar" in text/aria
        candidates = []
        for b in inspect:
            if b.get('disabled'): continue
            if not b.get('visible'): continue
            txt = (b.get('text') or '').lower()
            aria = (b.get('aria') or '').lower()
            # Primary Post button: exact text Post/Publicar OR aria with same
            if txt in ('post', 'publicar') or aria in ('post', 'publicar') or 'post' in aria and len(aria) < 12:
                candidates.append(b)
        step('post_candidates_refined', candidates=candidates)

        if candidates:
            c = candidates[-1]  # bottom-right Post button
            page.mouse.click(c['x'], c['y'])
            step('clicked_post', x=c['x'], y=c['y'], text=c.get('text'), aria=c.get('aria'))
            posted = True

    if not posted:
        # Last resort: use playwright's get_by_role and force=true
        try:
            page.get_by_role("button", name="Post", exact=True).click(force=True, timeout=5000)
            step('clicked_via_get_by_role', name='Post')
            posted = True
        except Exception as e:
            step('get_by_role_err', err=str(e))

    if not posted:
        s = shot(page, "01_failed")
        step('all_strategies_failed', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(2)

    time.sleep(8)
    s1 = shot(page, "02_after_post")
    step('after_post', url=page.url, screenshot=s1)

    # Verify
    modal_still = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
    body = page.locator('body').inner_text(timeout=4000)
    success_markers = {}
    for kw in ['View post', 'Post successful', 'Your post is now visible', 'Tu publicación', 'Publicación']:
        if kw.lower() in body.lower():
            success_markers[kw] = True
    # Also check if post text now appears in feed
    post_in_feed = 'I built an AI agent that runs my business 24/7' in body
    step('verify', modal_still=modal_still, success_markers=success_markers, post_in_feed=post_in_feed)

    s2 = shot(page, "03_final")
    step('final', url=page.url, screenshot=s2)
    report['ok'] = True
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
