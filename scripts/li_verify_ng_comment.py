"""Verify if the Ng comment actually posted. If not, repost with explicit button click."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/verify_ng_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

NG_POST_URL = "https://www.linkedin.com/posts/andrewyng_new-course-build-ai-agents-that-generate-ugcPost-7462912139121352704-H14S/"

COMMENT = (
    "Same evaluation pattern, different domain — I run it for ops agents, not just creative. "
    "The bottleneck shifted exactly as Maryna Deundiak said: from generation to verification + authority.\n\n"
    "What's worked for me: encoding the rubric as tier-based approval policies in version control "
    "(file_delete=critical, deploy=critical, send_message=medium). The agent reads it before acting, not after.\n\n"
    "The Goodhart risk Jawad raised is real. But a versioned rubric lets you audit the optimization "
    "pressure too — the diff tells you when the agent starts gaming it."
)

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if 'linkedin.com' in p.url:
            page = p
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})

    # Force a fresh load
    page.goto(NG_POST_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)

    # Dismiss modals
    for sel in ['button[aria-label*="Dismiss"]', 'button.artdeco-modal__dismiss']:
        try:
            els = page.locator(sel)
            for i in range(els.count()):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1000):
                        el.click(timeout=2000)
                except Exception:
                    continue
        except Exception:
            continue
    time.sleep(1)

    # Scroll down to load comments
    for _ in range(4):
        try:
            page.mouse.wheel(0, 800)
            time.sleep(1)
        except Exception:
            pass
    time.sleep(2)

    s1 = shot(page, "01_post_scrolled")
    L('post_scrolled', screenshot=s1)

    # Try switching sort to "Most recent" to find recent comments
    sort_clicked = False
    for sel in [
        'button:has-text("Most relevant")',
        'button:has-text("Más relevante")',
        'button[aria-label*="Sort"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                sort_clicked = True
                L('sort_dropdown_clicked', selector=sel)
                time.sleep(1.5)
                # Click "Most recent"
                for opt_sel in [
                    'div[role="menuitem"]:has-text("Most recent")',
                    'div[role="menuitem"]:has-text("Más recientes")',
                    '[role="option"]:has-text("Most recent")',
                ]:
                    try:
                        opt = page.locator(opt_sel).first
                        if opt.count() > 0 and opt.is_visible(timeout=1500):
                            opt.click(timeout=3000)
                            L('sort_to_most_recent', selector=opt_sel)
                            break
                    except Exception:
                        continue
                break
        except Exception:
            continue
    time.sleep(3)
    s2 = shot(page, "02_sorted_recent")
    L('sorted_recent', screenshot=s2)

    # Search for my comment by JS — actual comment cards (not editor)
    js_find = """
    () => {
        const fingerprint = 'Same evaluation pattern, different domain';
        // Comment containers in LinkedIn use article or specific class names
        const candidates = Array.from(document.querySelectorAll('article, div.comments-comment-item, div[class*="comments-comment"], div[class*="comment-item"]'));
        const matches = [];
        for (const c of candidates) {
            const txt = (c.textContent || '').trim();
            if (txt.includes(fingerprint)) {
                matches.push({
                    tag: c.tagName,
                    cls: (c.className || '').toString().slice(0, 120),
                    len: txt.length,
                });
            }
        }
        // Also: any comment block within the comments thread
        const allComments = Array.from(document.querySelectorAll('[data-test-id*="comment"], [data-id*="comment"]'));
        const m2 = [];
        for (const c of allComments) {
            const txt = (c.textContent || '').trim();
            if (txt.includes(fingerprint)) {
                m2.push({type: 'data-id', len: txt.length});
            }
        }
        return {dom_matches: matches.length, sample: matches.slice(0, 3), data_id_matches: m2.length};
    }
    """
    found = page.evaluate(js_find)
    L('comment_search', result=found)

    posted = found.get('dom_matches', 0) > 0 or found.get('data_id_matches', 0) > 0

    if posted:
        L('verified_posted_yes', evidence=found)
        s3 = shot(page, "03_confirmed_posted")
        L('confirmed', screenshot=s3)
        print(json.dumps({'log': log, 'posted': True}, indent=2, ensure_ascii=False))
        raise SystemExit(0)

    # NOT POSTED — repost now with explicit button click
    L('NOT_POSTED_repost_attempt')

    # Scroll back to top so "Add a comment" placeholder is accessible
    page.goto(NG_POST_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)

    # Dismiss again
    for sel in ['button[aria-label*="Dismiss"]']:
        try:
            els = page.locator(sel)
            for i in range(els.count()):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1000):
                        el.click(timeout=2000)
                except Exception:
                    continue
        except Exception:
            continue

    # Scroll down to ensure comments section is loaded
    for _ in range(3):
        try:
            page.mouse.wheel(0, 500)
            time.sleep(0.8)
        except Exception:
            pass

    # Find the "Add a comment" placeholder
    # On LinkedIn it's typically a div that needs to be clicked first to expand into editor
    placeholder_clicked = False
    for sel in [
        'button[aria-label="Comment"]',
        '[aria-label*="Add a comment"]',
        'div[aria-label*="Add a comment"]',
        '[data-test-comment-text-editor]',
        '.comments-comment-box__form',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                placeholder_clicked = True
                L('placeholder_clicked', selector=sel)
                break
        except Exception:
            continue
    time.sleep(1.5)

    # Now click the actual textbox
    typed = False
    for sel in ['div[contenteditable="true"][role="textbox"]', 'div.ql-editor[contenteditable="true"]']:
        try:
            els = page.locator(sel)
            cnt = els.count()
            for k in range(cnt):
                el = els.nth(k)
                try:
                    if el.is_visible(timeout=1500):
                        el.click(timeout=3000)
                        time.sleep(0.6)
                        page.keyboard.insert_text(COMMENT)
                        typed = True
                        L('comment_retyped', selector=sel, index=k, chars=len(COMMENT))
                        break
                except Exception:
                    continue
            if typed: break
        except Exception:
            continue

    if not typed:
        s = shot(page, "04_repost_no_editor")
        L('repost_no_editor', screenshot=s)
        print(json.dumps({'log': log, 'posted': False}, indent=2, ensure_ascii=False))
        raise SystemExit(2)

    time.sleep(2)
    s4 = shot(page, "05_repost_typed")
    L('repost_typed', screenshot=s4)

    # Now click the explicit "Comment" or "Post" submit button
    submitted = False
    js_find_submit = """
    () => {
        // LinkedIn comment submit button: usually a button with text "Comment" or "Post"
        // It's inside the comment editor form, distinct from the post-level Comment toolbar button
        const editors = Array.from(document.querySelectorAll('form, div.comments-comment-box, [class*="comment-box"]'));
        for (const f of editors) {
            const buttons = Array.from(f.querySelectorAll('button'));
            for (const b of buttons) {
                const t = (b.innerText || '').trim().toLowerCase();
                if ((t === 'comment' || t === 'post' || t === 'reply' || t === 'comentar' || t === 'publicar' || t === 'responder') && !b.disabled) {
                    const rect = b.getBoundingClientRect();
                    if (rect.width > 30 && rect.height > 20) {
                        b.scrollIntoView({block: 'center'});
                        b.click();
                        return {clicked: true, text: t, x: rect.x + rect.width/2, y: rect.y + rect.height/2};
                    }
                }
            }
        }
        return {clicked: false};
    }
    """
    submit_result = page.evaluate(js_find_submit)
    L('submit_attempt', result=submit_result)
    submitted = submit_result.get('clicked', False)

    time.sleep(7)
    s5 = shot(page, "06_after_submit")
    L('after_submit', screenshot=s5)

    # Re-verify
    found2 = page.evaluate(js_find)
    L('reverify', result=found2)
    final_posted = found2.get('dom_matches', 0) > 0 or found2.get('data_id_matches', 0) > 0

    print(json.dumps({'log': log, 'posted': final_posted, 'first_attempt_failed': True}, indent=2, ensure_ascii=False))
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e))
    print(json.dumps({'log': log, 'ok': False}, indent=2, ensure_ascii=False))
