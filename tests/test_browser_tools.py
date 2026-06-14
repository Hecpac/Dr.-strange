from __future__ import annotations

import unittest

from claw_v2.browser_tools import (
    BrowserElementRef,
    BrowserToolResult,
    RawElement,
    RawPage,
)


class DataclassTests(unittest.TestCase):
    def test_result_defaults(self) -> None:
        r = BrowserToolResult(success=True, url="https://x.test", title="X")
        self.assertTrue(r.success)
        self.assertEqual(r.element_count, 0)
        self.assertEqual(r.backend, "chrome_cdp")
        self.assertEqual(r.metadata, {})

    def test_raw_page_carries_elements_and_login_flag(self) -> None:
        page = RawPage(
            url="https://x.test",
            title="X",
            text="hello",
            elements=[
                RawElement(
                    selector="#post",
                    role="button",
                    label="Post",
                    text="Post",
                    href=None,
                    input_type=None,
                )
            ],
            login_or_challenge=False,
        )
        self.assertEqual(len(page.elements), 1)
        self.assertEqual(page.elements[0].label, "Post")

    def test_element_ref_shape(self) -> None:
        ref = BrowserElementRef(
            ref="@e1",
            label="Post",
            role="button",
            selector="#post",
            text="Post",
            href=None,
            input_type=None,
        )
        self.assertEqual(ref.ref, "@e1")


from claw_v2.browser_tools import BrowserToolService


class _FakeBackend:
    name = "fake"

    def __init__(self, pages: list[RawPage]) -> None:
        self._pages = pages
        self._i = -1
        self.acted: list[tuple[str, str, str | None]] = []

    def navigate(self, url: str) -> RawPage:
        self._i += 1
        return self._pages[min(self._i, len(self._pages) - 1)]

    def snapshot(self, full: bool = False) -> RawPage:
        return self._pages[min(self._i, len(self._pages) - 1)]

    def act(self, selector: str, action: str, text: str | None = None) -> RawPage:
        self.acted.append((selector, action, text))
        self._i += 1
        return self._pages[min(self._i, len(self._pages) - 1)]

    def screenshot(self, path: str) -> bool:
        return True

    def console(self, clear: bool = False) -> list[str]:
        return ["log: ok"]


def _page(url: str, *elements: RawElement, text: str = "body text", login: bool = False) -> RawPage:
    return RawPage(url=url, title=url, text=text, elements=list(elements), login_or_challenge=login)


class NavigateRefTests(unittest.TestCase):
    def test_navigate_returns_refs_and_snapshot(self) -> None:
        page = _page(
            "https://x.test",
            RawElement("#post", "button", "Post", "Post", None, None),
            RawElement("a.settings", "link", "Settings", "Settings", "/settings", None),
        )
        svc = BrowserToolService(backend=_FakeBackend([page]))
        r = svc.navigate("sess1", "https://x.test")
        self.assertTrue(r.success)
        self.assertEqual(r.url, "https://x.test")
        self.assertEqual(r.element_count, 2)
        self.assertIn('@e1 button "Post"', r.snapshot)
        self.assertIn('@e2 link "Settings" href="/settings"', r.snapshot)

    def test_each_navigate_bumps_ref_version_and_replaces_refs(self) -> None:
        p1 = _page("https://a.test", RawElement("#a", "button", "A", "A", None, None))
        p2 = _page("https://b.test", RawElement("#b", "button", "B", "B", None, None))
        svc = BrowserToolService(backend=_FakeBackend([p1, p2]))
        svc.navigate("sess1", "https://a.test")
        v1 = svc._sessions["sess1"].ref_version
        svc.navigate("sess1", "https://b.test")
        sess = svc._sessions["sess1"]
        self.assertGreater(sess.ref_version, v1)
        self.assertIn("@e1", sess.refs)
        self.assertEqual(sess.refs["@e1"].selector, "#b")


class InteractionTests(unittest.TestCase):
    def test_click_resolves_ref_to_selector(self) -> None:
        p1 = _page("https://x.test", RawElement("#post", "button", "Post", "Post", None, None))
        p2 = _page("https://x.test/done", RawElement("#ok", "button", "OK", "OK", None, None))
        backend = _FakeBackend([p1, p2])
        svc = BrowserToolService(backend=backend)
        svc.navigate("s", "https://x.test")
        r = svc.click("s", "@e1")
        self.assertTrue(r.success)
        self.assertEqual(backend.acted[-1], ("#post", "click", None))

    def test_type_passes_text(self) -> None:
        p1 = _page("https://x.test", RawElement("#q", "textbox", "Search", "", None, "text"))
        backend = _FakeBackend([p1, p1])
        svc = BrowserToolService(backend=backend)
        svc.navigate("s", "https://x.test")
        r = svc.type("s", "@e1", "hello")
        self.assertTrue(r.success)
        self.assertEqual(backend.acted[-1], ("#q", "type", "hello"))

    def test_stale_ref_after_version_change_fails_clearly(self) -> None:
        p1 = _page("https://a.test", RawElement("#a", "button", "A", "A", None, None))
        p2 = _page("https://b.test", RawElement("#b", "button", "B", "B", None, None))
        svc = BrowserToolService(backend=_FakeBackend([p1, p2]))
        svc.navigate("s", "https://a.test")
        svc.navigate("s", "https://b.test")  # ref map replaced
        r = svc.click("s", "@e99")
        self.assertFalse(r.success)
        self.assertEqual(r.error, "stale_ref: @e99 not in current snapshot")


