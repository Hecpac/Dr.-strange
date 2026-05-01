"""Dump the notebook overview + source titles from NotebookLM via CDP."""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
NOTEBOOK = "81bbc5d7-3eb5-4cb0-8a93-b74aed8ce1af"


def main() -> int:
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        if NOTEBOOK in p.url:
            page = p
            break
    if page is None:
        page = ctx.new_page()
        page.set_viewport_size({"width": 1280, "height": 1400})
        page.goto(f"https://notebooklm.google.com/notebook/{NOTEBOOK}",
                  wait_until="domcontentloaded", timeout=20000)
    page.bring_to_front()

    # Wait for body content to settle.
    for _ in range(40):
        try:
            txt = page.locator("body").inner_text(timeout=2000)
            if "fuentes" in txt and len(txt) > 800:
                break
        except Exception:
            pass
        time.sleep(0.5)

    full_text = page.locator("body").inner_text(timeout=4000)
    print("=== TITLE / TOP CARD ===")
    try:
        title = page.locator("h1").first.inner_text(timeout=2000)
        print(title)
    except Exception:
        print("(no h1)")

    print("\n=== CHAT/CENTER OVERVIEW ===")
    # The overview text appears in the center column. Pull paragraphs.
    try:
        center_paragraphs = page.locator("main p").all()
        for i, p in enumerate(center_paragraphs[:20]):
            try:
                t = p.inner_text(timeout=1500).strip()
                if t and len(t) > 20:
                    print(f"[{i}] {t}")
            except Exception:
                continue
    except Exception as exc:
        print(f"(paragraph fetch failed: {exc})")

    print("\n=== SOURCES (sidebar) ===")
    try:
        page.locator('button:has-text("Seleccionar todo")').first.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    try:
        items = page.locator('aside li, [role="listitem"]').all()
        seen = set()
        for it in items[:60]:
            try:
                t = it.inner_text(timeout=1200).strip()
                if t and t not in seen and len(t) > 4 and len(t) < 220:
                    seen.add(t)
                    print(f"- {t.replace(chr(10), ' | ')}")
            except Exception:
                continue
    except Exception as exc:
        print(f"(source list failed: {exc})")

    page.screenshot(path="/tmp/nblm_summary.png", full_page=True)
    print("\nsnapshot: /tmp/nblm_summary.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
