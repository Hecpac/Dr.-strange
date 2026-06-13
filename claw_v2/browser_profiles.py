"""Named browser profiles + login/challenge health checks.

A cheap, deterministic pre-flight for delegated browser work: before spending an
LLM agent on a named profile (X, Instagram, NotebookLM, ChatGPT), probe the live
CDP page and classify whether the session is usable. If it is logged out or
sitting behind an anti-bot challenge (Cloudflare), surface a clear human state
and let the caller stop — we never attempt to evade a challenge.

`classify_health` is a pure function (easy to unit-test). `check_profile_health`
wraps it with an injectable CDP prober (the default uses Playwright over CDP).
Integration is X-first: only X objectives are gated today; the other profiles
are registered (data ready) but not yet wired to objective detection.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class BrowserProfileHealth(str, enum.Enum):
    """State of a named browser profile's live session."""

    OK = "ok"
    NEEDS_LOGIN = "needs_login"
    BLOCKED_BY_CHALLENGE = "blocked_by_challenge"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BrowserProfile:
    name: str
    profile_dir: str
    home_url: str
    allowed_domains: tuple[str, ...]
    # Lowercased substrings. A login marker found in the final URL or page text
    # means the session is logged OUT. A challenge marker means an anti-bot wall.
    login_markers: tuple[str, ...]
    challenge_markers: tuple[str, ...]


# Cloudflare / generic anti-bot interstitials, shared across profiles.
_CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "attention required",
    "cf-challenge",
    "cf_chl",
    "/cdn-cgi/challenge-platform",
    "enable javascript and cookies to continue",
)

BROWSER_PROFILES: dict[str, BrowserProfile] = {
    "x": BrowserProfile(
        name="x",
        profile_dir="~/.claw/chrome-profile",
        home_url="https://x.com/home",
        allowed_domains=("x.com", "twitter.com", "pbs.twimg.com", "abs.twimg.com"),
        login_markers=(
            "/i/flow/login",
            "/login",
            "sign in to x",
            "sign in to twitter",
            "create account to",
            "new to x?",
        ),
        challenge_markers=_CHALLENGE_MARKERS,
    ),
    "instagram": BrowserProfile(
        name="instagram",
        profile_dir="~/.claw/chrome-profile",
        home_url="https://www.instagram.com/",
        allowed_domains=("instagram.com", "www.instagram.com", "cdninstagram.com"),
        login_markers=(
            "/accounts/login",
            "log in to instagram",
            "sign up to see photos",
        ),
        challenge_markers=_CHALLENGE_MARKERS,
    ),
    "notebooklm": BrowserProfile(
        name="notebooklm",
        profile_dir="~/.claw/chrome-profile",
        home_url="https://notebooklm.google.com/",
        allowed_domains=("notebooklm.google.com", "accounts.google.com"),
        login_markers=(
            "accounts.google.com/v3/signin",
            "accounts.google.com/signin",
            "sign in to continue",
        ),
        challenge_markers=_CHALLENGE_MARKERS,
    ),
    "chatgpt": BrowserProfile(
        name="chatgpt",
        profile_dir="~/.claw/chrome-profile",
        home_url="https://chatgpt.com/",
        allowed_domains=("chatgpt.com", "chat.openai.com", "auth.openai.com"),
        login_markers=(
            "auth.openai.com",
            "/auth/login",
            "log in to continue",
            "welcome back",
        ),
        challenge_markers=_CHALLENGE_MARKERS,
    ),
}


def get_profile(name: str) -> BrowserProfile | None:
    return BROWSER_PROFILES.get(str(name or "").strip().lower())


def resolve_profile_for_objective(objective: str) -> BrowserProfile | None:
    """Map a delegated objective to a named profile. X-first: only X is wired.

    Returns the X profile for X/Twitter browse requests, else None (generic
    browse keeps using the normal executor with no health gate).
    """
    text = str(objective or "")
    if not text.strip():
        return None
    try:
        from claw_v2.bot_helpers import _looks_like_x_browser_request

        if _looks_like_x_browser_request(text):
            return BROWSER_PROFILES["x"]
    except Exception:
        logger.debug("X objective detection unavailable", exc_info=True)
    return None


