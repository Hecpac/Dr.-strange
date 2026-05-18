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
_SOURCE_COUNT_PATTERNS = (
    re.compile(r"\b(?:fuentes|sources)\s*[\n\r: ]+\(?\s*(\d{1,4})\s*\)?", re.IGNORECASE),
    re.compile(r"\(?\s*(\d{1,4})\s*\)?\s+(?:fuentes|sources)\b", re.IGNORECASE),
)
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
    try:
        page.set_viewport_size({"width": 1280, "height": 900})
    except Exception:
        pass
    page.goto(NLM_HOME, wait_until="domcontentloaded", timeout=30_000)
    return page


def _extract_notebook_id(url: str) -> str | None:
    match = _NOTEBOOK_URL_RE.search(url)
    return match.group(1) if match else None


def _parse_source_count(text: str) -> int | None:
    collapsed = re.sub(r"[ \t]+", " ", str(text or ""))
    if not collapsed.strip():
        return None
    for pattern in _SOURCE_COUNT_PATTERNS:
        match = pattern.search(collapsed)
        if not match:
            continue
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            continue
    return None


def _notebook_source_count(page) -> int | None:
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return None
    return _parse_source_count(body_text)


def _wait_for_verified_sources(page, *, before_count: int | None, timeout: float = 60.0) -> int:
    baseline = before_count if before_count is not None else -1
    deadline = time.monotonic() + timeout
    last_count: int | None = None
    while time.monotonic() < deadline:
        count = _notebook_source_count(page)
        if count is not None:
            last_count = count
            if count > 0 and count > baseline:
                return count
        time.sleep(2)
    raise CdpNotebookLMError(
        "Deep Research import did not verify imported sources "
        f"(before={before_count}, after={last_count})."
    )


def _notebook_ids_from_home_grid(page, *, limit: int = 60) -> set[str]:
    ids: set[str] = set()
    try:
        cards = page.locator('a[href*="/notebook/"]')
        count = min(cards.count(), limit)
    except Exception:
        return ids
    for i in range(count):
        try:
            href = cards.nth(i).get_attribute("href") or ""
        except Exception:
            continue
        nb_id = _extract_notebook_id(href)
        if nb_id and nb_id not in _TRANSIENT_NOTEBOOK_IDS:
            ids.add(nb_id)
    return ids


def _click_create_notebook(page) -> None:
    factories = (
        lambda: page.get_by_role(
            "button",
            name=re.compile(r"(crear cuaderno nuevo|create new notebook)", re.IGNORECASE),
        ),
        lambda: page.locator('button:has-text("Crear cuaderno nuevo")'),
        lambda: page.locator('button:has-text("Create new notebook")'),
        lambda: page.locator("text=Crear cuaderno nuevo"),
        lambda: page.locator("text=Create new notebook"),
    )
    last_exc: Exception | None = None
    for factory in factories:
        try:
            factory().first.click(timeout=8_000)
            return
        except Exception as exc:
            last_exc = exc
    raise CdpNotebookLMError(
        f"Could not find NotebookLM create button: {last_exc}"
    )


