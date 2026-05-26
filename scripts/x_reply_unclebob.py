"""Post substantive reply to Uncle Bob Martin's tweet about agent productivity.
Anchored in actual swarm-forge repo read. Verify via DOM with specific selector.
"""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/x"
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/ub_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

TWEET_URL = "https://x.com/unclebobmartin/status/2057907070431543325"

REPLY = (
    "swarm-forge constitution split (project / engineering / workflow) + git worktrees per role is sharp. "
    "I run a similar pattern with one daemon and tier-based approval policies in version control. "
    "30-40% on tuning feels exactly right — anyone reporting less is hiding the work."
)

print(f"reply chars: {len(REPLY)}")

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if 'x.com' in p.url:
            page = p
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})

    page.goto(TWEET_URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(6)
    s1 = shot(page, "01_tweet_open")
    L('tweet_open', url=page.url, screenshot=s1)

    # Click the reply textbox
    typed = False
    for sel in [
        '[data-testid="tweetTextarea_0"]',
        'div[contenteditable="true"][role="textbox"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=3000):
                el.click(timeout=4000)
                time.sleep(0.6)
                page.keyboard.insert_text(REPLY)
                typed = True
                L('reply_typed', selector=sel, chars=len(REPLY))
                break
        except Exception as e:
            L(f'editor_err_{sel[:30]}', err=str(e))
            continue

    if not typed:
        s = shot(page, "02_no_editor")
        L('no_reply_editor', screenshot=s, fatal=True)
        print(json.dumps({'log': log}, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    time.sleep(2)
    s2 = shot(page, "03_typed")
    L('typed', screenshot=s2)

    # Submit — X reply uses Cmd+Enter as primary (documented working from earlier thread post),
    # backup: explicit click on tweetButtonInline
    submitted = False
    try:
        page.keyboard.press('Meta+Enter')
        L('cmd_enter_pressed')
        submitted = True
    except Exception as e:
        L('cmd_enter_err', err=str(e))

    # If Cmd+Enter didn't work, click the Reply button
    time.sleep(3)
    # Quick check if editor is still open with content — if yes, Cmd+Enter didn't submit
    try:
        editor_count = page.locator('[data-testid="tweetTextarea_0"]').count()
        L('post_cmdenter_editor_count', count=editor_count)
        if editor_count > 0:
            # Editor still open — try button click
            for sel in [
                'button[data-testid="tweetButtonInline"]',
                'button[data-testid="tweetButton"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=2000) and el.is_enabled(timeout=1500):
                        el.click(timeout=4000)
                        L('button_clicked_backup', selector=sel)
                        break
                except Exception:
                    continue
    except Exception:
        pass

    # Wait for submission to process
    time.sleep(7)
    s3 = shot(page, "04_after_submit")
    L('after_submit', url=page.url, screenshot=s3)

    # VERIFICATION (strict, per new protocol):
    # Look for the reply text inside a published tweet container, NOT in the editor
    # X uses article[data-testid="tweet"] for each published tweet (including replies)
    js_verify = """
    () => {
        const fingerprint = 'swarm-forge constitution split';
        // Look in published tweet articles only
        const tweets = Array.from(document.querySelectorAll('article[data-testid="tweet"]'));
        const matches = [];
        for (const t of tweets) {
            const txt = (t.textContent || '').trim();
            if (txt.includes(fingerprint)) {
                matches.push({
                    snippet: txt.slice(0, 200),
                    length: txt.length,
                });
            }
        }
        return {
            published_tweet_count: tweets.length,
            matches_in_published_tweets: matches.length,
            samples: matches.slice(0, 2),
        };
    }
    """
    verify = page.evaluate(js_verify)
    L('verify_strict', result=verify)

    published = verify.get('matches_in_published_tweets', 0) > 0

    if not published:
        # Try scrolling down to load the reply chain
        for _ in range(3):
            try:
                page.mouse.wheel(0, 700)
                time.sleep(1.5)
            except Exception:
                pass
        time.sleep(2)
        verify2 = page.evaluate(js_verify)
        L('verify_strict_after_scroll', result=verify2)
        published = verify2.get('matches_in_published_tweets', 0) > 0

    s4 = shot(page, "05_verify")
    L('verify_screenshot', screenshot=s4)

    print(json.dumps({'log': log, 'published': published, 'ok': True}, indent=2, ensure_ascii=False))
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e))
    print(json.dumps({'log': log, 'ok': False}, indent=2, ensure_ascii=False))
