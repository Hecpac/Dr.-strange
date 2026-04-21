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

    # The grid of 4 images is in the first job card at the top
    # Bottom-right is the 4th image in the grid
    # First, let's click on the job/grid to open it
    # The images are typically in a grid - click the main image area
    imgs = page.query_selector_all('img[alt]')
    job_imgs = []
    for img in imgs:
        src = img.get_attribute("src") or ""
        alt = img.get_attribute("alt") or ""
        visible = img.is_visible()
        if visible and ("cdn" in src or "midjourney" in src.lower()):
            box = img.bounding_box()
            if box and box["y"] < 300:  # Top area where new job is
                job_imgs.append((img, box))
                print(f"  Found img at y={box['y']:.0f} x={box['x']:.0f} w={box['width']:.0f} h={box['height']:.0f}")

    if job_imgs:
        # Click the first/main image to open the job detail
        img, box = job_imgs[0]
        img.click()
        print(f"Clicked job image")
        time.sleep(3)
        page.screenshot(path="/tmp/midjourney-jobdetail.png")
        print("Screenshot saved")
    else:
        # Alternative: just click the top area where the new generation appears
        print("No CDN images found, trying click on grid area")
        page.mouse.click(200, 150)
        time.sleep(3)
        page.screenshot(path="/tmp/midjourney-jobdetail.png")
        print("Screenshot saved via area click")

    print(f"URL: {page.url}")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