from claw_v2.browser_tools import SNAPSHOT_MAX_ELEMENTS, SNAPSHOT_MAX_TEXT_CHARS


class SafetyCapsTests(unittest.TestCase):
    def test_snapshot_caps_elements_and_marks_truncated(self) -> None:
        many = [
            RawElement(f"#e{i}", "button", f"B{i}", f"B{i}", None, None)
            for i in range(SNAPSHOT_MAX_ELEMENTS + 25)
        ]
        svc = BrowserToolService(backend=_FakeBackend([_page("https://x.test", *many)]))
        r = svc.navigate("s", "https://x.test")
        self.assertEqual(r.element_count, SNAPSHOT_MAX_ELEMENTS)
        self.assertIn("[truncated:", r.snapshot)

    def test_long_body_text_is_capped(self) -> None:
        page = _page("https://x.test", text="x" * (SNAPSHOT_MAX_TEXT_CHARS + 500))
        svc = BrowserToolService(backend=_FakeBackend([page]))
        r = svc.navigate("s", "https://x.test")
        self.assertLessEqual(len(r.snapshot), SNAPSHOT_MAX_TEXT_CHARS + 400)

    def test_login_page_is_not_success(self) -> None:
        page = _page(
            "https://x.test/login",
            RawElement("#u", "textbox", "User", "", None, "text"),
            text="Log in to continue",
            login=True,
        )
        svc = BrowserToolService(backend=_FakeBackend([page]))
        r = svc.navigate("s", "https://x.test/login")
        self.assertFalse(r.success)
        self.assertTrue(r.metadata.get("login_or_challenge"))
        self.assertIn("login_or_challenge", r.error)


class ObserveTests(unittest.TestCase):
    def test_navigate_emits_started_and_completed(self) -> None:
        events: list[tuple[str, dict]] = []

        class _Obs:
            def emit(self, event_type, payload=None):
                events.append((event_type, payload or {}))

        page = _page(
            "https://x.test/secret?token=abcd", RawElement("#a", "button", "A", "A", None, None)
        )
        svc = BrowserToolService(backend=_FakeBackend([page]))
        svc.observe = _Obs()
        svc.navigate("s", "https://x.test/secret?token=abcd")
        kinds = [e[0] for e in events]
        self.assertIn("browser_tool_action_started", kinds)
        self.assertIn("browser_tool_action_completed", kinds)
        for _, payload in events:
            self.assertNotIn("token=abcd", str(payload))

    def test_navigate_backend_error_emits_redacted_failed_event(self) -> None:
        class _BoomBackend:
            name = "fake"

            def navigate(self, url):
                raise RuntimeError("net::ERR loading https://x.test/p?token=secret")

            def snapshot(self, full=False):
                raise AssertionError("unused")

            def act(self, s, a, t=None):
                raise AssertionError("unused")

            def screenshot(self, p):
                return False

            def console(self, clear=False):
                return []

        events = []

        class _Obs:
            def emit(self, et, payload=None):
                events.append((et, payload or {}))

        svc = BrowserToolService(backend=_BoomBackend())
        svc.observe = _Obs()
        r = svc.navigate("s", "https://x.test/p?token=secret")
        self.assertFalse(r.success)
        self.assertIn("browser_tool_action_failed", [e[0] for e in events])
        for _, payload in events:
            self.assertNotIn("token=secret", str(payload))


import os
import urllib.request

from claw_v2.browser_tools import ChromeCdpBrowserBackend


def _cdp_up(endpoint: str = "http://127.0.0.1:9250") -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=2):
            return True
    except Exception:
        return False


class ChromeCdpBackendTests(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("CLAW_BROWSER_CDP_SMOKE") == "1" and _cdp_up(),
        "set CLAW_BROWSER_CDP_SMOKE=1 with Chrome CDP on :9250 to run",
    )
    def test_navigate_example_returns_title_and_refs(self) -> None:
        backend = ChromeCdpBrowserBackend(cdp_endpoint="http://127.0.0.1:9250")
        page = backend.navigate("https://example.com")
        self.assertIn("example", (page.title or "").lower())
        self.assertFalse(page.login_or_challenge)

    def test_backend_name_is_chrome_cdp(self) -> None:
        backend = ChromeCdpBrowserBackend(cdp_endpoint="http://127.0.0.1:9250")
        self.assertEqual(backend.name, "chrome_cdp")


from claw_v2.browser_tools import build_chrome_cdp_service


class FactoryTests(unittest.TestCase):
    def test_factory_builds_service_with_cdp_backend(self) -> None:
        svc = build_chrome_cdp_service(cdp_endpoint="http://127.0.0.1:9250")
        self.assertEqual(svc._backend.name, "chrome_cdp")
        self.assertEqual(svc._cdp_endpoint, "http://127.0.0.1:9250")


if __name__ == "__main__":
    unittest.main()
