"""V2: Get LinkedIn post URL by clicking timestamp on V1 post, then cross-link."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/crosslink"
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/v2_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

X_THREAD_URL = "https://x.com/HectorPach71777/status/2057914349990121964"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # ============================================================
    # STEP 1: Get LinkedIn post URL by clicking timestamp on V1 post
    # ============================================================
    li_page = None
    for p in ctx.pages:
        if 'linkedin.com' in p.url:
            li_page = p
            break
    if not li_page:
        li_page = ctx.new_page()
        li_page.set_viewport_size({'width': 1400, 'height': 950})

    li_page.goto("https://www.linkedin.com/in/me/recent-activity/all/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

    # Use JS to find the post containing "I built an AI agent" and extract its URN-based URL
    js_find_post_url = """
    () => {
        const target_text = 'I built an AI agent that runs my business';
        const allElements = Array.from(document.querySelectorAll('div, article, section'));
        for (const el of allElements) {
            const t = (el.textContent || '');
            if (t.includes(target_text)) {
                // Find a link inside this element that has the urn:li:activity pattern
                const link = el.querySelector('a[href*="/feed/update/urn"], a[href*="activity-"]');
                if (link) return link.href;
                // Find any element with data-urn
                const urnEl = el.closest('[data-urn]') || el.querySelector('[data-urn]');
                if (urnEl) {
                    const urn = urnEl.getAttribute('data-urn');
                    if (urn) return 'https://www.linkedin.com/feed/update/' + urn + '/';
                }
                // Find timestamp link
                const tsLink = el.querySelector('a[href*="/posts/"]');
                if (tsLink) return tsLink.href;
            }
        }
        return null;
    }
    """
    li_url = li_page.evaluate(js_find_post_url)
    L('li_url_via_js', url=li_url)

    if not li_url:
        # Alternative: click the "..." menu of first V1 post, click "Copy link to post"
        L('trying_menu_approach')
        # Find the "..." menu next to first post
        js_click_menu = """
        () => {
            const target_text = 'I built an AI agent that runs my business';
            const articles = Array.from(document.querySelectorAll('article, [class*="feed-shared-update"]'));
            for (const a of articles) {
                if ((a.textContent || '').includes(target_text)) {
                    const menuBtn = a.querySelector('button[aria-label*="More"], button[aria-label*="Más"]');
                    if (menuBtn) {
                        menuBtn.click();
                        return true;
                    }
                }
            }
            return false;
        }
        """
        clicked = li_page.evaluate(js_click_menu)
        L('menu_click_result', clicked=clicked)
        time.sleep(2)
        s = shot(li_page, "00_after_menu_click")
        L('after_menu_click', screenshot=s)

        # Look for "Copy link to post" option
        for sel in [
            '[role="menuitem"]:has-text("Copy link")',
            '[role="menuitem"]:has-text("Copiar enlace")',
            'button:has-text("Copy link to post")',
            'button:has-text("Copiar enlace al post")',
            'div:has-text("Copy link to post")',
        ]:
            try:
                el = li_page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=2000):
                    el.click(timeout=4000)
                    L('clicked_copy_link', selector=sel)
                    time.sleep(2)
                    break
            except Exception:
                continue

        # Try reading clipboard
        try:
            clip = li_page.evaluate('navigator.clipboard.readText()')
            L('clipboard_read', clip=clip)
            if clip and 'linkedin' in clip:
                li_url = clip.strip()
        except Exception as e:
            L('clipboard_err', err=str(e))

    if not li_url:
        s = shot(li_page, "01_no_url")
        L('li_url_extraction_completely_failed', screenshot=s, fatal=True)
        print(json.dumps({'log': log}, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    # Clean URL: strip query if any
    li_url_clean = li_url.split('?')[0]
    L('li_url_final', url=li_url_clean)

    # ============================================================
    # STEP 2: Add comment on LinkedIn with X link
    # ============================================================
    li_page.goto(li_url_clean, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s1 = shot(li_page, "02_li_post_open")
    L('li_post_open', url=li_page.url, screenshot=s1)

    li_comment = f"Also dropped this as a thread on X for that audience — {X_THREAD_URL}"

    # Find and click comment editor
    li_commented = False
    for sel in [
        'div[contenteditable="true"][role="textbox"]',
        'div.ql-editor[contenteditable="true"]',
    ]:
        try:
            els = li_page.locator(sel)
            cnt = els.count()
            for k in range(cnt):
                el = els.nth(k)
                try:
                    if el.is_visible(timeout=2000):
                        el.click(timeout=4000)
                        time.sleep(0.5)
                        li_page.keyboard.insert_text(li_comment)
                        L('li_comment_typed', chars=len(li_comment), selector=sel, index=k)
                        li_commented = True
                        break
                except Exception:
                    continue
            if li_commented: break
        except Exception:
            continue

    if li_commented:
        time.sleep(1.5)
        # Submit comment with Cmd+Enter
        li_page.keyboard.press('Meta+Enter')
        L('li_comment_submitted_cmd_enter')
        time.sleep(5)
        s2 = shot(li_page, "03_li_after_comment")
        L('li_after_comment', screenshot=s2)
    else:
        s = shot(li_page, "03_li_no_comment_box")
        L('li_comment_failed', screenshot=s)

    # ============================================================
    # STEP 3: Reply to X thread with LinkedIn link
    # ============================================================
    x_page = None
    for p in ctx.pages:
        if 'x.com' in p.url:
            x_page = p
            break
    if not x_page:
        x_page = ctx.new_page()
        x_page.set_viewport_size({'width': 1400, 'height': 950})

    x_page.goto(X_THREAD_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s3 = shot(x_page, "04_x_thread_open")
    L('x_thread_open', url=x_page.url, screenshot=s3)

    x_reply = f"Full long-form version on LinkedIn → {li_url_clean}"

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
                x_page.keyboard.insert_text(x_reply)
                L('x_reply_typed', chars=len(x_reply))
                x_replied = True
                break
        except Exception:
            continue

    if x_replied:
        time.sleep(2)
        # Submit via Cmd+Enter or button click
        try:
            x_page.keyboard.press('Meta+Enter')
            L('x_reply_submitted_cmd_enter')
        except Exception:
            pass
        time.sleep(2)
        # Backup: click Reply button
        for sel in ['button[data-testid="tweetButtonInline"]', 'button[data-testid="tweetButton"]']:
            try:
                el = x_page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=2000) and el.is_enabled(timeout=1500):
                    el.click(timeout=4000)
                    L('x_reply_submitted_button', selector=sel)
                    break
            except Exception:
                continue
        time.sleep(5)
        s4 = shot(x_page, "05_x_after_reply")
        L('x_after_reply', screenshot=s4)

    print(json.dumps({'log': log, 'li_url': li_url_clean, 'x_url': X_THREAD_URL, 'ok': True}, indent=2, ensure_ascii=False))
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e))
    print(json.dumps({'log': log, 'ok': False}, indent=2, ensure_ascii=False))
