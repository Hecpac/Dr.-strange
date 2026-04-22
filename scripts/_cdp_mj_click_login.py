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

    buttons = page.query_selector_all("button")
    for b in buttons:
        if "Log In" in (b.inner_text() or ""):
            b.click()
            print("Clicked Log In button")
            break

    time.sleep(5)
    page.screenshot(path="/tmp/midjourney-login2.png")
    print(f"URL: {page.url}")
    print(f"Title: {page.title()}")
    print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
