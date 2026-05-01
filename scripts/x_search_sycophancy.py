"""Connect to Chrome CDP and scrape X searches for sycophancy/goblin tweets.

Reads tweets from search timelines for Anthropic and OpenAI.
"""
from __future__ import annotations

import sys
import time
from urllib.parse import quote

from playwright.sync_api import sync_playwright

QUERIES = [
    ("AnthropicAI", "sycophancy"),
    ("AnthropicAI", "goblin"),
    ("OpenAI", "sycophancy"),
    ("OpenAI", "goblin"),
    ("sama", "sycophancy"),
    ("sama", "goblin"),
]


def search_url(handle: str, term: str) -> str:
    q = quote(f"from:{handle} {term}")
    return f"https://x.com/search?q={q}&src=typed_query&f=live"


def main() -> int:
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp("http://localhost:9250")
    except Exception as exc:
        print(f"CDP_CONNECT_FAILED: {exc}", file=sys.stderr)
        return 2

    if not browser.contexts:
        print("NO_CONTEXTS", file=sys.stderr)
        return 3
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_viewport_size({"width": 1280, "height": 1400})

    for handle, term in QUERIES:
        url = search_url(handle, term)
        print(f"\n=== {handle} / {term} ===")
        print(url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            print(f"GOTO_FAILED: {exc}")
            continue
        time.sleep(3.5)
        try:
            tweets = page.locator("article").all()[:8]
        except Exception as exc:
            print(f"LOCATOR_FAILED: {exc}")
            continue
        if not tweets:
            print("NO_TWEETS_FOUND")
            try:
                title = page.title()
            except Exception:
                title = "?"
            print(f"page_title={title!r}")
            continue
        for i, t in enumerate(tweets):
            try:
                txt = t.inner_text(timeout=4000)
            except Exception:
                continue
            txt = txt.replace("\n", " | ")[:500]
            print(f"  [{i}] {txt}")

    page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
