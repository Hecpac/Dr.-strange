from playwright.sync_api import sync_playwright
import time
import base64
import re

pw = sync_playwright().start()
try:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if "midjourney" in p.url:
            page = p
            break

    # Navigate to the upscaled job detail page
    # The upscaled image ID is 9b1d0685-5c2c-48f6-a3c4-86c260e0040d
    page.goto("https://www.midjourney.com/jobs/9b1d0685-5c2c-48f6-a3c4-86c260e0040d", timeout=15000, wait_until="domcontentloaded")
    time.sleep(4)

    # Get all image sources
    all_srcs = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            return Array.from(imgs)
                .filter(i => i.src.includes('cdn.midjourney.com') && i.offsetParent !== null)
                .map(i => ({
                    src: i.src,
                    w: i.naturalWidth,
                    h: i.naturalHeight,
                    dispW: i.getBoundingClientRect().width,
                    dispH: i.getBoundingClientRect().height
                }));
        }
    """)

    print("All CDN images:")
    for s in all_srcs:
        print(f"  {s['w']}x{s['h']} (disp: {s['dispW']:.0f}x{s['dispH']:.0f}) {s['src'][:100]}")

    # Find the largest image and modify URL for full quality
    if all_srcs:
        biggest = max(all_srcs, key=lambda s: s['dispW'])
        src = biggest['src']
        # Try to get full resolution by modifying URL
        # Remove quality param, increase size
        full_url = re.sub(r'_\d+_N\.webp.*', '_3072_N.webp', src)
        print(f"\nTrying full res URL: {full_url[:100]}")

        b64 = page.evaluate("""
            async (url) => {
                try {
                    const resp = await fetch(url);
                    if (!resp.ok) return null;
                    const blob = await resp.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; }
            }
        """, full_url)

        if not b64:
            # Try without size suffix
            full_url = re.sub(r'_\d+_N\.webp.*', '.webp', src)
            print(f"Retrying: {full_url[:100]}")
            b64 = page.evaluate("""
                async (url) => {
                    try {
                        const resp = await fetch(url);
                        if (!resp.ok) return null;
                        const blob = await resp.blob();
                        return new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onload = () => resolve(reader.result.split(',')[1]);
                            reader.readAsDataURL(blob);
                        });
                    } catch(e) { return null; }
                }
            """, full_url)

        if not b64:
            # Try PNG
            full_url = re.sub(r'/0_0.*', '/0_0.png', src)
            print(f"Retrying PNG: {full_url[:100]}")
            b64 = page.evaluate("""
                async (url) => {
                    try {
                        const resp = await fetch(url);
                        if (!resp.ok) return null;
                        const blob = await resp.blob();
                        return new Promise((resolve) => {
                            const reader = new FileReader();
                            reader.onload = () => resolve(reader.result.split(',')[1]);
                            reader.readAsDataURL(blob);
                        });
                    } catch(e) { return null; }
                }
            """, full_url)

        if b64:
            data = base64.b64decode(b64)
            ext = ".png" if "png" in full_url else ".webp"
            out = f"/Users/hector/Projects/Dr.-strange/captures/pachano-design/assets/hero-midjourney{ext}"
            with open(out, "wb") as f:
                f.write(data)
            print(f"\nSaved: {len(data)} bytes ({len(data)/1024:.0f} KB) → {out}")
        else:
            print("\nCould not fetch full resolution")

    page.screenshot(path="/tmp/midjourney-fullres.png")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
