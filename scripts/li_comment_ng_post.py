"""Post a strategic comment on Andrew Ng's image/video agent course LinkedIn post."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/ng_{ts}_{label}.png"
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

    page.goto(NG_POST_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    s1 = shot(page, "01_post_open")
    L('post_open', url=page.url, screenshot=s1)

    # Dismiss any Premium modal
    for sel in ['button[aria-label*="Dismiss"]', 'button.artdeco-modal__dismiss']:
        try:
            els = page.locator(sel)
            for i in range(els.count()):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1000):
                        el.click(timeout=2000)
                        L('dismissed_modal', selector=sel)
                except Exception:
                    continue
        except Exception:
            continue
    time.sleep(1)

    # Scroll down a bit to ensure comment box is visible
    try:
        page.mouse.wheel(0, 400)
        time.sleep(1)
    except Exception:
        pass

    # Find and click the comment textbox
    # On Andrew Ng's post permalink view, the comment editor is typically below the post
    commented = False
    for sel in [
        'div[contenteditable="true"][role="textbox"]',
        'div.ql-editor[contenteditable="true"]',
    ]:
        try:
            els = page.locator(sel)
            cnt = els.count()
            L(f'editor_count_{sel[:30]}', count=cnt)
            for k in range(cnt):
                el = els.nth(k)
                try:
                    if el.is_visible(timeout=2000):
                        el.click(timeout=4000)
                        time.sleep(0.6)
                        page.keyboard.insert_text(COMMENT)
                        L('comment_typed', chars=len(COMMENT), selector=sel, index=k)
                        commented = True
                        break
                except Exception:
                    continue
            if commented: break
        except Exception:
            continue

    if not commented:
        s = shot(page, "02_no_editor")
        L('no_comment_editor', screenshot=s, fatal=True)
        print(json.dumps({'log': log}, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    time.sleep(2)
    s2 = shot(page, "03_typed")
    L('typed', screenshot=s2)

    # Submit via Cmd+Enter
    try:
        page.keyboard.press('Meta+Enter')
        L('submitted_cmd_enter')
    except Exception as e:
        L('cmd_enter_err', err=str(e))

    time.sleep(6)
    s3 = shot(page, "04_after")
    L('after', url=page.url, screenshot=s3)

    # Verify: check if comment text appears in page body
    try:
        body = page.locator('body').inner_text(timeout=4000)
        comment_visible = 'Same evaluation pattern' in body
        L('verify', comment_in_post=comment_visible)
    except Exception as e:
        L('verify_err', err=str(e))

    print(json.dumps({'log': log, 'ok': True}, indent=2, ensure_ascii=False))
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e))
    print(json.dumps({'log': log, 'ok': False}, indent=2, ensure_ascii=False))
