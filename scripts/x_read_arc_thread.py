"""Open the ARC Prize tweet thread and scrape replies from the same author."""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
TWEET_URL = "https://x.com/arcprize/status/2050261221165989969"


def main() -> int:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_viewport_size({"width": 1280, "height": 1800})
    page.goto(TWEET_URL, wait_until="domcontentloaded", timeout=25000)
    time.sleep(5)

    # Scroll a few times to load thread replies
    for _ in range(8):
        page.mouse.wheel(0, 1500)
        time.sleep(1.2)

    arts = page.locator("article").all()
    print(f"=== {len(arts)} articles loaded ===\n")
    for i, a in enumerate(arts[:25]):
        try:
            t = a.inner_text(timeout=4000)
        except Exception:
            continue
        # Only show tweets that mention the same handle (thread author)
        if "@arcprize" in t.lower() or "ARC Prize" in t:
            print(f"--- [{i}] ---")
            print(t.replace("\n", " | ")[:900])
            print()
    page.screenshot(path="/tmp/arc_thread.png", full_page=True)
    print("snapshot: /tmp/arc_thread.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
