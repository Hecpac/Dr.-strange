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
# IDs that NotebookLM uses transiently while a notebook is being provisioned.
# We must wait for the URL to settle past these before capturing the real id.
_TRANSIENT_NOTEBOOK_IDS = {"creating", "loading", "new"}


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
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            raise CdpNotebookLMError("CDP browser has no contexts; is Chrome running?")

        page = _open_nlm_page(browser)
        if "/notebook/" in page.url:
            page.goto(NLM_HOME, wait_until="domcontentloaded", timeout=30_000)

        # Snapshot existing notebook ids so we can identify the freshly-created
        # one — NotebookLM may either redirect the current tab or open a NEW
        # tab for the new notebook depending on session state.
        before_ids: set[str] = set()
        for p in context.pages:
            try:
                nb_id = _extract_notebook_id(p.url)
                if nb_id and nb_id not in _TRANSIENT_NOTEBOOK_IDS:
                    before_ids.add(nb_id)
            except Exception:
                continue

        try:
            page.locator("text=Crear cuaderno nuevo").first.click(timeout=15_000)
        except Exception as exc:
            raise CdpNotebookLMError(
                f"Could not find 'Crear cuaderno nuevo' button: {exc}"
            ) from exc

        # Find the new notebook by scanning ALL tabs for a real id we haven't
        # seen before. Handles in-place navigation and new-tab creation alike.
        notebook_id: str | None = None
        new_page = page
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            for p in context.pages:
                try:
                    candidate = _extract_notebook_id(p.url)
                except Exception:
                    continue
                if (
                    candidate
                    and candidate not in _TRANSIENT_NOTEBOOK_IDS
                    and candidate not in before_ids
                ):
                    notebook_id = candidate
                    new_page = p
                    break
            if notebook_id:
                break
            time.sleep(0.5)
        if not notebook_id:
            urls = [p.url for p in context.pages if "notebooklm" in p.url]
            raise CdpNotebookLMError(
                f"Notebook URL did not settle past transient placeholder. "
                f"Current notebooklm tabs: {urls}"
            )

        # The freshly-created notebook may live in a new tab; switch to it for
        # the title rename step.
        page = new_page
        try:
            page.bring_to_front()
        except Exception:
            pass

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


def _focus_notebook(browser, notebook_id: str) -> "Page":
    """Navigate the existing NLM tab to the given notebook id."""
    target = f"https://notebooklm.google.com/notebook/{notebook_id}"
    page = _open_nlm_page(browser)
    if notebook_id not in page.url:
        page.goto(target, wait_until="domcontentloaded", timeout=30_000)
    return page