def classify_health(
    *, final_url: str, title: str, body_text: str, profile: BrowserProfile
) -> BrowserProfileHealth:
    """Pure classification from a loaded page's URL/title/text. Challenge wins
    over login (a challenge wall can also look logged-out)."""
    url = (final_url or "").lower()
    haystack = f"{url}\n{(title or '').lower()}\n{(body_text or '').lower()}"
    if any(marker in haystack for marker in profile.challenge_markers):
        return BrowserProfileHealth.BLOCKED_BY_CHALLENGE
    if any(marker in haystack for marker in profile.login_markers):
        return BrowserProfileHealth.NEEDS_LOGIN
    return BrowserProfileHealth.OK


def _default_cdp_probe(cdp_url: str, home_url: str, timeout_ms: int) -> tuple[str, str, str]:
    """Open a fresh page in the existing CDP context, load home_url, and return
    (final_url, title, body_text). Runs in a worker thread so it is safe to call
    from inside a running asyncio loop. Closes only the probe page (never Chrome
    or the shared context — login cookies must survive)."""
    import asyncio
    import concurrent.futures

    async def _run() -> tuple[str, str, str]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = await context.new_page()
                try:
                    await page.goto(home_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    final_url = page.url
                    title = await page.title()
                    try:
                        body = await page.inner_text("body")
                    except Exception:
                        body = await page.content()
                    return final_url, title, (body or "")[:8000]
                finally:
                    try:
                        await page.close()
                    except Exception:
                        logger.debug("probe page close failed", exc_info=True)
            finally:
                # Disconnect the CDP client; this does NOT close Chrome.
                try:
                    await browser.close()
                except Exception:
                    logger.debug("probe browser disconnect failed", exc_info=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_run())).result(timeout=timeout_ms / 1000 + 15)


def check_profile_health(
    profile: BrowserProfile,
    cdp_url: str,
    *,
    prober=None,
    timeout_ms: int = 15000,
) -> tuple[BrowserProfileHealth, str]:
    """Probe the live page and classify. Returns (health, detail). Any probe
    error → UNAVAILABLE (with the error text) — the caller decides whether to
    proceed; a flaky probe must not be reported as login/challenge."""
    probe = prober or _default_cdp_probe
    try:
        final_url, title, body = probe(cdp_url, profile.home_url, timeout_ms)
    except Exception as exc:  # noqa: BLE001 - probe failure is a state, not a crash
        logger.debug("profile health probe failed for %s", profile.name, exc_info=True)
        return BrowserProfileHealth.UNAVAILABLE, str(exc)[:200]
    return classify_health(final_url=final_url, title=title, body_text=body, profile=profile), ""


def human_message(profile: BrowserProfile, health: BrowserProfileHealth, detail: str = "") -> str:
    """Clear, non-evasive human-facing status for a non-OK profile health."""
    name = profile.name.upper()
    if health is BrowserProfileHealth.NEEDS_LOGIN:
        return (
            f"El perfil de {name} quedó deslogueado: abrí {profile.home_url} y pide login. "
            f"Entra una vez en el navegador y reintento — no fuerzo el login por ti."
        )
    if health is BrowserProfileHealth.BLOCKED_BY_CHALLENGE:
        return (
            f"{name} está mostrando un muro de verificación (Cloudflare/anti-bot). "
            f"No intento evadirlo. Pasa la verificación una vez en el navegador y reintento."
        )
    if health is BrowserProfileHealth.UNAVAILABLE:
        suffix = f" ({detail})" if detail else ""
        return (
            f"No pude verificar el estado del perfil de {name}: el navegador/CDP no "
            f"respondió a tiempo{suffix}. Reviso CDP y reintento."
        )
    return ""
