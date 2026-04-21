from playwright.sync_api import sync_playwright
import time
import urllib.request

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if "midjourney" in p.url:
            page = p
            break

    if not page:
        print("ERROR: No Midjourney tab")
        exit()

    # Take screenshot to see current state
    page.screenshot(path="/tmp/midjourney-check.png")
    print(f"URL: {page.url}")
    print(f"Title: {page.title()}")

    # Go back to Create page to find the upscaled image
    page.goto("https://www.midjourney.com/imagine", timeout=15000, wait_until="domcontentloaded")
    time.sleep(4)

    # Get the first/latest image URL (upscaled should be at the top)
    imgs = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            return Array.from(imgs)
                .filter(i => i.src.includes('cdn.midjourney.com') && i.offsetParent !== null)
                .map(i => ({
                    src: i.src,
                    w: i.getBoundingClientRect().width,
                    h: i.getBoundingClientRect().height,
                    y: i.getBoundingClientRect().y
                }))
                .sort((a, b) => a.y - b.y)
                .slice(0, 8);
        }
    """)

    print(f"\nTop images:")
    for i, img in enumerate(imgs):
        print(f"  [{i}] y={img['y']:.0f} {img['w']:.0f}x{img['h']:.0f} src={img['src'][:80]}")

    page.screenshot(path="/tmp/midjourney-latest.png")
    print("\nScreenshot saved")

except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
