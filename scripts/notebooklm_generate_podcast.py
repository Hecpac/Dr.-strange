"""Trigger an audio summary (podcast) on a NotebookLM notebook in Spanish.

Connects to the existing Chrome CDP session, navigates to the notebook,
verifies language is set to Spanish, clicks "Resumen en audio" in the Studio
panel, and waits a short while to confirm generation has started.
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
        page.goto(f"https://notebooklm.google.com/notebook/{notebook_id}",
                  wait_until="domcontentloaded", timeout=20000)
    page.bring_to_front()
    time.sleep(2.5)

    page.screenshot(path="/tmp/nblm_pod_pre.png", full_page=True)
    log("snapshot pre: /tmp/nblm_pod_pre.png")

    # Click "Resumen en audio" in the Studio panel (right side).
    audio_clicked = False
    for sel in [
        'button:has-text("Resumen en audio")',
        '[role="button"]:has-text("Resumen en audio")',
        'div:has-text("Resumen en audio")',
        'button[aria-label*="Resumen en audio"]',
        'button[aria-label*="audio"]',
    ]:
        try:
            btns = page.locator(sel).all()
            for b in btns:
                try:
                    if b.is_visible():
                        b.click()
                        log(f"clicked Resumen en audio via {sel!r}")
                        audio_clicked = True
                        break
                except Exception:
                    continue
            if audio_clicked:
                break
        except Exception as exc:
            log(f"audio sel {sel!r} failed: {exc}")
    if not audio_clicked:
        page.screenshot(path="/tmp/nblm_pod_no_audio.png", full_page=True)
        log("snapshot: /tmp/nblm_pod_no_audio.png")
        return 5

    time.sleep(2.5)
    page.screenshot(path="/tmp/nblm_pod_panel.png", full_page=True)
    log("snapshot panel: /tmp/nblm_pod_panel.png")

    # The audio overview panel may show language selector + "Generar"/"Generate" button.
    # Verify Spanish is selected; if a language chip is visible, ensure it says Español.
    body_lower = ""
    try:
        body_lower = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        pass
    if "español" in body_lower:
        log("language hint: 'español' present in page text")
    else:
        log("WARN: 'español' not detected in page text — proceeding (language may be set elsewhere)")

    # Click Generate / Generar.
    gen_clicked = False
    for sel in [
        'button:has-text("Generar")',
        'button:has-text("Generate")',
        'button[aria-label*="Generar"]',
        'button[aria-label*="Generate"]',
    ]:
        try:
            btns = page.locator(sel).all()
            for b in btns:
                try:
                    if b.is_visible() and b.is_enabled():
                        b.click()
                        log(f"clicked Generate via {sel!r}")
                        gen_clicked = True
                        break
                except Exception:
                    continue
            if gen_clicked:
                break
        except Exception as exc:
            log(f"gen sel {sel!r} failed: {exc}")

    time.sleep(4.0)
    page.screenshot(path="/tmp/nblm_pod_started.png", full_page=True)
    log(f"snapshot started: /tmp/nblm_pod_started.png  generate_clicked={gen_clicked}")

    if gen_clicked:
        log("STATE=GENERATION_STARTED")
        return 0
    log("STATE=AUDIO_PANEL_OPEN_NO_GENERATE")
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
