"""
Try to load an Instagram post via Chrome CDP using the user's authenticated session.
Pre-loaded fetch returned an error page; Instagram requires login.
"""
import json, time, os
from playwright.sync_api import sync_playwright

URL = "https://www.instagram.com/p/DYZ0ptyAAIy/"
ART = "/Users/hector/Projects/Dr.-strange/artifacts/instagram"
os.makedirs(ART, exist_ok=True)
ts = int(time.time())

report = {'ts': ts, 'url': URL, 'steps': []}

def step(name, **kw):
    report['steps'].append({'name': name, **kw})

def shot(page, label):
    p = f"{ART}/ig_{ts}_{label}.png"
    try:
        page.screenshot(path=p, full_page=False)
        return p
    except Exception as e:
        return f"err:{e}"

try:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_viewport_size({'width': 1280, 'height': 1024})
    page.goto(URL, wait_until="domcontentloaded", timeout=30000)
    time.sleep(4)
    s1 = shot(page, "01_loaded")
    step('loaded', url=page.url, title=page.title(), screenshot=s1)

    # Auth wall detect
    auth = 'accounts/login' in page.url or 'accounts/signin' in page.url
    if auth:
        step('auth_wall', url=page.url)

    # Try to extract caption / author / metadata
    try:
        # IG layout: article > header > a[href*=...] for author
        author = page.locator('article header a').first.inner_text(timeout=5000)
        step('author', text=author)
    except Exception as e:
        step('author_fail', err=str(e))

    try:
        # Caption typically in article > div > ul > li > div > h1 (changes often)
        # Try multiple selectors
        for sel in ['article h1', 'article div[data-testid*="caption"]', 'article ul li div span',
                    'meta[property="og:description"]']:
            try:
                if sel.startswith('meta'):
                    val = page.locator(sel).first.get_attribute('content', timeout=3000)
                    if val:
                        step('caption_meta', value=val[:1000])
                        break
                else:
                    text = page.locator(sel).first.inner_text(timeout=3000)
                    if text:
                        step('caption', selector=sel, value=text[:1000])
                        break
            except Exception:
                continue
    except Exception as e:
        step('caption_fail', err=str(e))

    # Get all <meta> og tags
    meta_dump = {}
    for prop in ['og:title', 'og:description', 'og:image', 'og:video', 'twitter:title', 'twitter:description', 'twitter:image']:
        try:
            val = page.locator(f'meta[property="{prop}"]').first.get_attribute('content', timeout=1500)
            if val: meta_dump[prop] = val
        except Exception:
            try:
                val = page.locator(f'meta[name="{prop}"]').first.get_attribute('content', timeout=1500)
                if val: meta_dump[prop] = val
            except Exception:
                pass
    step('meta_tags', meta_dump=meta_dump)

    time.sleep(1)
    s2 = shot(page, "02_final")
    step('final_screenshot', screenshot=s2)

    report['ok'] = True
except Exception as e:
    step('exception', err=str(e), type=type(e).__name__)
    report['ok'] = False

print(json.dumps(report, indent=2, ensure_ascii=False))