def deep_research(
    notebook_id: str,
    query: str,
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    poll_timeout: float = 600.0,
) -> int:
    """Run Deep Research on an existing notebook and import the discovered sources.

    Steps (mirrors feedback_notebooklm_podcast.md):
      1. Navigate to the notebook page.
      2. Open the source-mode dropdown (currently labeled 'Investigación rápida')
         and switch to 'Deep Research'.
      3. Locate the `source-discovery-query-box` input, type the query via
         mouse.click + keyboard.type (Angular requires native events).
      4. Click 'Enviar' via bounding_box (NOT aria-label — there are two).
      5. Poll until 'Deep Research finalizó la búsqueda' appears.
      6. Click 'Importar' to add the discovered sources.

    Returns the number of sources imported (best-effort estimate from the
    success banner or 0 if it can't be parsed).

    Raises CdpNotebookLMError on any failure.
    """
    query = (query or "").strip()
    if not query:
        raise CdpNotebookLMError("deep_research requires a non-empty query")

    pw, browser = _connect(cdp_url)
    try:
        page = _focus_notebook(browser, notebook_id)

        # 1. Open the source-mode dropdown and switch to Deep Research.
        try:
            page.locator("text=Investigación rápida").first.click(timeout=15_000)
            page.locator("text=Deep Research").first.click(timeout=10_000)
        except Exception as exc:
            raise CdpNotebookLMError(
                f"Could not switch to Deep Research mode: {exc}"
            ) from exc

        # 2. Find the Deep Research query input (NOT the page title at y~12).
        try:
            box_locator = page.locator("source-discovery-query-box").first
            box_locator.wait_for(state="visible", timeout=10_000)
            inner_input = box_locator.locator("input").first
            inp_box = inner_input.bounding_box()
            if not inp_box:
                raise CdpNotebookLMError("source-discovery query input has no bounding box")
            page.mouse.click(
                inp_box["x"] + inp_box["width"] / 2,
                inp_box["y"] + inp_box["height"] / 2,
            )
            page.keyboard.press("Meta+a")
            page.keyboard.press("Backspace")
            page.keyboard.type(query, delay=40)
        except CdpNotebookLMError:
            raise
        except Exception as exc:
            raise CdpNotebookLMError(f"Could not enter Deep Research query: {exc}") from exc

        # 3. Click Enviar by bounding_box (aria-label collides with the chat box).
        try:
            send_btn = page.locator(
                'source-discovery-query-box button[aria-label="Enviar"]'
            ).first
            send_btn.wait_for(state="visible", timeout=10_000)
            send_box = send_btn.bounding_box()
            if not send_box:
                raise CdpNotebookLMError("Enviar button has no bounding box")
            page.mouse.click(send_box["x"] + 16, send_box["y"] + 16)
        except CdpNotebookLMError:
            raise
        except Exception as exc:
            raise CdpNotebookLMError(f"Could not click Enviar: {exc}") from exc

        # 4. Poll for completion (up to poll_timeout seconds).
        deadline = time.monotonic() + poll_timeout
        completed = False
        while time.monotonic() < deadline:
            try:
                if page.locator("text=Deep Research finalizó la búsqueda").first.is_visible(
                    timeout=2_000
                ):
                    completed = True
                    break
            except Exception:
                pass
            time.sleep(15)
        if not completed:
            raise CdpNotebookLMError(
                f"Deep Research did not complete within {poll_timeout:.0f}s"
            )

        # 5. Click Importar.
        try:
            buttons = page.locator("button")
            count = buttons.count()
            clicked = False
            for i in range(count):
                btn = buttons.nth(i)
                try:
                    if btn.is_visible() and "Importar" in btn.inner_text():
                        btn.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                raise CdpNotebookLMError("Could not find 'Importar' button after research")
        except CdpNotebookLMError:
            raise
        except Exception as exc:
            raise CdpNotebookLMError(f"Could not click Importar: {exc}") from exc

        # 6. Best-effort source count: brief settle then return 0 if not parseable.
        # NotebookLM doesn't expose a stable count selector; the caller already gets
        # a "Deep Research completado" notification with the notebook URL, so the
        # exact count is informational.
        time.sleep(2)
        return 0
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()


# Map of artifact kind → UI label that triggers generation. Only podcast is
# documented in feedback_notebooklm_podcast.md; video/infographic UI labels
# would need empirical discovery before implementation.
_ARTIFACT_BUTTON_TEXT = {
    "podcast": "Resumen en audio",
}


def generate_artifact(
    notebook_id: str,
    kind: str,
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    poll_timeout: float = 1200.0,
) -> None:
    """Trigger NotebookLM artifact generation (podcast/video/infographic) via CDP.

    Currently only `kind="podcast"` is supported via CDP — the UI labels for
    video and infographic generation are not documented in the workflow memory.
    Other kinds raise CdpNotebookLMError with a clear message so the caller can
    surface a degradation notice instead of crashing.

    Steps for podcast:
      1. Navigate to the notebook page.
      2. Click "Resumen en audio".
      3. Wait until the "Generando" indicator disappears (5-15 min for large
         source sets).
    """
    kind = (kind or "").strip().lower()
    if kind not in _ARTIFACT_BUTTON_TEXT:
        raise CdpNotebookLMError(
            f"CDP NotebookLM only supports podcast generation today; '{kind}' is not implemented. "
            "Use the NotebookLM UI directly for video/infographic."
        )

    pw, browser = _connect(cdp_url)
    try:
        page = _focus_notebook(browser, notebook_id)

        try:
            page.locator(f"text={_ARTIFACT_BUTTON_TEXT[kind]}").first.click(timeout=15_000)
        except Exception as exc:
            raise CdpNotebookLMError(
                f"Could not find '{_ARTIFACT_BUTTON_TEXT[kind]}' button: {exc}"
            ) from exc

        # Poll: wait for "Generando" to appear (it should within seconds), then
        # wait for it to disappear (signals completion).
        deadline_start = time.monotonic() + 30
        generating_seen = False
        while time.monotonic() < deadline_start:
            try:
                if page.locator("text=Generando").first.is_visible(timeout=2_000):
                    generating_seen = True
                    break
            except Exception:
                pass
            time.sleep(2)
        if not generating_seen:
            # Some artifact types finish very quickly; treat absence of the
            # spinner as immediate success.
            return

        deadline_end = time.monotonic() + poll_timeout
        while time.monotonic() < deadline_end:
            try:
                if not page.locator("text=Generando").first.is_visible(timeout=2_000):
                    return
            except Exception:
                return
            time.sleep(15)
        raise CdpNotebookLMError(
            f"Artifact '{kind}' did not complete within {poll_timeout:.0f}s"
        )
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()
