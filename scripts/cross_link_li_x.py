"""Cross-link LinkedIn post + X thread via comments.
Step 1: Find LinkedIn post URL from Hector's profile recent activity.
Step 2: Find X thread URL from @HectorPach71777 profile.
Step 3: Add comment on LinkedIn linking to X.
Step 4: Reply to X thread linking to LinkedIn.
"""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/crosslink"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/xl_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # ============================================================
    # STEP 1: Find LinkedIn post URL
    # ============================================================
    li_page = None
    for p_existing in ctx.pages:
        if 'linkedin.com' in p_existing.url:
            li_page = p_existing
            break
    if not li_page:
        li_page = ctx.new_page()
        li_page.set_viewport_size({'width': 1400, 'height': 950})

    li_page.goto("https://www.linkedin.com/in/me/recent-activity/all/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s1 = shot(li_page, "01_li_activity")
    L('li_activity', url=li_page.url, screenshot=s1)

    # Find URL of most recent post — look for first urn:li:activity in href
    li_post_url = None
    try:
        js = """
        () => {
            const links = Array.from(document.querySelectorAll('a[href*="activity-"], a[href*="urn:li:activity"]'));
            for (const a of links) {
                const href = a.href || '';
                if (href.includes('/feed/update/') || href.includes('activity-')) {
                    return href;
                }
            }
            // Try by clicking on the "..." menu and looking for Copy link button
            return null;
        }
        """
        li_post_url = li_page.evaluate(js)
        L('li_post_url_found', url=li_post_url)
    except Exception as e:
        L('li_url_extraction_err', err=str(e))

    if not li_post_url:
        # Fallback: click first post header timestamp
        try:
            # Look for first post link
            js2 = """
            () => {
                const all = Array.from(document.querySelectorAll('a'));
                for (const a of all) {
                    const h = a.href || '';
                    if (h.match(/feed\\/update.*urn.*activity/i) || h.match(/posts\\/.*activity-\\d+/i)) {
                        return h;
                    }
                }
                return null;
            }
            """
            li_post_url = li_page.evaluate(js2)
            L('li_post_url_fallback', url=li_post_url)
        except Exception as e:
            L('li_fallback_err', err=str(e))

    # ============================================================
    # STEP 2: Find X thread URL
    # ============================================================
    x_page = None
    for p_existing in ctx.pages:
        if 'x.com' in p_existing.url and 'home' in p_existing.url:
            x_page = p_existing
            break
    if not x_page:
        x_page = ctx.new_page()
        x_page.set_viewport_size({'width': 1400, 'height': 950})

    x_page.goto("https://x.com/HectorPach71777", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s2 = shot(x_page, "02_x_profile")
    L('x_profile', url=x_page.url, screenshot=s2)

    # Find first thread tweet status URL
    x_thread_url = None
    try:
        js_x = """
        () => {
            const links = Array.from(document.querySelectorAll('a[href*="/status/"]'));
            for (const a of links) {
                const h = a.href || '';
                if (h.match(/\\/HectorPach71777\\/status\\/\\d+/i)) {
                    return h.split('?')[0]; // strip query
                }
            }
            return null;
        }
        """
        x_thread_url = x_page.evaluate(js_x)
        L('x_thread_url_found', url=x_thread_url)
    except Exception as e:
        L('x_url_extraction_err', err=str(e))

    if not li_post_url or not x_thread_url:
        L('missing_urls', li=li_post_url, x=x_thread_url, fatal=True)
        print(json.dumps({'log': log}, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    # ============================================================
    # STEP 3: Add comment on LinkedIn linking to X
    # ============================================================
    # Navigate to LinkedIn post permalink
    li_page.goto(li_post_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s3 = shot(li_page, "03_li_post_open")
    L('li_post_open', url=li_page.url, screenshot=s3)

    li_comment_text = f"Also dropped this as a thread on X for that audience — {x_thread_url}"

    # Click comment textbox
    li_commented = False
    for sel in [
        'div[contenteditable="true"][role="textbox"]',
        'div.ql-editor[contenteditable="true"]',
        '[data-test-comment-text-editor] div[contenteditable]',
    ]:
        try:
            el = li_page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=3000):
                el.click(timeout=4000)
                time.sleep(0.6)
                li_page.keyboard.insert_text(li_comment_text)
                L('li_comment_typed', chars=len(li_comment_text), selector=sel)
                li_commented = True
                break
        except Exception:
            continue

    if li_commented:
        time.sleep(1.5)
        # Submit comment — usually Cmd+Enter on Mac, or click Comment button
        try:
            li_page.keyboard.press('Meta+Enter')
            L('li_comment_submitted_via_cmd_enter')
        except Exception:
            # Fallback: click Comment button
            for sel in ['button:has-text("Comment")', 'button:has-text("Comentar")', 'button[type="submit"]']:
                try:
                    el = li_page.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=2000) and el.is_enabled(timeout=1500):
                        el.click(timeout=4000)
                        L('li_comment_submitted_via_button', selector=sel)
                        break
                except Exception:
                    continue
        time.sleep(5)
        s4 = shot(li_page, "04_li_after_comment")
        L('li_after_comment', screenshot=s4)
    else:
        s4 = shot(li_page, "04_li_no_comment_box")
        L('li_comment_box_not_found', screenshot=s4)

    # ============================================================
    # STEP 4: Reply to X thread linking to LinkedIn
    # ============================================================
    x_page.goto(x_thread_url, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s5 = shot(x_page, "05_x_thread_open")
    L('x_thread_open', url=x_page.url, screenshot=s5)

    x_reply_text = f"Full long-form version on LinkedIn → {li_post_url}"

    # Click reply textbox (on X, replying to a tweet uses the inline reply form)
    x_replied = False
    for sel in [
        '[data-testid="tweetTextarea_0"]',
        'div[contenteditable="true"][role="textbox"]',
    ]:
        try:
            el = x_page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=3000):
                el.click(timeout=4000)
                time.sleep(0.5)
                x_page.keyboard.insert_text(x_reply_text)
                L('x_reply_typed', chars=len(x_reply_text), selector=sel)
                x_replied = True
                break
        except Exception:
            continue

    if x_replied:
        time.sleep(1.5)
        # Submit reply — X uses Cmd+Enter or click Reply button
        try:
            x_page.keyboard.press('Meta+Enter')
            L('x_reply_submitted_via_cmd_enter')
        except Exception:
            for sel in ['button[data-testid="tweetButtonInline"]', 'button[data-testid="tweetButton"]', 'button:has-text("Reply")', 'button:has-text("Post")']:
                try:
                    el = x_page.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=2000) and el.is_enabled(timeout=1500):
                        el.click(timeout=4000)
                        L('x_reply_submitted_via_button', selector=sel)
                        break
                except Exception:
                    continue
        time.sleep(5)
        s6 = shot(x_page, "06_x_after_reply")
        L('x_after_reply', screenshot=s6)
    else:
        s6 = shot(x_page, "06_x_no_reply_box")
        L('x_reply_box_not_found', screenshot=s6)

    print(json.dumps({'log': log, 'li_url': li_post_url, 'x_url': x_thread_url, 'ok': True}, indent=2, ensure_ascii=False))
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e), type=type(e).__name__)
    print(json.dumps({'log': log, 'ok': False}, indent=2, ensure_ascii=False))
