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

    # Click "Continue with Google"
    page.click("text=Continue with Google", timeout=10000)
    print("Clicked Continue with Google")

    time.sleep(8)
    page.screenshot(path="/tmp/midjourney-google.png")
    print(f"URL: {page.url}")
    print(f"Title: {page.title()}")
    print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
