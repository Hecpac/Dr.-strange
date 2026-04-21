from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    print(f"Total pages: {len(ctx.pages)}")
    for i, p in enumerate(ctx.pages):
        url = p.url[:100]
        title = p.title()[:60]
        print(f"  Tab {i}: {url} — {title}")

    # Check for Google accounts popup
    for p in ctx.pages:
        if "accounts.google" in p.url:
            print(f"\nGoogle auth page found: {p.url[:120]}")
            p.screenshot(path="/tmp/midjourney-google-popup.png")
            print("Screenshot saved to /tmp/midjourney-google-popup.png")
            break
    else:
        # Maybe the login already completed, check midjourney page
        for p in ctx.pages:
            if "midjourney" in p.url:
                time.sleep(2)
                p.screenshot(path="/tmp/midjourney-current.png")
                print(f"\nMidjourney page: {p.url}")
                print("Screenshot saved to /tmp/midjourney-current.png")
                break
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
