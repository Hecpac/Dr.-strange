"""Retry: dismiss Premium modal first, then publish V1 LinkedIn post."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/li2_{ts}_{label}.png"
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

    # Find existing linkedin/feed tab
    page = None
    for p_existing in ctx.pages:
        if 'linkedin.com/feed' in p_existing.url or p_existing.url == 'https://www.linkedin.com/':
            page = p_existing
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
    step('on_tab', url=page.url)

    # Dismiss Premium modal — try multiple strategies
    dismissed = False
    # Strategy 1: explicit close button on modal
    for sel in [
        'button[aria-label*="Dismiss"]',
        'button[aria-label*="Descartar"]',
        'button[aria-label*="Close"]',
        'button[aria-label*="Cerrar"]',
        'svg[data-test-icon="close-medium"]',
        'button:has(svg[data-test-icon="close-medium"])',
        'button.artdeco-modal__dismiss',
        '.artdeco-modal__dismiss',
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1500):
                        el.click(timeout=3000)
                        dismissed = True
                        step('modal_dismissed', selector=sel, index=i)
                        break
                except Exception:
                    continue
            if dismissed: break
        except Exception:
            continue

    # Strategy 2: escape key
    if not dismissed:
        try:
            page.keyboard.press('Escape')
            time.sleep(0.5)
            page.keyboard.press('Escape')
            step('pressed_escape_twice')
        except Exception:
            pass

    time.sleep(2)
    s1 = shot(page, "01_after_dismiss")
    step('after_dismiss', screenshot=s1)

    # Hard reload feed to ensure clean state
    page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    s2 = shot(page, "02_feed_reloaded")
    step('feed_reloaded', url=page.url, screenshot=s2)

    # Dismiss any modal that re-appears
    for sel in ['button.artdeco-modal__dismiss', 'button[aria-label*="Dismiss"]', 'button[aria-label*="Close"]']:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1500):
                el.click(timeout=3000)
                step('post_reload_modal_dismissed', selector=sel)
                time.sleep(1)
                break
        except Exception:
            continue

    # Find "Start a post" button — broader search
    clicked_start = False
    for sel in [
        'button:has-text("Start a post")',
        'button:has-text("Comenzar una publicación")',
        'button:has-text("Empieza una publicación")',
        'button[aria-label*="Start a post"]',
        '.share-box-feed-entry__trigger',
        '[class*="share-box"] button',
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=2000):
                        el.click(timeout=4000)
                        clicked_start = True
                        step('clicked_start_a_post', selector=sel, index=i)
                        break
                except Exception:
                    continue
            if clicked_start: break
        except Exception:
            continue

    if not clicked_start:
        # Try keyboard shortcut 'n' which opens new post on LinkedIn
        try:
            page.keyboard.press('n')
            time.sleep(2)
            step('tried_keyboard_n_shortcut')
        except Exception:
            pass

    time.sleep(3)
    s3 = shot(page, "03_after_start_click")
    step('after_start_click', url=page.url, screenshot=s3)

    # Focus composer
    composer_focused = False
    for sel in [
        'div[contenteditable="true"][role="textbox"]',
        'div.ql-editor[contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="ext editor"]',
        'div[contenteditable="true"][aria-label*="post"]',
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

    time.sleep(0.6)
    try:
        page.keyboard.insert_text(POST_TEXT)
        step('text_inserted', chars=len(POST_TEXT))
    except Exception as e:
        step('insert_err', err=str(e))
        # fallback: typing
        for line in POST_TEXT.split('\n'):
            page.keyboard.type(line, delay=2)
            page.keyboard.press('Enter')

    time.sleep(2)
    s4 = shot(page, "05_text_filled")
    step('text_filled', screenshot=s4)

    # Click Post button
    posted = False
    for sel in [
        'button:has-text("Post")',
        'button:has-text("Publicar")',
        'button[aria-label*="Post"]',
    ]:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=1500) and el.is_enabled(timeout=1500):
                        txt = (el.inner_text(timeout=1500) or '').strip().lower()
                        if txt in ('post', 'publicar'):
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
        s = shot(page, "06_no_post_button")
        step('post_button_not_clicked', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(4)

    time.sleep(7)
    s5 = shot(page, "07_after_post_click")
    step('after_post_click', url=page.url, screenshot=s5)

    # Verify success markers
    try:
        body = page.locator('body').inner_text(timeout=4000)
        modal_open = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
        step('verify_state', modal_open=modal_open, body_has_view_post='View post' in body, body_has_visible='visible' in body.lower())
    except Exception as e:
        step('verify_err', err=str(e))

    report['ok'] = True
    report['posted'] = posted
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
