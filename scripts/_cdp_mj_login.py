from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    # Find the Midjourney tab
    page = None
    for p in ctx.pages:
        if "midjourney" in p.url:
            page = p
            break

    if not page:
        print("ERROR: No Midjourney tab found")
    else:
        # Click Log In button
        page.click("text=Log In", timeout=10000)
        time.sleep(4)
        page.screenshot(path="/tmp/midjourney-login.png")
        print(f"URL: {page.url}")
        print(f"Title: {page.title()}")
        print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
