#!/usr/bin/env python3
"""Open landing prototype in Chrome via CDP and take screenshot."""
from playwright.sync_api import sync_playwright
import time

URL = "file:///Users/hector/Projects/Dr.-strange/captures/pachano-design/landing-prototype.html"

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(URL, timeout=15000, wait_until="load")
    time.sleep(2)
    page.screenshot(path="/tmp/landing-prototype.png")
    print(f"Opened: {URL}")
    print("Screenshot saved to /tmp/landing-prototype.png")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
