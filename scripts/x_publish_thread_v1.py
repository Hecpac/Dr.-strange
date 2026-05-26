"""Publish X thread (8 tweets) — same V1 topic as LinkedIn post.
Via Chrome CDP on Hector's authenticated session (@PachanoDesign).
"""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/x"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
log = []

def L(name, **kw):
    log.append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/x_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

TWEETS = [
    "I built an AI agent that runs my business 24/7.\n\nThe thing that made it work isn't better prompts. It's not a bigger context window.\n\nIt's a file hierarchy you'd find in any decent codebase. 🧵",

    "Most \"AI agent\" failures aren't about model quality. They're about session amnesia.\n\nYou ask the agent to fix a bug. It does.\nNext session it forgets your workspace rules. Asks the same setup questions again.\n\nAnthropic calls this \"session guessing.\"",

    "The fix is a two-layer file hierarchy:\n\n1️⃣ Personal Rules (~/.codex/AGENTS.md)\nHow YOU work. Your style. Hard rules you've refused for years.\n\n2️⃣ Repository Rules (project AGENTS.md)\nWhat the codebase IS. Naming. Architectural fragility. Constraints.",

    "Two separate concerns. Two separate files. Both loaded automatically at session start.\n\nThe moment you separate \"how I work\" from \"what this project is,\" the prompt roulette stops.\n\nThe agent stops asking stupid questions.",

    "I've been running this in my own system for 6 months.\n\n5 layered files: identity, soul, user profile, project memory, conventions.\n\n~12KB of carefully maintained Markdown.\n\nThat 12KB does more for output quality than any prompt I've ever written.",

    "Three things changed:\n\n— Context drift dropped noticeably\n— Task onboarding went from 5 min to 30 sec\n— Mistakes became repairable, not recurring\n\nWhen the agent does something wrong, I update ONE file. Every future session inherits the fix.",

    "The uncomfortable truth most \"AI consultants\" won't tell you:\n\nThe breakthrough isn't the model.\nIt's the engineering rigor around it.\n\nIf you're prompting from scratch every session, you don't need a better model.\nYou need durable instruction files.",

    "What's the worst session amnesia moment you've had with an AI tool?\n\nDrop it below 👇",
]

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # Find X tab or open new
    page = None
    for p_existing in ctx.pages:
        if 'x.com' in p_existing.url or 'twitter.com' in p_existing.url:
            page = p_existing
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
    L('on_tab', url=page.url)

    # Verify authenticated
    if 'login' in page.url or 'flow' in page.url:
        s = shot(page, "00_authwall")
        L('auth_required', url=page.url, screenshot=s, fatal=True)
        print(json.dumps({'log': log}))
        raise SystemExit(1)

    # Navigate to home if not there
    if '/home' not in page.url and 'x.com' not in page.url.split('/')[-1]:
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)

    s0 = shot(page, "01_home")
    L('on_home', url=page.url, screenshot=s0)

    # Click "Post" button to open composer — top-left of X UI (the big blue button)
    # X uses data-testid="SideNav_NewTweet_Button" for the main Post button
    posted_opened = False
    for sel in [
        'a[data-testid="SideNav_NewTweet_Button"]',
        'a[href="/compose/post"]',
        'a[href="/compose/tweet"]',
        '[data-testid="tweetButtonInline"]',
        'a[aria-label="Post"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2000):
                el.click(timeout=4000)
                posted_opened = True
                L('opened_composer', selector=sel)
                break
        except Exception as e:
            L(f'compose_open_err_{sel[:30]}', err=str(e))
            continue

    if not posted_opened:
        # Fallback: navigate to /compose/post directly
        try:
            page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
            posted_opened = True
            L('opened_composer_via_navigate')
        except Exception as e:
            L('compose_navigate_err', err=str(e))

    if not posted_opened:
        s = shot(page, "02_no_composer")
        L('composer_not_opened', screenshot=s, fatal=True)
        print(json.dumps({'log': log}))
        raise SystemExit(2)

    time.sleep(3)
    s1 = shot(page, "03_composer_open")
    L('composer_open', screenshot=s1)

    # Type first tweet
    for i, tweet in enumerate(TWEETS):
        # Focus the current tweet textarea
        # X uses data-testid="tweetTextarea_0", _1, _2... for each thread tweet
        textarea_sel = f'[data-testid="tweetTextarea_{i}"]'
        focused = False
        for attempt in range(3):
            try:
                el = page.locator(textarea_sel).first
                if el.count() > 0 and el.is_visible(timeout=2500):
                    el.click(timeout=4000)
                    focused = True
                    break
            except Exception:
                pass
            time.sleep(1)

        if not focused:
            # Fallback: any contenteditable that's visible
            try:
                page.locator('div[contenteditable="true"]').nth(i).click(timeout=4000)
                focused = True
                L(f'tweet_{i}_focused_via_fallback')
            except Exception as e:
                L(f'tweet_{i}_focus_failed', err=str(e), fatal=True)
                s = shot(page, f"04_focus_failed_{i}")
                print(json.dumps({'log': log}))
                raise SystemExit(3)

        time.sleep(0.4)
        page.keyboard.insert_text(tweet)
        L(f'tweet_{i}_typed', chars=len(tweet))
        time.sleep(1.5)

        # If there are more tweets, click "+" to add next
        if i < len(TWEETS) - 1:
            added = False
            for sel in [
                'button[aria-label="Add post"]',
                'button[data-testid="addButton"]',
                'div[data-testid="addButton"]',
                'button[aria-label*="Add"]',
            ]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0 and el.is_visible(timeout=2000):
                        el.click(timeout=4000)
                        added = True
                        L(f'tweet_{i}_added_next', selector=sel)
                        break
                except Exception:
                    continue
            if not added:
                s = shot(page, f"05_no_add_button_{i}")
                L(f'tweet_{i}_no_add_button', screenshot=s, fatal=True)
                print(json.dumps({'log': log}))
                raise SystemExit(4)
            time.sleep(1.5)

    s2 = shot(page, "06_all_tweets_typed")
    L('all_tweets_typed', count=len(TWEETS), screenshot=s2)

    # Click "Post all" button
    posted = False
    for sel in [
        'button[data-testid="tweetButton"]',
        'button[data-testid="tweetButtonInline"]',
        'div[data-testid="tweetButton"]',
        'button:has-text("Post all")',
        'button:has-text("Post")',
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            for k in range(count):
                el = els.nth(k)
                try:
                    if el.is_visible(timeout=1500) and el.is_enabled(timeout=1500):
                        el.click(timeout=5000)
                        posted = True
                        L('post_clicked', selector=sel, index=k)
                        break
                except Exception:
                    continue
            if posted: break
        except Exception:
            continue

    if not posted:
        s = shot(page, "07_no_post_button")
        L('post_button_not_clicked', screenshot=s, fatal=True)
        print(json.dumps({'log': log}))
        raise SystemExit(5)

    time.sleep(8)
    s3 = shot(page, "08_after_post")
    L('after_post', url=page.url, screenshot=s3)

    # Verify thread visible (URL change to home/profile + first tweet text in body)
    try:
        body = page.locator('body').inner_text(timeout=4000)
        thread_visible = 'I built an AI agent that runs my business 24/7' in body
        L('verify', thread_in_feed=thread_visible)
    except Exception as e:
        L('verify_err', err=str(e))

    print(json.dumps({'log': log, 'ok': True}, indent=2, ensure_ascii=False))
except SystemExit:
    raise
except Exception as e:
    L('exception', err=str(e), type=type(e).__name__)
    print(json.dumps({'log': log, 'ok': False}, indent=2, ensure_ascii=False))
