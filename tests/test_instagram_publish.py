"""Offline tests for InstagramPublishService.

The real DOM flow runs against a logged-in Chrome over CDP and cannot be
exercised in CI, so these tests:
  - cover the pure share-success detector (the case-sensitivity bug fix),
  - drive publish_reel against a fully faked Playwright page to assert the
    create→upload→caption→share→verify sequence and its guard branches,
  - assert the brain tool is registered as Tier 3 with the right schema.

No real network, browser, or Instagram calls happen here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claw_v2.instagram_publish import (
    InstagramPublishService,
    PublishResult,
    is_share_success,
)


# --- pure detector ---------------------------------------------------------

def test_is_share_success_matches_capitalized_modal():
    # The live modal capitalizes "Se compartió tu reel" — the original bug
    # matched lowercase only and returned False on a real success.
    assert is_share_success("Reel compartido\nSe compartió tu reel.") is True


def test_is_share_success_english_and_negative():
    assert is_share_success("Your reel was shared") is True
    assert is_share_success("Compartir") is False
    assert is_share_success("") is False


def test_publish_result_to_dict_keys():
    d = PublishResult(ok=True, account="pachanodesign", shared=True, verified=True).to_dict()
    assert d["account"] == "pachanodesign"
    assert d["shared"] is True and d["verified"] is True
    assert set(("ok", "account", "shared", "verified", "reason")).issubset(d)


# --- faked Playwright page -------------------------------------------------

class _FakeKeyboard:
    def __init__(self):
        self.inserted = []
    def insert_text(self, text):
        self.inserted.append(text)


class _FakeLoc:
    def __init__(self, count=1, fail=False):
        self._count = count
        self._fail = fail
    @property
    def first(self):
        return self
    def click(self, timeout=None):
        if self._fail:
            raise RuntimeError("not clickable")
    def count(self):
        return self._count


class _FakeElement:
    def click(self, timeout=None):
        pass


class _FakeFC:
    def __init__(self, sink):
        self._sink = sink
    def set_files(self, path):
        self._sink.append(path)


class _FakePage:
    def __init__(self, *, logged_out=False, hrefs=None, body_text="Se compartió tu reel"):
        self.keyboard = _FakeKeyboard()
        self._logged_out = logged_out
        self._hrefs = hrefs if hrefs is not None else ["/pachanodesign/"]
        self._body_text = body_text
        self.files_set = []
        self.shots = []
    def goto(self, *a, **k):
        pass
    def wait_for_timeout(self, *a, **k):
        pass
    def evaluate(self, js, *a):
        if "name=username" in js or "name=password" in js:
            return self._logged_out
        if "a[href]" in js:
            return self._hrefs
        if "document.body.innerText" in js:
            return self._body_text
        return None
    def get_by_role(self, role, name=None):
        return _FakeLoc()
    def get_by_text(self, text, exact=False):
        return _FakeLoc()
    def locator(self, sel):
        return _FakeLoc(count=1)
    def wait_for_selector(self, sel, timeout=None, state=None):
        return _FakeElement()
    def expect_file_chooser(self, timeout=None):
        page = self
        class _Ctx:
            def __enter__(self_inner):
                class _V:
                    value = _FakeFC(page.files_set)
                return _V
            def __exit__(self_inner, *a):
                return False
        return _Ctx()
    def screenshot(self, path=None):
        self.shots.append(path)


class _FakeBrowser:
    def __init__(self, page):
        self.contexts = [self._Ctx(page)]
    class _Ctx:
        def __init__(self, page):
            self._page = page
        def new_page(self):
            return self._page


def _install_fake_playwright(monkeypatch, page):
    class _PW:
        def __enter__(self_inner):
            class _Inst:
                class chromium:
                    @staticmethod
                    def connect_over_cdp(url):
                        return _FakeBrowser(page)
            return _Inst()
        def __exit__(self_inner, *a):
            return False
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _PW())


@pytest.fixture
def svc(tmp_path):
    return InstagramPublishService(artifacts_dir=tmp_path / "ig", share_confirm_timeout_seconds=1)


def test_publish_happy_path(svc, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1024)
    page = _FakePage(hrefs=["/pachanodesign/"], body_text="Reel compartido\nSe compartió tu reel.")
    _install_fake_playwright(monkeypatch, page)

    res = svc.publish_reel(str(video), caption="hola mundo", expected_account="pachanodesign")

    assert res.ok is True
    assert res.shared is True and res.verified is True
    assert res.account == "pachanodesign"
    assert res.caption_chars == len("hola mundo")
    assert page.keyboard.inserted == ["hola mundo"]
    assert page.files_set == [str(video)]


def test_publish_missing_video(svc):
    res = svc.publish_reel("/no/such/file.mp4", caption="x")
    assert res.ok is False
    assert res.reason == "video_not_found"


def test_publish_wrong_account_guard(svc, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1024)
    page = _FakePage(hrefs=["/someoneelse/"])
    _install_fake_playwright(monkeypatch, page)

    res = svc.publish_reel(str(video), caption="x", expected_account="pachanodesign")
    assert res.ok is False
    assert res.reason == "wrong_account"
    assert page.files_set == []  # never uploaded to the wrong account


def test_publish_account_unverified_fails_closed(svc, tmp_path, monkeypatch):
    # 2026-05-31 audit (H4): _detect_account returns None (DOM change / JS error
    # / no profile href). The guard must fail CLOSED — never publish to whatever
    # IG account the shared CDP session happens to be logged into.
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1024)
    page = _FakePage(hrefs=[])  # no profile href -> _detect_account -> None
    _install_fake_playwright(monkeypatch, page)

    res = svc.publish_reel(str(video), caption="x", expected_account="pachanodesign")
    assert res.ok is False
    assert res.reason == "account_unverified"
    assert page.files_set == []  # never uploaded when the account is unconfirmed


def test_publish_not_logged_in(svc, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1024)
    page = _FakePage(logged_out=True)
    _install_fake_playwright(monkeypatch, page)

    res = svc.publish_reel(str(video), caption="x")
    assert res.ok is False
    assert res.reason == "not_logged_in"


def test_publish_share_not_confirmed(svc, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * 1024)
    page = _FakePage(hrefs=["/pachanodesign/"], body_text="(nothing happened)")
    _install_fake_playwright(monkeypatch, page)

    res = svc.publish_reel(str(video), caption="x", expected_account="pachanodesign")
    assert res.ok is False
    assert res.verified is False
    assert res.reason == "share_not_confirmed"


def test_brain_tool_registered_with_schema():
    from claw_v2.tools import (
        DEFAULT_TOOL_AGENT_CLASSES,
        ToolRegistry,
        TIER_REQUIRES_APPROVAL,
    )

    assert "InstagramPublish" in DEFAULT_TOOL_AGENT_CLASSES
    registry = ToolRegistry.default(workspace_root=Path("/tmp"))
    tool = registry.get("InstagramPublish")
    assert tool.tier == TIER_REQUIRES_APPROVAL
    assert callable(tool.handler)
    assert tool.mutates_state is True
    assert tool.requires_network is True
    props = tool.parameter_schema.get("properties", {})
    assert {"video_path", "caption", "account"}.issubset(props)
    assert "video_path" in tool.parameter_schema.get("required", [])
    assert tool.success_condition is not None
    assert tool.preflight is not None
