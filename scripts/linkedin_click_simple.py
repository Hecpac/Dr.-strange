"""Direct Playwright locator + filter for the Post button only."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/simple_{ts}_{label}.png"
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
        L('no_feed_tab')
        print(json.dumps({'log': log}))
        raise SystemExit(1)

    L('on_tab', url=page.url)

    # Wait for modal/composer to settle
    time.sleep(1)

    # Use locator.filter with has_text=exact and visibility check
    # LinkedIn's Post button has aria-label="Post" — much more reliable
    s0 = shot(page, "00_before")
    L('before', screenshot=s0)

    # Strategy 1: target by aria-label="Post"
    posted = False
    try:
        loc = page.locator('button[aria-label="Post"]:not([disabled])').filter(has_text=lambda _: True)
    except Exception:
        loc = page.locator('button[aria-label="Post"]')
    try:
        cnt = page.locator('button[aria-label="Post"]').count()
        L('aria_post_count', count=cnt)
        if cnt > 0:
            page.locator('button[aria-label="Post"]').first.click(force=True, timeout=5000)
            posted = True
            L('clicked_aria_post')
    except Exception as e:
        L('aria_post_err', err=str(e))

    # Strategy 2: scoped button:has-text("Post") with exact match
    if not posted:
        try:
            cnt = page.get_by_role("button", name="Post", exact=True).count()
            L('role_post_exact_count', count=cnt)
            if cnt > 0:
                page.get_by_role("button", name="Post", exact=True).first.click(force=True, timeout=5000)
                posted = True
                L('clicked_role_post_exact')
        except Exception as e:
            L('role_post_err', err=str(e))

    # Strategy 3: click bottom-right of modal by coordinate
    if not posted:
        # Modal is approximately centered in 1400x950 viewport
        # Post button is at bottom-right of modal, roughly (1010-1040, 470-510)
        try:
            page.mouse.click(1010, 482)
            L('coord_click_attempt_1', x=1010, y=482)
            time.sleep(2)
            # Check if modal closed
            modal_open = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
            if not modal_open:
                posted = True
                L('coord_click_succeeded')
            else:
                # Try another coord
                page.mouse.click(1020, 490)
                L('coord_click_attempt_2', x=1020, y=490)
                time.sleep(2)
                modal_open = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
                if not modal_open:
                    posted = True
                    L('coord_click_2_succeeded')
        except Exception as e:
            L('coord_click_err', err=str(e))

    time.sleep(6)
    s1 = shot(page, "01_after")
    L('after', url=page.url, screenshot=s1)

    # Verify
    modal_still = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
    body = page.locator('body').inner_text(timeout=4000)
    post_in_feed = 'I built an AI agent that runs my business 24/7' in body
    L('verify', modal_still=modal_still, post_in_feed=post_in_feed, posted_attempt=posted)
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e))

print(json.dumps({'log': log}, indent=2, ensure_ascii=False))
