from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    for p in ctx.pages:
        if "midjourney" in p.url:
            time.sleep(2)
            p.screenshot(path="/tmp/midjourney-status.png")
            print(f"URL: {p.url}")
            print(f"Title: {p.title()}")
            print("Screenshot saved")
            break
    else:
        print("No midjourney tab found")
        for i, p in enumerate(ctx.pages):
            print(f"  Tab {i}: {p.url[:80]}")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