def _scan_home_for_new_id(page, before_ids: set[str], *, limit: int = 30) -> str | None:
    try:
        page.goto(NLM_HOME, wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        return None
    time.sleep(1.5)
    try:
        cards = page.locator('a[href*="/notebook/"]')
        count = min(cards.count(), limit)
    except Exception:
        return None
    for i in range(count):
        try:
            href = cards.nth(i).get_attribute("href") or ""
        except Exception:
            continue
        candidate = _extract_notebook_id(href)
        if (
            candidate
            and candidate not in _TRANSIENT_NOTEBOOK_IDS
            and candidate not in before_ids
        ):
            return candidate
    return None


def list_notebooks(*, cdp_url: str = DEFAULT_CDP_URL, limit: int = 60) -> list[dict]:
    """Enumerate notebooks visible on the NotebookLM home grid.

    Returns a list of {id, title, created_at} dicts, ordered most-recent first
    when NotebookLM exposes that ordering (the default home view does).
    Each card on the home page is a link to /notebook/<id> with a visible
    title and a relative date label like "May 4, 2026".
    """
    pw, browser = _connect(cdp_url)
    try:
        page = _open_nlm_page(browser)
        if "/notebook/" in page.url:
            try:
                page.goto(NLM_HOME, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                pass
        # Wait a moment for the grid to hydrate.
        time.sleep(1.5)
        cards = page.locator('a[href*="/notebook/"]')
        count = min(cards.count(), limit)
        seen: set[str] = set()
        notebooks: list[dict] = []
        for i in range(count):
            try:
                href = cards.nth(i).get_attribute("href") or ""
            except Exception:
                continue
            nb_id = _extract_notebook_id(href)
            if not nb_id or nb_id in _TRANSIENT_NOTEBOOK_IDS or nb_id in seen:
                continue
            seen.add(nb_id)
            try:
                text = (cards.nth(i).inner_text(timeout=1500) or "").strip()
            except Exception:
                text = ""
            title = text.split("\n")[0].strip() if text else nb_id[:8]
            created_at = ""
            for line in text.split("\n")[1:]:
                line = line.strip()
                if not line:
                    continue
                if "fuente" in line.lower() or "source" in line.lower():
                    continue
                created_at = line
                break
            notebooks.append({"id": nb_id, "title": title, "created_at": created_at})
        return notebooks
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


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
        before_ids.update(_notebook_ids_from_home_grid(page))

        _click_create_notebook(page)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:
            pass
        time.sleep(1)

        # Find the new notebook by scanning ALL tabs for a real id we haven't
        # seen before. Handles in-place navigation and new-tab creation alike.
        notebook_id: str | None = None
        new_page = page
        deadline = time.monotonic() + 120
        reloads_done = 0
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
            elapsed = 120 - (deadline - time.monotonic())
            if reloads_done < 2 and elapsed > 30 + 45 * reloads_done:
                for p in context.pages:
                    if "/notebook/creating" in p.url:
                        try:
                            p.reload(wait_until="domcontentloaded", timeout=15_000)
                        except Exception:
                            pass
                        reloads_done += 1
                        break
                else:
                    reloads_done += 1
            time.sleep(0.5)
        if not notebook_id:
            fallback_id = _scan_home_for_new_id(page, before_ids)
            if fallback_id:
                notebook_id = fallback_id
                target = f"https://notebooklm.google.com/notebook/{fallback_id}"
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=20_000)
                    new_page = page
                except Exception:
                    logger.warning(
                        "Notebook %s found via home-grid fallback but navigation failed",
                        fallback_id,
                    )
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


# Deep Research lifecycle states observable on the source-discovery panel.
DR_STATE_READY_TO_IMPORT = "ready_to_import"
DR_STATE_IN_PROGRESS = "in_progress"
DR_STATE_SUBMITTABLE = "submittable"
DR_STATE_COMPLETED = "completed"
DR_STATE_UNKNOWN = "unknown"


def detect_deep_research_state(page) -> str:
    """Read the notebook DOM and classify the Deep Research panel state.

    Returns one of DR_STATE_*. This is the gate that prevents premature
    re-submission when a previous DR run is still in flight or finished
    and only waiting on the Importar click. Today's failure mode was
    misreading a disabled textarea (aria-label says "consulta enviada",
    i.e. past tense) as "not submitted yet" and starting over.
    """
    try:
        body = page.locator("body").inner_text(timeout=4_000)
    except Exception:
        body = ""
    body_low = body.lower()

    # 1. If sources already imported, we are done.
    count = _parse_source_count(body)
    if count is not None and count > 0:
        return DR_STATE_COMPLETED

    # 2. Results panel surfaced — "Deep Research finalizó la búsqueda".
    if "deep research finalizó" in body_low or "deep research finished" in body_low:
        return DR_STATE_READY_TO_IMPORT

    # 3. Mid-flight indicators visible.
    progress_markers = (
        "planificando",
        "investigando",
        "no salgas de esta página",
        "do not leave this page",
    )
    if any(m in body_low for m in progress_markers):
        return DR_STATE_IN_PROGRESS

    # 4. Query textarea state: aria-label "consulta enviada" + disabled means
    # a query is already submitted (mid-flight or pending results render).
    try:
        ta = page.locator(
            'mat-dialog-container textarea[aria-label*="consulta enviada"], '
            'source-discovery-query-box textarea[aria-label*="consulta enviada"]'
        ).first
        if ta.is_visible(timeout=1_500):
            disabled = ta.evaluate(
                "el => el.disabled || el.readOnly || el.getAttribute('aria-disabled')==='true'"
            )
            if disabled:
                return DR_STATE_IN_PROGRESS
    except Exception:
        pass

    # 5. Importar button visible without explicit "finalizó" text — treat as ready.
    try:
        importar = page.locator('button:has-text("Importar")').first
        if importar.is_visible(timeout=1_500):
            return DR_STATE_READY_TO_IMPORT
    except Exception:
        pass

    # 6. Empty notebook welcome with no DR markers means we can submit fresh.
    if "iniciemos tu cuaderno" in body_low or "let's get started" in body_low:
        return DR_STATE_SUBMITTABLE

    return DR_STATE_UNKNOWN


def _click_importar(page) -> bool:
    """Click the Importar button if visible. Returns True if clicked."""
    for sel in (
        'button:has-text("Importar")',
        '[role="button"]:has-text("Importar")',
        'mat-dialog-container button:has-text("Importar")',
    ):
        try:
            loc = page.locator(sel)
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            try:
                btn = loc.nth(i)
                if not btn.is_visible(timeout=1_500):
                    continue
                disabled = False
                try:
                    disabled = btn.evaluate(
                        "el => el.disabled || el.getAttribute('aria-disabled')==='true'"
                    )
                except Exception:
                    pass
                if disabled:
                    continue
                btn.click()
                return True
            except Exception:
                continue
    return False


def _wait_for_deep_research_completion(page, *, poll_timeout: float) -> None:
    """Poll until 'Deep Research finalizó' or Importar surfaces."""
    deadline = time.monotonic() + poll_timeout
    while time.monotonic() < deadline:
        state = detect_deep_research_state(page)
        if state in (DR_STATE_READY_TO_IMPORT, DR_STATE_COMPLETED):
            return
        time.sleep(15)
    raise CdpNotebookLMError(
        f"Deep Research did not complete within {poll_timeout:.0f}s"
    )


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
        before_source_count = _notebook_source_count(page)

        # 0. Idempotency gate: if a previous DR run for this notebook is mid-flight
        # or already finished and waiting on Importar, do NOT re-submit.
        # Re-submitting would either fail silently (textarea is disabled while
        # aria-label is "consulta enviada") or destroy completed results.
        state = detect_deep_research_state(page)
        if state == DR_STATE_COMPLETED:
            count = _notebook_source_count(page) or 0
            logger.info("deep_research: notebook already has %s sources, skipping", count)
            return count
        if state in (DR_STATE_IN_PROGRESS, DR_STATE_READY_TO_IMPORT):
            logger.info(
                "deep_research: prior run detected (state=%s); skipping submit and resuming",
                state,
            )
            if state == DR_STATE_IN_PROGRESS:
                _wait_for_deep_research_completion(page, poll_timeout=poll_timeout)
            if not _click_importar(page):
                raise CdpNotebookLMError(
                    "Could not click 'Importar' on existing Deep Research results"
                )
            return _wait_for_verified_sources(page, before_count=before_source_count)

        # 1. Open the source-mode dropdown and switch to Deep Research.
        try:
            page.locator("text=Fast Research").first.click(timeout=15_000)
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

        # 4. Poll for completion using the shared state detector.
        _wait_for_deep_research_completion(page, poll_timeout=poll_timeout)

        # 5. Click Importar via shared helper.
        if not _click_importar(page):
            raise CdpNotebookLMError("Could not find 'Importar' button after research")

        # 6. Completion is not enough: verify that sources actually landed in
        # the notebook. This prevents transient "planning/completed" UI states
        # from being reported as a successful research run while the notebook
        # remains empty.
        return _wait_for_verified_sources(page, before_count=before_source_count)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        pw.stop()


def resume_deep_research(
    notebook_id: str,
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    poll_timeout: float = 600.0,
) -> dict:
    """Inspect an existing notebook's Deep Research panel and finish whatever
    step is pending: wait → click Importar → verify sources.

    Idempotent: callable repeatedly. Never submits a new query. Returns
    {"state": <DR_STATE_*>, "sources_imported": int|None}.
    """
    pw, browser = _connect(cdp_url)
    try:
        page = _focus_notebook(browser, notebook_id)
        before = _notebook_source_count(page)
        state = detect_deep_research_state(page)
        if state == DR_STATE_COMPLETED:
            return {"state": state, "sources_imported": before or 0}
        if state == DR_STATE_IN_PROGRESS:
            _wait_for_deep_research_completion(page, poll_timeout=poll_timeout)
            state = DR_STATE_READY_TO_IMPORT
        if state == DR_STATE_READY_TO_IMPORT:
            if not _click_importar(page):
                raise CdpNotebookLMError(
                    "Could not click 'Importar' on existing Deep Research results"
                )
            count = _wait_for_verified_sources(page, before_count=before)
            return {"state": DR_STATE_COMPLETED, "sources_imported": count}
        return {"state": state, "sources_imported": before}
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
