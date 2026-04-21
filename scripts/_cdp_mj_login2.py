from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if "midjourney" in p.url:
            page = p
            break

    if not page:
        print("ERROR: No Midjourney tab found")
    else:
        # Find clickable buttons
        buttons = page.query_selector_all("button, a[href], [role='button']")
        for b in buttons:
            txt = (b.inner_text() or "").strip()[:50]
            tag = b.evaluate("el => el.tagName")
            href = b.get_attribute("href") or ""
            visible = b.is_visible()
            print(f"  [{tag}] visible={visible} text='{txt}' href='{href}'")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
