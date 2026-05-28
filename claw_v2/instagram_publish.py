"""Instagram Reel publishing service (Chrome CDP).

Permanent runtime capability for: drive the logged-in Instagram web UI in the
CDP Chrome session to publish a local video as a Reel — create flow, file
upload, caption, share — and verify the post actually shipped via Instagram's
own "Se compartió tu reel" / "Your reel was shared" confirmation modal.

Usage from Python:
    from claw_v2.instagram_publish import InstagramPublishService
    svc = InstagramPublishService()
    result = svc.publish_reel(
        "/path/to/video.mp4", caption="...", expected_account="pachanodesign")

Usage from CLI:
    python -m claw_v2.cli.instagram_publish /path/to/video.mp4 \\
        --caption "..." --account pachanodesign

Why this exists: Instagram has no public posting API for personal/creator
reels from a desktop flow, so the deterministic path is CDP browser drive.
Verified live 2026-05-28 on @pachanodesign. The success signal is the IG
confirmation modal text (matched case-insensitively — the modal capitalizes
"Se compartió tu reel", which a naive lowercase check misses).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CDP_URL = "http://localhost:9250"
ARTIFACTS_DIR = Path("/Users/hector/Projects/Dr.-strange/artifacts/instagram")

# IG confirmation phrases that prove the reel was actually shared (any locale).
SHARE_SUCCESS_PHRASES = (
    "se compartió tu reel",
    "se ha compartido",
    "se compartió",
    "tu publicación se ha compartido",
    "reel compartido",
    "your reel was shared",
    "your reel has been shared",
    "your post has been shared",
    "has been shared",
)

# Locale-tolerant button/label candidates.
CREATE_LABELS = ("Crear", "Create")
POST_SUBMENU_LABELS = ("Publicación", "Post")
SELECT_FILE_LABELS = (
    "Seleccionar desde la computadora", "Seleccionar desde el dispositivo",
    "Select from computer", "Select From Computer",
)
OK_LABELS = ("OK", "Aceptar")
NEXT_LABELS = ("Siguiente", "Next")
SHARE_LABELS = ("Compartir", "Share")
CAPTION_SELECTORS = (
    'div[aria-label*="leyenda"][contenteditable="true"]',
    'div[contenteditable="true"][aria-label*="caption"]',
    'div[contenteditable="true"][role="textbox"]',
)


def is_share_success(page_text: str) -> bool:
    """True when Instagram has shown a reel/post share-confirmation message.

    Case-insensitive on purpose: the live modal renders "Se compartió tu reel"
    with a capital S; matching lowercase only (the original bug) returns False
    even though the post shipped.
    """
    low = (page_text or "").lower()
    return any(phrase in low for phrase in SHARE_SUCCESS_PHRASES)


@dataclass(slots=True)
class PublishResult:
    ok: bool
    account: str
    shared: bool = False
    verified: bool = False
    caption_chars: int = 0
    video_path: str | None = None
    screenshots: list[str] = field(default_factory=list)
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "account": self.account,
            "shared": self.shared,
            "verified": self.verified,
            "caption_chars": self.caption_chars,
            "video_path": self.video_path,
            "screenshots": list(self.screenshots),
            "reason": self.reason,
        }


class InstagramPublishService:
    """End-to-end Reel publish over the logged-in CDP Chrome session."""

    def __init__(
        self,
        cdp_url: str = CDP_URL,
        artifacts_dir: Path = ARTIFACTS_DIR,
        share_confirm_timeout_seconds: int = 60,
    ) -> None:
        self.cdp_url = cdp_url
        self.artifacts_dir = artifacts_dir
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.share_confirm_timeout = share_confirm_timeout_seconds

    # -- helpers -----------------------------------------------------------
    def _shot(self, page: Any, label: str, sink: list[str]) -> None:
        path = self.artifacts_dir / f"igpub_{label}.png"
        try:
            page.screenshot(path=str(path))
            sink.append(str(path))
        except Exception:
            pass

    @staticmethod
    def _click_any_role(page: Any, names: tuple[str, ...], timeout: int = 4000) -> bool:
        for name in names:
            try:
                page.get_by_role("button", name=name).first.click(timeout=timeout)
                return True
            except Exception:
                continue
        return False

    def _detect_account(self, page: Any) -> str | None:
        try:
            hrefs = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.getAttribute('href'))"
            )
        except Exception:
            return None
        skip = {"/explore/", "/reels/", "/direct/inbox/", "/notifications/", "/popular/"}
        for h in hrefs or []:
            if h and h.startswith("/") and h.endswith("/") and h.count("/") == 2 and h not in skip:
                return h.strip("/")
        return None

    # -- main flow ---------------------------------------------------------
    def publish_reel(
        self,
        video_path: str,
        caption: str,
        expected_account: str | None = None,
    ) -> PublishResult:
        from playwright.sync_api import sync_playwright

        vp = Path(video_path)
        account = expected_account or "unknown"
        shots: list[str] = []
        if not vp.exists():
            return PublishResult(ok=False, account=account, video_path=video_path,
                                 reason="video_not_found")

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.connect_over_cdp(self.cdp_url)
            except Exception as e:
                return PublishResult(ok=False, account=account, video_path=video_path,
                                     reason=f"cdp_unavailable:{type(e).__name__}")
            ctx = browser.contexts[0]
            page = ctx.new_page()
            page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)

            logged_out = page.evaluate(
                "() => !!document.querySelector('input[name=username], input[name=password]')"
            )
            if logged_out:
                return PublishResult(ok=False, account=account, video_path=video_path,
                                     reason="not_logged_in")
            detected = self._detect_account(page)
            if detected:
                account = detected
            if expected_account and detected and expected_account.lower() != detected.lower():
                return PublishResult(ok=False, account=detected, video_path=video_path,
                                     reason="wrong_account")

            if not self._click_any_role(page, CREATE_LABELS, 6000):
                try:
                    page.locator('svg[aria-label="Nueva publicación"], svg[aria-label="New post"]').first.click(timeout=6000)
                except Exception:
                    return PublishResult(ok=False, account=account, video_path=video_path,
                                         reason="create_button_not_found")
            page.wait_for_timeout(2000)
            self._shot(page, "01_menu", shots)

            for loc in (page.get_by_text("Publicación", exact=True),
                        page.get_by_role("link", name="Publicación"),
                        page.get_by_text("Post", exact=True)):
                try:
                    loc.first.click(timeout=3000)
                    break
                except Exception:
                    continue
            page.wait_for_timeout(2500)

            file_set = False
            try:
                with page.expect_file_chooser(timeout=8000) as fc_info:
                    self._click_any_role(page, SELECT_FILE_LABELS, 2500)
                fc_info.value.set_files(str(vp))
                file_set = True
            except Exception:
                try:
                    page.locator('input[type="file"]').first.set_input_files(str(vp), timeout=5000)
                    file_set = True
                except Exception:
                    pass
            if not file_set:
                self._shot(page, "fail_upload", shots)
                return PublishResult(ok=False, account=account, video_path=video_path,
                                     screenshots=shots, reason="file_upload_failed")
            page.wait_for_timeout(9000)
            self._shot(page, "02_uploaded", shots)

            self._click_any_role(page, OK_LABELS, 3000)
            page.wait_for_timeout(2000)

            for _ in range(3):
                try:
                    if page.locator(", ".join(CAPTION_SELECTORS[:2])).count() > 0:
                        break
                except Exception:
                    pass
                if not self._click_any_role(page, NEXT_LABELS, 4000):
                    try:
                        page.locator('div[role="button"]:has-text("Siguiente"), div[role="button"]:has-text("Next")').first.click(timeout=4000)
                    except Exception:
                        pass
                page.wait_for_timeout(3000)

            cap_filled = False
            for sel in CAPTION_SELECTORS:
                try:
                    el = page.wait_for_selector(sel, timeout=4000, state="visible")
                    if el:
                        el.click()
                        page.wait_for_timeout(400)
                        page.keyboard.insert_text(caption)
                        cap_filled = True
                        break
                except Exception:
                    continue
            page.wait_for_timeout(1500)
            self._shot(page, "03_caption", shots)
            if not cap_filled:
                return PublishResult(ok=False, account=account, video_path=video_path,
                                     screenshots=shots, reason="caption_box_not_found")

            shared_click = self._click_any_role(page, SHARE_LABELS, 5000)
            if not shared_click:
                try:
                    page.locator('div[role="button"]:has-text("Compartir"), div[role="button"]:has-text("Share")').first.click(timeout=5000)
                    shared_click = True
                except Exception:
                    pass
            if not shared_click:
                self._shot(page, "fail_share", shots)
                return PublishResult(ok=False, account=account, video_path=video_path,
                                     screenshots=shots, reason="share_button_not_found")

            verified = False
            deadline = time.time() + self.share_confirm_timeout
            while time.time() < deadline:
                page.wait_for_timeout(3000)
                try:
                    body = page.evaluate("() => (document.body.innerText||'')")
                except Exception:
                    body = ""
                if is_share_success(body):
                    verified = True
                    break
            self._shot(page, "04_after_share", shots)

            return PublishResult(
                ok=verified,
                account=account,
                shared=shared_click,
                verified=verified,
                caption_chars=len(caption),
                video_path=video_path,
                screenshots=shots,
                reason=None if verified else "share_not_confirmed",
            )
