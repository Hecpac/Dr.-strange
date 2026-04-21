from playwright.sync_api import sync_playwright
import time

PROMPT = "Cinematic wide-angle photograph of a modern architectural masterpiece in a surreal desert landscape. A sleek contemporary house with clean geometric lines sits at the end of a long straight road. Behind it, a massive monolithic amber structure rises into dramatic cumulus clouds lit by golden hour light. Deep teal sky transitioning to warm amber and burnt orange at the horizon. Aspirational, cinematic, premium. Ultra-wide 16:9, photorealistic, architectural photography style. No text, no people --ar 16:9 --v 7 --s 750"

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
        print("ERROR: No Midjourney tab found")
    else:
        # Find the prompt input
        input_el = page.query_selector('textarea, input[type="text"], [contenteditable="true"], [placeholder*="magine"], [placeholder*="rompt"]')
        if input_el:
            input_el.click()
            time.sleep(0.5)
            input_el.fill(PROMPT)
            print("Prompt typed into input field")
        else:
            # Try clicking the prompt area at the top
            # Midjourney has a search/prompt bar at the top
            bars = page.query_selector_all('input, textarea')
            for b in bars:
                ph = b.get_attribute("placeholder") or ""
                visible = b.is_visible()
                print(f"  Input: placeholder='{ph}' visible={visible}")

            # Try the top bar area
            page.keyboard.press("Tab")
            time.sleep(0.3)
            page.keyboard.type(PROMPT, delay=5)
            print("Typed via keyboard")

        time.sleep(2)
        page.screenshot(path="/tmp/midjourney-prompt.png")
        print("Screenshot saved")
except Exception as e:
    print(f"ERROR: {e}")
finally:
    pw.stop()
