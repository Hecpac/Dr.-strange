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

    # Bottom-right image: x=781, y=471, w=559, h=313
    # Center: x=1060, y=627
    page.mouse.click(1060, 627)
    print("Clicked bottom-right image (1060, 627)")
    time.sleep(4)
    page.screenshot(path="/tmp/midjourney-v4-detail.png")
    print(f"URL: {page.url}")
    print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
