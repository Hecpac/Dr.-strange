"""Chrome CDP automation for NotebookLM (replaces the missing notebooklm-py SDK).

Workflow documented in memory file `feedback_notebooklm_podcast.md`. Each public
method is a self-contained sync operation that connects via Playwright sync over
CDP at the URL configured for the local Chrome (default port 9250 — see
`com.claw.chrome-cdp.plist`).

These functions are blocking (CDP work is inherently sequential). Long-running
flows (Deep Research, podcast generation) are expected to be wrapped in a
background thread by the caller.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)

DEFAULT_CDP_URL = "http://localhost:9250"
NLM_HOME = "https://notebooklm.google.com/"
_NOTEBOOK_URL_RE = re.compile(r"notebooklm\.google\.com/notebook/([A-Za-z0-9_-]+)")


class CdpNotebookLMError(RuntimeError):
    """Raised when a CDP NotebookLM operation cannot complete."""


def _connect(cdp_url: str = DEFAULT_CDP_URL):
    """Open a Playwright sync_playwright context and return (pw, browser).

    Caller is responsible for closing both. Mirrors the connect logic in
    claw_v2/browser.py:_cdp_connect but kept local to avoid an import cycle
    with DevBrowserService (which depends on this module being independent).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CdpNotebookLMError(
            "playwright is not installed; cannot use CDP NotebookLM workflow."
        ) from exc

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=10_000)
    except Exception as exc:
        pw.stop()
        raise CdpNotebookLMError(f"CDP connect failed at {cdp_url}: {exc}") from exc
    return pw, browser


def _open_nlm_page(browser) -> "Page":
    """Reuse an existing NotebookLM tab or open a fresh one on the home page."""
    context = browser.contexts[0] if browser.contexts else None
    if context is None:
        raise CdpNotebookLMError("CDP browser has no contexts; is Chrome running?")

    for page in context.pages:
        try:
            if "notebooklm.google.com" in page.url:
                page.bring_to_front()
                return page
        except Exception:
            continue

    page = context.new_page()
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(NLM_HOME, wait_until="domcontentloaded", timeout=30_000)
    return page


def _extract_notebook_id(url: str) -> str | None:
    match = _NOTEBOOK_URL_RE.search(url)
    return match.group(1) if match else None


def create_notebook(title: str, *, cdp_url: str = DEFAULT_CDP_URL) -> dict:
    """Create a new NotebookLM notebook via CDP and return {id, title}.

    Steps:
      1. Open or focus the NotebookLM home page.
      2. Click "Crear cuaderno nuevo".
      3. Wait for navigation to /notebook/{id}.
      4. Set the title (the notebook is created with a placeholder name).
      5. Extract the id from the URL.

    Raises CdpNotebookLMError if any step fails.
    """
    title = (title or "").strip()
    if not title:
        raise CdpNotebookLMError("create_notebook requires a non-empty title")

    pw, browser = _connect(cdp_url)
    try:
        page = _open_nlm_page(browser)
        if "/notebook/" in page.url:
            page.goto(NLM_HOME, wait_until="domcontentloaded", timeout=30_000)

        try:
            page.locator("text=Crear cuaderno nuevo").first.click(timeout=15_000)
        except Exception as exc:
            raise CdpNotebookLMError(
                f"Could not find 'Crear cuaderno nuevo' button: {exc}"
            ) from exc

        try:
            page.wait_for_url(_NOTEBOOK_URL_RE, timeout=30_000)
        except Exception as exc:
            raise CdpNotebookLMError(
                f"Notebook URL did not appear after click: {exc}"
            ) from exc

        notebook_id = _extract_notebook_id(page.url)
        if not notebook_id:
            raise CdpNotebookLMError(f"Could not parse notebook id from URL {page.url}")

        # Set the title. The first text input on the notebook page (~y=12) is the title.
        # Use mouse.click + keyboard.type to satisfy Angular change detection.
        try:
            title_input = page.locator("input").first
            title_input.wait_for(state="visible", timeout=10_000)
            box = title_input.bounding_box()
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.keyboard.press("Meta+a")
                page.keyboard.press("Backspace")
                page.keyboard.type(title, delay=40)
                page.keyboard.press("Tab")  # commit the title
                # Brief settle so Angular persists the rename before we navigate away.
                time.sleep(0.5)
        except Exception:
            logger.warning(
                "Could not rename notebook %s to %r; notebook exists with default title",
                notebook_id, title, exc_info=True,
            )

        return {"id": notebook_id, "title": title}
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()
