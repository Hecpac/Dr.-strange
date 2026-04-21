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

    if page:
        page.keyboard.press("Enter")
        print("Pressed Enter to submit prompt")
        time.sleep(5)
        page.screenshot(path="/tmp/midjourney-submitted.png")
        print(f"URL: {page.url}")
        print("Screenshot saved")
    else:
        print("ERROR: No Midjourney tab")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
