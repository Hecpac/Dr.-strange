#!/usr/bin/env python3
"""Download full-res Midjourney hero image."""
from playwright.sync_api import sync_playwright
import base64, os

OUTPUT_DIR = "/Users/hector/Projects/Dr.-strange/captures"
os.makedirs(OUTPUT_DIR, exist_ok=True)

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
        exit(1)

    print(f"URL: {page.url}")

    # Find all image URLs on the page to pick the full-res one
    all_urls = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            const urls = [];
            for (const img of imgs) {
                if (img.src.includes('cdn.midjourney.com')) {
                    const rect = img.getBoundingClientRect();
                    urls.push({
                        src: img.src,
                        srcset: img.srcset || '',
                        w: rect.width,
                        h: rect.height,
                        area: rect.width * rect.height
                    });
                }
            }
            // Also check for any download links/anchors
            const links = document.querySelectorAll('a[href*="cdn.midjourney.com"]');
            for (const a of links) {
                urls.push({src: a.href, srcset: '', w: 0, h: 0, area: 0, isLink: true});
            }
            return urls.sort((a, b) => b.area - a.area);
        }
    """)

    for u in all_urls:
        print(f"  area={u['area']:.0f} src={u['src'][:150]}")

    # Get the largest image and construct full-res URL
    if not all_urls:
        print("No CDN images found")
        exit(1)

    thumb_url = all_urls[0]["src"]
    # Midjourney full-res: replace quality param and size indicator
    # Pattern: /jobid/0_0_384_N.webp?... -> /jobid/0_0.png (full res)
    job_id = "9b1d0685-5c2c-48f6-a3c4-86c260e0040d"
    full_url = f"https://cdn.midjourney.com/{job_id}/0_0.png"
    print(f"\nTrying full-res: {full_url}")

    result = page.evaluate("""
        async (url) => {
            try {
                const resp = await fetch(url);
                if (!resp.ok) return {error: resp.status};
                const blob = await resp.blob();
                const arrayBuf = await blob.arrayBuffer();
                const arr = new Uint8Array(arrayBuf);
                let binary = '';
                const chunk = 8192;
                for (let i = 0; i < arr.length; i += chunk) {
                    const slice = arr.subarray(i, i + chunk);
                    binary += String.fromCharCode.apply(null, slice);
                }
                return {ok: true, b64: btoa(binary), size: arr.length, type: blob.type};
            } catch(e) {
                return {error: e.message};
            }
        }
    """, full_url)

    if result.get("error"):
        print(f"Full-res error: {result['error']}")
        # Try webp full quality
        full_url2 = f"https://cdn.midjourney.com/{job_id}/0_0.webp?method=shortest&qst=6&quality=100"
        print(f"Trying HQ webp: {full_url2}")
        result = page.evaluate("""
            async (url) => {
                try {
                    const resp = await fetch(url);
                    if (!resp.ok) return {error: resp.status};
                    const blob = await resp.blob();
                    const arrayBuf = await blob.arrayBuffer();
                    const arr = new Uint8Array(arrayBuf);
                    let binary = '';
                    const chunk = 8192;
                    for (let i = 0; i < arr.length; i += chunk) {
                        const slice = arr.subarray(i, i + chunk);
                        binary += String.fromCharCode.apply(null, slice);
                    }
                    return {ok: true, b64: btoa(binary), size: arr.length, type: blob.type};
                } catch(e) {
                    return {error: e.message};
                }
            }
        """, full_url2)

    if result.get("error"):
        print(f"Error: {result['error']}")
        exit(1)

    data = base64.b64decode(result["b64"])
    ext = "webp" if "webp" in result.get("type", "") else "png"
    outpath = os.path.join(OUTPUT_DIR, f"hero-desert-house.{ext}")
    with open(outpath, "wb") as f:
        f.write(data)
    print(f"Downloaded: {outpath} ({len(data)/1024:.1f} KB, type={result.get('type')})")

except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
