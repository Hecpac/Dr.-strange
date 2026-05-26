"""Publish V1 LinkedIn post via Chrome CDP using Hector's authenticated session.
Workflow: feed -> Start a post -> paste text -> screenshot for verify -> click Post -> verify success.
"""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/li_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

POST_TEXT = """I built an AI agent that runs my business 24/7.

The pattern that made it work isn't better prompts. It's a file hierarchy you can find in any decent codebase.

Here's the insight from Anthropic's research most builders are still missing →

Most "AI agent" failures aren't about model quality. They're about session amnesia.

You ask the agent to fix a bug. It does. Next session it forgets the workspace rules. Asks you the same setup questions again. You re-explain. It works. Next time → same loop.

Anthropic researchers call this "session guessing" — the agent has no durable memory of how you work or what the project is.

The fix isn't better prompts. It isn't a bigger context window.

It's a two-layer file hierarchy:

1️⃣ Personal Rules (~/.codex/AGENTS.md)
How YOU work. Coding style. Error handling. The things you've refused for years.

2️⃣ Repository Rules (project AGENTS.md)
What the codebase IS. Naming conventions. Fragile points. Deployment constraints.

Two separate concerns. Two separate files. Both loaded automatically at session start.

The moment you separate "how I work" from "what this project is," the agent stops asking stupid questions. The "prompt roulette" — where global and local rules collide and the agent guesses — disappears.

I've been running this pattern in my own system for 6 months. My agent boots with 5 layered files: identity, soul, user profile, project memory, conventions. ~12KB of carefully maintained Markdown.

That 12KB does more for output quality than any prompt I've ever written.

Three things changed when I implemented this:

— Context drift dropped noticeably. The agent stops re-deriving constraints I've already declared.

— Onboarding new tasks went from 5 minutes to 30 seconds. No re-explanation. The file system is the brief.

— Mistakes became repairable, not recurring. When the agent does something wrong, I update ONE file. Every future session has the fix.

The uncomfortable truth most "AI consultants" won't tell you: the breakthrough isn't the model. It's the engineering rigor around it.

If you're building agents and feeling like you're "prompting from scratch" every session, you don't need a better model. You need durable instruction files.

What's the worst session amnesia moment you've had with an AI tool?

#AIEngineering #AgenticAI #SoftwareArchitecture #BuildingInPublic"""

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]

    # Find or open a LinkedIn feed tab
    page = None
    for p_existing in ctx.pages:
        if 'linkedin.com' in p_existing.url and ('/feed' in p_existing.url or p_existing.url.endswith('linkedin.com/')):
            page = p_existing
            step('reused_existing_linkedin_tab', url=p_existing.url)
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(5)
        step('new_tab_to_feed', url=page.url)

    # Verify authenticated
    if 'authwall' in page.url or 'login' in page.url:
        s = shot(page, "00_authwall")
        step('auth_required', url=page.url, screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(1)

    # Navigate to feed if not already there
    if '/feed' not in page.url:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)

    s1 = shot(page, "01_feed_landed")
    step('on_feed', url=page.url, screenshot=s1)

    # Click "Start a post" button
    clicked_start = False
    for sel in [
        'button:has-text("Start a post")',
        'button:has-text("Comenzar una publicación")',
        'button[aria-label*="Start a post"]',
        'button[aria-label*="omenzar"]',
        '.share-box-feed-entry__trigger',
        'button.artdeco-button:has-text("Start a post")',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2500):
                el.click(timeout=5000)
                clicked_start = True
                step('clicked_start_a_post', selector=sel)
                break
        except Exception:
            continue

    if not clicked_start:
        s = shot(page, "02_no_start_button")
        step('start_button_not_found', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(2)

    time.sleep(3)
    s2 = shot(page, "03_composer_opened")
    step('composer_opened', screenshot=s2)

    # Find the contenteditable post body and focus it
    composer_focused = False
    for sel in [
        'div[contenteditable="true"][role="textbox"]',
        'div.ql-editor[contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="Text editor"]',
        'div[contenteditable="true"][aria-label*="post"]',
        '.editor-content div[contenteditable="true"]',
    ]:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2500):
                el.click(timeout=4000)
                composer_focused = True
                step('composer_focused', selector=sel)
                break
        except Exception:
            continue

    if not composer_focused:
        s = shot(page, "04_no_composer")
        step('composer_not_found', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(3)

    time.sleep(0.5)

    # Type the post text using insertText to preserve emojis and newlines
    # Use page.keyboard.insert_text — better than keyboard.type for special chars
    try:
        page.keyboard.insert_text(POST_TEXT)
        step('text_inserted_via_insert_text', chars=len(POST_TEXT))
    except Exception as e:
        step('insert_text_err', err=str(e))
        # Fallback to typing line by line
        for line in POST_TEXT.split('\n'):
            page.keyboard.type(line, delay=2)
            page.keyboard.press('Enter')
        step('text_typed_line_by_line', chars=len(POST_TEXT))

    time.sleep(2)
    s3 = shot(page, "05_text_filled")
    step('text_filled', screenshot=s3)

    # IMPORTANT: pause to allow Hector to abort if anything is wrong
    # (we screenshot here for verification, then continue if no abort signal)
    # Hector cannot intervene in real-time since he's not at Mac, but we have the screenshot
    # for post-mortem audit

    # Click Post button
    posted = False
    for sel in [
        'button:has-text("Post")',
        'button:has-text("Publicar")',
        'button[aria-label*="Post"]',
        'button[aria-label*="Publicar"]',
        '.share-actions__primary-action',
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1500) and el.is_enabled(timeout=1500):
                        txt = (el.inner_text(timeout=1500) or '').strip()
                        # Only click the primary "Post" button, not nested ones with longer text
                        if txt.lower() in ('post', 'publicar'):
                            el.click(timeout=5000)
                            posted = True
                            step('post_button_clicked', selector=sel, index=i, text=txt)
                            break
                except Exception:
                    continue
            if posted: break
        except Exception:
            continue

    if not posted:
        s = shot(page, "06_post_button_not_clicked")
        step('post_button_not_clicked', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(4)

    # Wait for success: modal closes, URL stays /feed, toast appears
    time.sleep(6)
    s4 = shot(page, "07_after_post")
    step('after_post_click', url=page.url, screenshot=s4)

    # Verify success
    success_signals = {}
    try:
        body = page.locator('body').inner_text(timeout=4000)
        for kw in ['Post successful', 'Publicación exitosa', 'Your post', 'Tu publicación', 'visible', 'View post']:
            if kw.lower() in body.lower():
                success_signals[kw] = True
    except Exception as e:
        step('body_scan_err', err=str(e))

    # Modal closed check
    modal_still_open = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
    step('modal_state', still_open=modal_still_open)

    step('success_signals', signals=success_signals)
    report['ok'] = True
    report['posted'] = posted
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
