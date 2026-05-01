"""Open ChatGPT in the existing Chrome CDP session, paste the image prompt.

Hector will drag-drop his selfie into the chat as a face reference. This
script's only job is to land in a fresh ChatGPT chat with the refined prompt
already typed in the composer, ready to send once the photo is attached.
"""
from __future__ import annotations

import time

from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9250"
CHATGPT_URL = "https://chatgpt.com/?model=gpt-image-1"

PROMPT = (
    "Generate a cinematic editorial portrait using the attached selfie as the "
    "EXACT face reference. Match face, bone structure, eye shape and color, "
    "skin tone, jawline, hair fade pattern (tight military taper, dark brown), "
    "and the salt-and-pepper short beard (subtle gray accents) precisely from "
    "the photo. Do not stylize the face — keep it photoreal and identical.\n\n"
    "Subject: Hector Pachano, 40, Latin American, founder of Pachano Design. "
    "5'5\" (165 cm), 183 lbs of athletic muscle from 3 years of strength "
    "training 4-5 days/week. Confident builder energy, direct intelligent gaze, "
    "subtle half-smile of someone who ships and verifies.\n\n"
    "Setting: late-evening modern home office / apartment. Warm 3200K desk "
    "lamp + cool 5600K rim from three monitors. On the desk: a MacBook Pro "
    "open to a terminal with green Claude Code text scrolling, an iPhone on a "
    "stand showing a Telegram chat with a bot called 'Claw', a second monitor "
    "with a 3D logo render rotating, a third with a Vercel deploy dashboard. "
    "AirPods Max around his neck (NotebookLM podcast playing). Behind him a "
    "small whiteboard with sketches: 'TIC Insurance', 'SGC SaaS', "
    "'QTS BTC/Gold/DOGE', 'Dr.-strange / Claw'. A folded paper postcard "
    "mockup on the desk (AI lead-gen for home services).\n\n"
    "Wardrobe: technical black quarter-zip pullover (Lululemon Metal Vent "
    "Tech style) over a charcoal tee, dark indigo selvedge denim, black "
    "smartwatch on left wrist (matching the one in the selfie), no other "
    "jewelry. Operator-builder aesthetic, not startup-hoodie cliché.\n\n"
    "Composition: 3/4 angle, eyes slightly off-camera as if reading a "
    "Telegram notification mid-thought. Sharp on the face, soft bokeh on "
    "the desk surface.\n\n"
    "Style: shot on Sony A7R V, 50mm f/1.4 prime, ISO 800, shallow DOF, "
    "color graded teal-and-orange editorial — dark teal shadows, warm amber "
    "highlights. Photoreal skin texture, no plastic AI smoothness. "
    "Aspect ratio 4:5 portrait. No text overlays. No watermarks."
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_or_open_chatgpt(ctx):
    for p in ctx.pages:
        try:
            if "chatgpt.com" in p.url or "chat.openai.com" in p.url:
                return p
        except Exception:
            continue
    p = ctx.new_page()
    p.set_viewport_size({"width": 1280, "height": 900})
    p.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30000)
    return p


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

    page = find_or_open_chatgpt(ctx)
    log(f"using page: {page.url}")
    page.bring_to_front()
    time.sleep(2.5)

    # Try to start a new chat to avoid threadlock with prior conversation.
    try:
        new_chat = page.locator('a[href="/"]:has-text("New chat")').first
        if new_chat.is_visible():
            new_chat.click()
            time.sleep(1.5)
            log("clicked New chat")
    except Exception:
        pass

    # Locate the composer.
    typed = False
    for sel in [
        'textarea[data-testid="prompt-textarea"]',
        'textarea[placeholder*="Message"]',
        'textarea[placeholder*="Mensaje"]',
        'div[contenteditable="true"][data-testid="prompt-textarea"]',
        'div[contenteditable="true"]',
        'textarea',
    ]:
        try:
            box = page.locator(sel).first
            box.wait_for(state="visible", timeout=4000)
            box.click()
            time.sleep(0.3)
            page.keyboard.type(PROMPT, delay=4)
            log(f"typed prompt into {sel!r} ({len(PROMPT)} chars)")
            typed = True
            break
        except Exception as exc:
            log(f"composer {sel!r} failed: {exc}")
            continue

    page.screenshot(path="/tmp/chatgpt_prompt_ready.png", full_page=True)
    log("snapshot: /tmp/chatgpt_prompt_ready.png")
    if typed:
        log("STATE=PROMPT_TYPED — Hector now drag-drops the selfie and presses Send")
        return 0
    log("STATE=COMPOSER_NOT_FOUND")
    return 5


if __name__ == "__main__":
    raise SystemExit(main())
