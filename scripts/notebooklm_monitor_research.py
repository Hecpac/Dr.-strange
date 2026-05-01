"""Monitor a Deep Research run on NotebookLM and click Importar when done.

Usage: ./.venv/bin/python scripts/notebooklm_monitor_research.py [notebook_id]

Connects to the existing Chrome CDP session, navigates to the notebook, polls
the page for completion signals (the Importar button becoming visible, or
"finalizó"/"Listo" text). Returns 0 when sources were imported, 10 when still
running, 11 if no progress was visible.
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
DEFAULT_NOTEBOOK = "81bbc5d7-3eb5-4cb0-8a93-b74aed8ce1af"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    notebook_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_NOTEBOOK
    target_url = f"https://notebooklm.google.com/notebook/{notebook_id}"

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        log(f"CDP_CONNECT_FAILED: {exc}")
        return 2
    if not browser.contexts:
        log("NO_CONTEXTS")
        return 3
    ctx = browser.contexts[0]

    page = None
    for p in ctx.pages:
        try:
            if notebook_id in p.url:
                page = p
                break
        except Exception:
            continue
    if page is None:
        page = ctx.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
    page.bring_to_front()
    time.sleep(2.5)

    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=4000)
    except Exception as exc:
        log(f"could not read body: {exc}")

    states = {
        "planning": "Planificando" in body_text,
        "researching": "Investigando" in body_text or "researching" in body_text.lower(),
        "ready": "Listo" in body_text or "finalizó" in body_text or "Importar" in body_text,
    }
    log(f"signals: {states}")

    page.screenshot(path="/tmp/nblm_monitor.png", full_page=True)
    log("snapshot: /tmp/nblm_monitor.png")

    # If Importar is visible, click it.
    importar_clicked = False
    for sel in [
        'button:has-text("Importar")',
        'button:has-text("Import")',
    ]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible():
                btn.click()
                log(f"clicked Importar via {sel!r}")
                importar_clicked = True
                break
        except Exception:
            continue

    if importar_clicked:
        time.sleep(8.0)
        page.screenshot(path="/tmp/nblm_monitor_imported.png", full_page=True)
        log("snapshot: /tmp/nblm_monitor_imported.png")
        log("STATE=IMPORTED")
        return 0

    if states["planning"] or states["researching"]:
        log("STATE=RUNNING")
        return 10

    if states["ready"]:
        log("STATE=READY_BUT_NO_BUTTON")
        return 12

    log("STATE=UNKNOWN")
    return 11


if __name__ == "__main__":
    raise SystemExit(main())
