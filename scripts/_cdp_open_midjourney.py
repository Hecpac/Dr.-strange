from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    # Open new tab with Midjourney
    page = ctx.new_page()
    page.goto("https://www.midjourney.com/imagine", timeout=30000, wait_until="domcontentloaded")
    time.sleep(3)

    print(f"URL: {page.url}")
    print(f"Title: {page.title()}")
    page.screenshot(path="/tmp/midjourney.png")
    print("Screenshot saved to /tmp/midjourney.png")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
