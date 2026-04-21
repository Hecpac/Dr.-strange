from playwright.sync_api import sync_playwright

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]
    for i, p in enumerate(ctx.pages):
        print(f"Tab {i}: {p.url[:100]} - {p.title()[:60]}")
    print(f"TOTAL: {len(ctx.pages)} tabs")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
