"""Open LinkedIn, navigate to Guillermo Rauch's profile, click Message, paste
the draft. Does NOT send — Hector reviews and clicks Send manually.

Reads Version A of the message (peer-level, no-ask).
"""
from __future__ import annotations

import sys
import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
RAUCH_URL = "https://www.linkedin.com/in/rauchg/"

MESSAGE = (
    "Guillermo, hola — soy Hector Pachano, founder de Pachano Design.\n\n"
    "Vi tu anuncio sobre Vercel Labs (\"we used to build tools for humans, now "
    "we're building them for agents\") y resonó fuerte porque llevo meses "
    "construyendo exactamente eso desde el otro lado: un agente autónomo (Claw) "
    "que corre 24/7 en mi Mac y lo opero desde Telegram mientras estoy en mi "
    "trabajo de mantenimiento.\n\n"
    "Algunas piezas que me tocó resolver y donde Vercel Labs pisa fuerte:\n\n"
    "- Brain-bypass: agente generalista versus dispatcher determinista, con env "
    "flags para ramp gradual.\n"
    "- Evidence-based verifier: estoy integrando Petri (Anthropic alignment) "
    "para que ninguna task cierre como \"succeeded\" sin judge agent "
    "independiente que verifique evidencia persistida.\n"
    "- Multi-tier autonomy: Tier 1-2 ejecuta sin confirmación; Tier 3 (deploy, "
    "push a main, send_message externo) requiere aprobación explícita del "
    "owner.\n"
    "- Telemetría dual-stream (target / harness) para auditoría post-hoc.\n\n"
    "No estoy aplicando a Vercel — no es eso. Lo que quería es decirte que tu "
    "thesis sobre \"devtools for agents\" está siendo construida también por "
    "founders solitarios desde afuera. Si en algún momento te interesa "
    "intercambiar notas sobre lo que estoy resolviendo, encantado.\n\n"
    "Saludos desde Texas.\n"
    "— Hector"
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


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

    page = None
    for p in ctx.pages:
        try:
            if "linkedin.com" in p.url:
                page = p
                break
        except Exception:
            continue
    if page is None:
        page = ctx.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})

    page.goto(RAUCH_URL, wait_until="domcontentloaded", timeout=30000)
    page.bring_to_front()
    log(f"navigated to {page.url}")
    time.sleep(4.5)

    # Verify we're not at a login wall.
    if "/login" in page.url or "authwall" in page.url:
        page.screenshot(path="/tmp/li_login_wall.png", full_page=True)
        log("LOGIN_WALL — Hector needs to log in to LinkedIn first")
        return 5

    page.screenshot(path="/tmp/li_profile.png", full_page=True)
    log("snapshot profile: /tmp/li_profile.png")

    # Try clicking the Message button on the profile.
    msg_clicked = False
    for sel in [
        'button:has-text("Mensaje")',
        'button:has-text("Message")',
        'a:has-text("Mensaje")',
        'a:has-text("Message")',
        '[aria-label*="Mensaje"]',
        '[aria-label*="Message"]',
    ]:
        try:
            btns = page.locator(sel).all()
            for b in btns:
                try:
                    if b.is_visible():
                        b.click()
                        log(f"clicked Message via {sel!r}")
                        msg_clicked = True
                        break
                except Exception:
                    continue
            if msg_clicked:
                break
        except Exception as exc:
            log(f"selector {sel!r} failed: {exc}")

    if not msg_clicked:
        page.screenshot(path="/tmp/li_no_message_btn.png", full_page=True)
        log("snapshot: /tmp/li_no_message_btn.png")
        log("MESSAGE_BUTTON_NOT_FOUND — could be 1st-degree-only or rate limited")
        return 6

    time.sleep(3.5)
    page.screenshot(path="/tmp/li_compose_open.png", full_page=True)
    log("snapshot compose: /tmp/li_compose_open.png")

    # Locate composer (LinkedIn uses a contenteditable div).
    composer_selectors = [
        'div.msg-form__contenteditable[contenteditable="true"]',
        'div[role="textbox"][contenteditable="true"]',
        'div[contenteditable="true"][aria-label*="Mensaje"]',
        'div[contenteditable="true"][aria-label*="Message"]',
        'div[contenteditable="true"]',
    ]
    typed = False
    for sel in composer_selectors:
        try:
            box = page.locator(sel).first
            box.wait_for(state="visible", timeout=4000)
            box.click()
            time.sleep(0.3)
            page.keyboard.type(MESSAGE, delay=4)
            log(f"typed {len(MESSAGE)} chars into {sel!r}")
            typed = True
            break
        except Exception as exc:
            log(f"composer {sel!r} failed: {exc}")

    page.screenshot(path="/tmp/li_message_pasted.png", full_page=True)
    log("snapshot pasted: /tmp/li_message_pasted.png")
    if typed:
        log("STATE=MESSAGE_TYPED_NOT_SENT — Hector reviews and clicks Send manually")
        return 0
    log("STATE=COMPOSER_NOT_FOUND")
    return 7


if __name__ == "__main__":
    raise SystemExit(main())
