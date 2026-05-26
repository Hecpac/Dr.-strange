"""V3: JS-based 'Start a post' click + robust composer detect."""
import json, time, os
from playwright.sync_api import sync_playwright

ART = "/Users/hector/Projects/Dr.-strange/artifacts/linkedin"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())
report = {'ts': ts, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/li3_{ts}_{label}.png"
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

    page = None
    for p_existing in ctx.pages:
        if 'linkedin.com/feed' in p_existing.url:
            page = p_existing
            break
    if not page:
        page = ctx.new_page()
        page.set_viewport_size({'width': 1400, 'height': 950})
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
    step('on_tab', url=page.url)

    # Dismiss any survey/modal
    js_dismiss = """
    () => {
        // dismiss feedback surveys, modals, etc
        const closeButtons = Array.from(document.querySelectorAll('button[aria-label*="Dismiss"], button[aria-label*="Close"], button.artdeco-modal__dismiss'));
        let n = 0;
        for (const b of closeButtons) {
            try { b.click(); n++; } catch(e){}
        }
        return n;
    }
    """
    dismissed = page.evaluate(js_dismiss)
    step('dismissed_count', n=dismissed)
    time.sleep(1.5)

    # Click "Start a post" via JS — find by text and click
    js_click_start = """
    () => {
        // Search for element with exact "Start a post" text
        const all = Array.from(document.querySelectorAll('button, div[role="button"], span[role="button"]'));
        for (const el of all) {
            const t = (el.innerText || el.textContent || '').trim();
            if (t === 'Start a post' || t === 'Comenzar una publicación' || t.startsWith('Start a post')) {
                el.scrollIntoView({block:'center'});
                el.click();
                return {clicked: true, tag: el.tagName, role: el.getAttribute('role'), text: t.slice(0,40)};
            }
        }
        // Fallback: any element near share-box
        const sb = document.querySelector('.share-box-feed-entry__trigger, [data-test-id="share-box-feed-entry-trigger"], [class*="share-box"]');
        if (sb) {
            sb.scrollIntoView({block:'center'});
            sb.click();
            return {clicked: 'fallback-sharebox', cls: sb.className};
        }
        return {clicked: false};
    }
    """
    r1 = page.evaluate(js_click_start)
    step('start_click_result', result=r1)
    time.sleep(3)
    s1 = shot(page, "01_after_start")
    step('after_start', screenshot=s1)

    # Find composer with multiple selectors + iframes
    composer_focused = False
    selectors = [
        'div[contenteditable="true"][role="textbox"]',
        'div.ql-editor[contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="ext editor"]',
        'div[contenteditable="true"][aria-label*="post"]',
        'div[contenteditable="true"]',
    ]
    for sel in selectors:
        try:
            els = page.locator(sel)
            count = els.count()
            for i in range(count):
                el = els.nth(i)
                try:
                    if el.is_visible(timeout=2000):
                        el.click(timeout=4000)
                        composer_focused = True
                        step('composer_focused', selector=sel, index=i)
                        break
                except Exception:
                    continue
            if composer_focused: break
        except Exception:
            continue

    if not composer_focused:
        s = shot(page, "02_no_composer")
        step('composer_not_found', screenshot=s, fatal=True)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        raise SystemExit(3)

    time.sleep(0.6)
    page.keyboard.insert_text(POST_TEXT)
    step('text_inserted', chars=len(POST_TEXT))

    time.sleep(2)
    s2 = shot(page, "03_text_filled")
    step('text_filled', screenshot=s2)

    # Click Post button
    js_click_post = """
    () => {
        const all = Array.from(document.querySelectorAll('button'));
        for (const b of all) {
            const t = (b.innerText || '').trim();
            if ((t === 'Post' || t === 'Publicar') && !b.disabled) {
                const rect = b.getBoundingClientRect();
                if (rect.width > 30 && rect.height > 20) {
                    b.scrollIntoView({block:'center'});
                    b.click();
                    return {clicked: true, text: t};
                }
            }
        }
        return {clicked: false};
    }
    """
    r2 = page.evaluate(js_click_post)
    step('post_click_result', result=r2)

    time.sleep(7)
    s3 = shot(page, "04_after_post")
    step('after_post', url=page.url, screenshot=s3)

    # Verify
    try:
        modal_still = page.locator('div[contenteditable="true"][role="textbox"]').count() > 0
        body = page.locator('body').inner_text(timeout=4000)
        toast = any(k in body.lower() for k in ['post successful', 'view post', 'tu publicación', 'your post is now visible', 'publicación exitosa'])
        step('verify', modal_still=modal_still, toast=toast)
    except Exception as e:
        step('verify_err', err=str(e))

    report['ok'] = True
except SystemExit:
    raise
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
