#!/usr/bin/env python3
"""Refresh landing page tab and take screenshot."""
from playwright.sync_api import sync_playwright
import time

URL = "file:///Users/hector/Projects/Dr.-strange/captures/pachano-design/landing-prototype.html"

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if "landing-prototype" in p.url:
            page = p
            break

    if not page:
        page = ctx.new_page()

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(URL, timeout=15000, wait_until="load")
    time.sleep(3)
    page.screenshot(path="/tmp/landing-v3.png")
    print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
