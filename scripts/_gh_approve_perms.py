#!/usr/bin/env python3
"""Click 'Accept new permissions' on GitHub Claude app page."""
from playwright.sync_api import sync_playwright
import time

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if "permissions/update" in p.url or "Claude permissions" in p.title():
            page = p
            break

    if not page:
        print("ERROR: Tab not found")
        exit(1)

    page.click("text=Accept new permissions")
    time.sleep(4)
    page.screenshot(path="/tmp/gh-perms-accepted.png")
    print(f"Title: {page.title()}")
    print(f"URL: {page.url}")

except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
