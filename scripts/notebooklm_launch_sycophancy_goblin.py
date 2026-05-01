"""Create a NotebookLM notebook and launch Deep Research on sycophancy + goblin.

Connects to the existing Chrome CDP session, opens NotebookLM, creates a new
notebook, switches to Deep Research mode, types the query, and submits. The
research itself takes 3-8 minutes — this script returns once the query is
submitted; monitoring is a separate step.
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
NOTEBOOK_HOME = "https://notebooklm.google.com/"

QUERY = (
    "Reward hacking en LLMs frontier 2024-2026: sycophancy y goblin como "
    "artefactos hermanos del mismo problema. "
    "Cubrir: "
    "(1) Estudio Anthropic 2026 sobre 1M conversaciones de personal guidance, "
    "Claude Opus 4.7 y Mythos Preview cortando la tasa de sycophancy a la mitad, "
    "concentracion en spirituality y relationship guidance. "
    "(2) Persona vectors de Anthropic (agosto 2025) como teoria unificada de "
    "rasgos neurales que controlan evil, sycophancy, hallucination. "
    "(3) Tool open-source de Anthropic para auditar sycophancy y deception "
    "(octubre 2025, lanzada con Sonnet 4.5). "
    "(4) Reward model sycophancy paper de Anthropic (marzo 2025) y la "
    "generalizacion de modelos desde sycophancy hacia premeditated lying y "
    "reward function tampering (junio 2024). "
    "(5) Caso OpenAI GPT-4o abril-mayo 2025: rollback por overly flattering, "
    "postmortem 'Expanding on what we missed with sycophancy', y el "
    "artefacto goblin causado por sobre-recompensa de la 'nerdy personality' "
    "que metia menciones magicas en contextos irrelevantes. "
    "(6) Mitigaciones tecnicas: contrastive training, filtering de training data, "
    "remocion de reward signals especificos, evals automatizadas. "
    "(7) Implicaciones para agentes autonomos que ejecutan tareas de larga "
    "duracion sin supervision humana directa. "
    "Idioma del informe: espanol. "
    "Citar papers, blog posts oficiales y hilos de @AnthropicAI y @OpenAI."
)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def find_notebooklm_page(ctx, create_if_missing: bool = True):
    for p in ctx.pages:
        try:
            if "notebooklm.google.com" in p.url:
                return p
        except Exception:
            continue
    if not create_if_missing:
        return None
    p = ctx.new_page()
    p.set_viewport_size({"width": 1280, "height": 900})
    p.goto(NOTEBOOK_HOME, wait_until="domcontentloaded", timeout=30000)
    return p


def click_first_visible(page, selectors: list[str], label: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.click()
                    log(f"clicked '{label}' via {sel!r}")
                    return True
            except Exception:
                continue
        time.sleep(0.4)
    log(f"FAILED to click '{label}' — tried {selectors}")
    return False


def main() -> int:
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

    page = find_notebooklm_page(ctx, create_if_missing=True)
    if page is None:
        log("NO_PAGE")
        return 4
    log(f"using page: {page.url}")
    page.bring_to_front()

    # Force-reopen the addSource modal so state is deterministic.
    if "/notebook/" in page.url:
        target = page.url.split("?")[0] + "?addSource=true"
        log(f"reloading with addSource=true → {target}")
        page.goto(target, wait_until="domcontentloaded", timeout=20000)
    time.sleep(3.5)

    # Dialog selector — must skip the hidden emoji-keyboard role=dialog.
    dialog_candidates = [
        'mat-dialog-container',
        'div.mdc-dialog__surface',
        'div[role="dialog"]:has-text("Buscar fuentes")',
        'div[role="dialog"]:has-text("Fast Research")',
        'div:has(> input[placeholder*="Buscar fuentes"])',
    ]
    dialog = None
    for ds in dialog_candidates:
        try:
            d = page.locator(ds).first
            d.wait_for(state="visible", timeout=4000)
            dialog = d
            log(f"dialog via {ds!r}")
            break
        except Exception:
            continue
    if dialog is None:
        log("dialog not visible — trying page-scoped fallback")
        page.screenshot(path="/tmp/nblm_no_dialog.png", full_page=True)
        dialog = page  # fall back to whole page; selectors will still work

    # Step 2a: Click the "Fast Research" chip INSIDE the dialog.
    fr_chip = dialog.locator('button:has-text("Fast Research")').first
    try:
        fr_chip.click(timeout=5000)
        log("clicked Fast Research chip in dialog")
    except Exception as exc:
        log(f"FR chip click failed: {exc}")
        page.screenshot(path="/tmp/nblm_fr_fail.png", full_page=True)
        return 5
    time.sleep(0.8)

    # Step 2b: Click "Deep Research" in the popover (it's outside the dialog).
    deep_clicked = False
    for sel in [
        'div[role="menu"] >> text="Deep Research"',
        'mat-option:has-text("Deep Research")',
        '[role="option"]:has-text("Deep Research")',
        'button:has-text("Deep Research")',
    ]:
        try:
            opt = page.locator(sel).first
            opt.wait_for(state="visible", timeout=2500)
            opt.click()
            log(f"clicked Deep Research via {sel!r}")
            deep_clicked = True
            break
        except Exception:
            continue
    if not deep_clicked:
        page.screenshot(path="/tmp/nblm_deep_fail.png", full_page=True)
        log("snapshot: /tmp/nblm_deep_fail.png")
        return 6
    time.sleep(0.8)

    deep_selectors = [
        'div:has-text("Deep Research"):visible',
        'mat-option:has-text("Deep Research")',
        'button:has-text("Deep Research")',
        'li:has-text("Deep Research")',
        'span:has-text("Deep Research")',
    ]
    click_first_visible(page, deep_selectors, "Deep Research option")
    time.sleep(1.5)

    # Step 3: Type the query into the input INSIDE the dialog.
    typed = False
    for sel in [
        'input[placeholder*="Buscar fuentes"]',
        'textarea[placeholder*="investigar"]',
        'input[placeholder*="investigar"]',
        'textarea',
        'input[type="text"]',
    ]:
        try:
            ta = dialog.locator(sel).first
            ta.wait_for(state="visible", timeout=3000)
            ta.click()
            time.sleep(0.2)
            page.keyboard.type(QUERY, delay=5)
            log(f"typed query into dialog {sel!r} ({len(QUERY)} chars)")
            typed = True
            break
        except Exception as exc:
            log(f"input {sel!r} failed: {exc}")
            continue
    if not typed:
        page.screenshot(path="/tmp/nblm_no_textarea.png", full_page=True)
        log("snapshot: /tmp/nblm_no_textarea.png")
        return 6
    time.sleep(0.6)

    # Step 4: Click Send INSIDE the dialog.
    sent = False
    for sel in [
        'button[aria-label="Enviar"]',
        'button[aria-label="Send"]',
        'button:has(mat-icon)',
    ]:
        try:
            btns = dialog.locator(sel).all()
            for b in btns:
                try:
                    if b.is_visible() and b.is_enabled():
                        b.click()
                        log(f"clicked Send via dialog {sel!r}")
                        sent = True
                        break
                except Exception:
                    continue
            if sent:
                break
        except Exception as exc:
            log(f"send {sel!r} failed: {exc}")
    if not sent:
        # Fallback: press Enter
        try:
            page.keyboard.press("Enter")
            log("submitted via Enter key")
            sent = True
        except Exception as exc:
            log(f"Enter fallback failed: {exc}")
    if not sent:
        page.screenshot(path="/tmp/nblm_no_send.png", full_page=True)
        log("snapshot: /tmp/nblm_no_send.png")
        return 7

    time.sleep(3.0)
    final_url = page.url
    page.screenshot(path="/tmp/nblm_research_launched.png", full_page=True)
    log(f"DEEP_RESEARCH_LAUNCHED url={final_url}")
    log("snapshot: /tmp/nblm_research_launched.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
