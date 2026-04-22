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

    if not page:
        print("ERROR: No Midjourney tab")
        exit()

    # Get viewport size
    vp = page.viewport_size
    print(f"Viewport: {vp}")

    # Find all clickable image areas in the top section
    # The 4-image grid in Midjourney is usually the first job
    # Let me find all img elements and their bounding boxes
    all_imgs = page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img');
            return Array.from(imgs).map(img => ({
                src: (img.src || '').substring(0, 80),
                alt: img.alt || '',
                x: img.getBoundingClientRect().x,
                y: img.getBoundingClientRect().y,
                w: img.getBoundingClientRect().width,
                h: img.getBoundingClientRect().height,
                visible: img.offsetParent !== null
            })).filter(i => i.visible && i.w > 50 && i.h > 50);
        }
    """)

    print(f"\nVisible images (>50px):")
    for img in all_imgs:
        print(f"  x={img['x']:.0f} y={img['y']:.0f} w={img['w']:.0f}x{img['h']:.0f} src={img['src'][:60]}")

    # Find the 4 grid images (should be in the top area, similar sizes)
    grid_imgs = [i for i in all_imgs if i['y'] < 400 and i['w'] > 100]
    grid_imgs.sort(key=lambda i: (i['y'], i['x']))

    print(f"\nGrid candidates: {len(grid_imgs)}")
    for i, img in enumerate(grid_imgs):
        print(f"  [{i}] x={img['x']:.0f} y={img['y']:.0f} w={img['w']:.0f}x{img['h']:.0f}")

    if len(grid_imgs) >= 4:
        # Bottom-right is the last one (index 3)
        target = grid_imgs[3]
        cx = target['x'] + target['w'] / 2
        cy = target['y'] + target['h'] / 2
        print(f"\nClicking bottom-right at ({cx:.0f}, {cy:.0f})")
        page.mouse.click(cx, cy)
        time.sleep(3)
        page.screenshot(path="/tmp/midjourney-selected.png")
        print("Screenshot saved")
    else:
        print("Could not identify 4-image grid")

except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
