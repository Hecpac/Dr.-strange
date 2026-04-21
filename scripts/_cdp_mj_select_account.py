from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    # Find Google auth page
    page = None
    for p in ctx.pages:
        if "accounts.google" in p.url:
            page = p
            break

    if not page:
        print("ERROR: No Google auth page found")
    else:
        page.click("text=pachanohector15@gmail.com", timeout=10000)
        print("Clicked pachanohector15 account")
        time.sleep(8)

        # Check midjourney tab for successful login
        for p in ctx.pages:
            if "midjourney" in p.url:
                p.screenshot(path="/tmp/midjourney-loggedin.png")
                print(f"Midjourney URL: {p.url}")
                print(f"Midjourney Title: {p.title()}")
                break
        print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
