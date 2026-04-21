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

    # Find all buttons to locate "Subtle" under Upscale
    buttons = page.query_selector_all("button")
    for b in buttons:
        txt = (b.inner_text() or "").strip()
        visible = b.is_visible()
        if visible and txt:
            print(f"  Button: '{txt}'")

    # Click the "Subtle" button in the Upscale row
    # There are two "Subtle" buttons - one for Vary, one for Upscale
    # Upscale Subtle should be the second one
    subtle_buttons = []
    for b in buttons:
        txt = (b.inner_text() or "").strip()
        if txt == "Subtle" and b.is_visible():
            subtle_buttons.append(b)

    if len(subtle_buttons) >= 2:
        # Second "Subtle" is for Upscale
        subtle_buttons[1].click()
        print("\nClicked Upscale Subtle")
    elif len(subtle_buttons) == 1:
        subtle_buttons[0].click()
        print("\nClicked first Subtle button")
    else:
        print("\nNo Subtle button found")

    time.sleep(5)
    page.screenshot(path="/tmp/midjourney-upscale.png")
    print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
