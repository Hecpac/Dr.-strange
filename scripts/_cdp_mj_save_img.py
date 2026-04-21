from playwright.sync_api import sync_playwright
import time
import base64

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if "midjourney" in p.url:
            page = p
            break

    # Click on the upscaled image to open detail
    page.mouse.click(400, 150)
    time.sleep(3)

    # Get the main image src from detail view
    img_src = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            const big = Array.from(imgs)
                .filter(i => i.src.includes('cdn.midjourney.com') && i.offsetParent !== null)
                .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
            return big.length > 0 ? big[0].src : null;
        }
    """)

    print(f"Image src: {img_src[:120] if img_src else 'None'}")

    if img_src:
        # Use browser to fetch the full image and convert to base64
        b64 = page.evaluate("""
            async (url) => {
                const resp = await fetch(url);
                const blob = await resp.blob();
                return new Promise((resolve) => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(reader.result.split(',')[1]);
                    reader.readAsDataURL(blob);
                });
            }
        """, img_src)

        if b64:
            data = base64.b64decode(b64)
            ext = ".webp" if "webp" in img_src else ".png"
            out = f"/Users/hector/Projects/Dr.-strange/captures/pachano-design/assets/hero-midjourney{ext}"
            with open(out, "wb") as f:
                f.write(data)
            print(f"Downloaded: {len(data)} bytes ({len(data)/1024:.0f} KB)")
            print(f"Saved to: {out}")
        else:
            print("Failed to fetch image via browser")

    page.screenshot(path="/tmp/midjourney-detail-up.png")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
