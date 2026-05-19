"""Create NotebookLM notebook + Deep Research on Autonomous Agents.

Mirrors notebooklm_launch_ai_news_may2026.py — cloned 2026-05-18.
"""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
NOTEBOOK_HOME = "https://notebooklm.google.com/"

QUERY = (
    "Deep research sobre agentes autonomos LLM en 2025-2026. Idioma del informe: espanol. "
    "Detallado, con citas inline y enlaces a fuentes oficiales (papers ArXiv, blog posts de "
    "laboratorios, repos GitHub, sitios universitarios). Cubrir con profundidad: "
    "(1) Universidades de referencia — Stanford HAI (Human-Centered AI Institute), MIT CSAIL, "
    "Berkeley BAIR (Berkeley AI Research) y la SkyLab, Princeton (Karthik Narasimhan, Center "
    "for Information Technology Policy), Carnegie Mellon (LTI, Graham Neubig), University of "
    "Washington H2lab, Tsinghua KEG. Citar autores, programas y papers especificos de cada "
    "grupo sobre agentes autonomos. "
    "(2) Papers seminales — ReAct (Yao et al, Princeton/Google), Voyager (Wang et al, NVIDIA/"
    "Caltech), Toolformer (Schick et al, Meta), AutoGPT post-mortem, GAIA benchmark (Mialon et "
    "al, Meta/HuggingFace), AgentBench (Liu et al, Tsinghua), SWE-Bench y SWE-Bench Live "
    "(Jimenez et al, Princeton), METR time-horizon reports, Skill0 (RL para internalizacion de "
    "skills), Constitutional AI (Anthropic), Reflexion, Tree-of-Thoughts. Resumen tecnico + "
    "implicaciones. "
    "(3) Labs frontier y sus sistemas de agentes — Anthropic (Petri, Project Glasswing, Claude "
    "Mythos Preview, Claude computer use), OpenAI (o-series agents, ChatGPT Atlas, Codex), "
    "Google DeepMind (AlphaEvolve, SIMA, Project Mariner), Meta (CICERO, Llama agents), xAI "
    "(Grok agent loops). Que arquitectura interna usan, evaluaciones publicas, casos productivos. "
    "(4) Benchmarks y evaluaciones — ARC-AGI-3, OSWorld-V, SWE-Bench Live, Cyber Range AISI, "
    "WebArena, MLE-Bench, GAIA, AgentBench. Cifras actuales de Claude Sonnet 4.6, GPT-5.5, "
    "Gemini 3.x, modelos open-source. Diferencias entre evals estaticas y agenticas. "
    "(5) Expert voices — Andrej Karpathy (vision software 3.0), Yao Shunyu (autor ReAct), Noam "
    "Brown (OpenAI, reasoning), Yann LeCun (postura anti-LLM-agent), Shane Legg (DeepMind), "
    "Dario Amodei (Anthropic ASL framework), Demis Hassabis, Ilya Sutskever (SSI). Citas, "
    "papers o entrevistas recientes (ultimos 12 meses) que articulen su posicion. "
    "(6) Patrones arquitectonicos — ReAct loops, tool-use, planning + execution split, multi-"
    "agent orchestration, file-as-bus communication, reflexion/self-critique, RAG-augmented "
    "agents, code-execution sandboxes. Trade-offs y cuando aplica cada uno. "
    "(7) Safety y alignment — sycophancy, reward hacking, eval-awareness, deceptive alignment, "
    "Constitutional AI, RLHF/DPO, model behavior reports (Anthropic, OpenAI), AISI evals, "
    "EU AI Act enforcement para agentic systems, US executive orders relevantes. "
    "(8) Aplicaciones reales — agentic coding (Cursor, Cognition Devin, Claw, Cline, Aider), "
    "research agents (Elicit, Consensus, OpenAI Deep Research, Perplexity), customer support, "
    "RPA evolved (UiPath, Sema4), enterprise deployments (Novo Nordisk, JPMorgan, Walmart). "
    "(9) Outlook proximos 12 meses — METR time-horizon proyecciones, scaling laws para agentes, "
    "memoria persistente, multimodal embodied agents, robotic foundation models (RT-3, Pi, "
    "Figure), economic impact estimates de Goldman/McKinsey/Anthropic econ team. "
    "Tono: tecnico-ejecutivo, sin marketing fluff. Para cada hallazgo dar fuente concreta."
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


def click_first_visible(page, selectors, label, timeout: float = 8.0) -> bool:
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

    if "/notebook/" in page.url or page.url.rstrip("/").endswith("notebooklm.google.com"):
        try:
            page.goto(NOTEBOOK_HOME, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
    time.sleep(2.0)

    create_selectors = [
        'button:has-text("Crear cuaderno nuevo")',
        'button:has-text("Crear nuevo")',
        'button:has-text("Create new")',
        'button:has-text("Nuevo cuaderno")',
        'button:has-text("+ Crear")',
        'button[aria-label*="Crear"]',
        'button[aria-label*="Create"]',
    ]
    click_first_visible(page, create_selectors, "Create notebook", timeout=10.0)

    # New UI (2026-05-18): clicking "Crear nuevo" opens an add-sources modal
    # directly on the home page BEFORE a notebook id exists. Skip the URL wait
    # if the dialog is already visible. Notebook id materializes after submit.
    dialog_visible_quickly = False
    quick_deadline = time.time() + 8.0
    while time.time() < quick_deadline:
        try:
            if page.locator('mat-dialog-container').first.is_visible():
                dialog_visible_quickly = True
                log(f"dialog appeared on home page (no notebook id yet) — url={page.url}")
                break
        except Exception:
            pass
        time.sleep(0.5)

    if not dialog_visible_quickly:
        deadline = time.time() + 60.0
        while time.time() < deadline:
            if "/notebook/" in page.url and "/notebook/creating" not in page.url:
                log(f"notebook ready: {page.url}")
                break
            time.sleep(1.0)
        else:
            page.screenshot(path="/tmp/nblm_agents_create_timeout.png", full_page=True)
            log("TIMEOUT waiting for notebook id")
            return 8

        target = page.url.split("?")[0] + "?addSource=true"
        log(f"reloading with addSource=true → {target}")
        page.goto(target, wait_until="domcontentloaded", timeout=20000)
        time.sleep(4.0)

    dialog_candidates = [
        'mat-dialog-container',
        'div.mdc-dialog__surface',
        'div[role="dialog"]:has-text("Buscar fuentes")',
        'div[role="dialog"]:has-text("Fast Research")',
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
        log("dialog not visible — falling back to page scope")
        page.screenshot(path="/tmp/nblm_agents_no_dialog.png", full_page=True)
        dialog = page

    fr_chip = dialog.locator('button:has-text("Fast Research")').first
    try:
        fr_chip.click(timeout=5000)
        log("clicked Fast Research chip")
    except Exception as exc:
        log(f"FR chip click failed: {exc}")
        page.screenshot(path="/tmp/nblm_agents_fr_fail.png", full_page=True)
        return 5
    time.sleep(0.8)

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
        page.screenshot(path="/tmp/nblm_agents_deep_fail.png", full_page=True)
        return 6
    time.sleep(1.5)

    typed = False
    for sel in [
        'textarea[placeholder*="investigar"]',
        'input[placeholder*="Buscar fuentes"]',
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
            log(f"typed query into {sel!r} ({len(QUERY)} chars)")
            typed = True
            break
        except Exception as exc:
            log(f"input {sel!r} failed: {exc}")
            continue
    if not typed:
        page.screenshot(path="/tmp/nblm_agents_no_textarea.png", full_page=True)
        return 6
    time.sleep(0.6)

    sent = False
    for sel in [
        'button[aria-label="Enviar"]',
        'button[aria-label="Send"]',
        'button:has(mat-icon)',
    ]:
        try:
            for b in dialog.locator(sel).all():
                try:
                    if b.is_visible() and b.is_enabled():
                        b.click()
                        log(f"clicked Send via {sel!r}")
                        sent = True
                        break
                except Exception:
                    continue
            if sent:
                break
        except Exception as exc:
            log(f"send {sel!r} failed: {exc}")
    if not sent:
        try:
            page.keyboard.press("Enter")
            log("submitted via Enter key fallback")
            sent = True
        except Exception:
            pass
    if not sent:
        page.screenshot(path="/tmp/nblm_agents_no_send.png", full_page=True)
        return 7

    time.sleep(3.0)
    final_url = page.url
    page.screenshot(path="/tmp/nblm_agents_launched.png", full_page=True)
    log(f"DEEP_RESEARCH_LAUNCHED url={final_url}")
    log("snapshot: /tmp/nblm_agents_launched.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
