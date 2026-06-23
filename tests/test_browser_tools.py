from __future__ import annotations

import concurrent.futures
import threading
import time
import unittest

import claw_v2.browser_tools as browser_tools_mod
from claw_v2.browser_tools import (
    BrowserElementRef,
    BrowserToolResult,
    RawElement,
    RawPage,
    _redact_url,
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
        self.navigated: list[str] = []
        self.acted: list[tuple[str, str, str | None]] = []

    def navigate(self, url: str) -> RawPage:
        self.navigated.append(url)
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
        v1 = svc._session("sess1").ref_version
        svc.navigate("sess1", "https://b.test")
        sess = svc._session("sess1")
        self.assertGreater(sess.ref_version, v1)
        self.assertIn("@e1", sess.refs)
        self.assertEqual(sess.refs["@e1"].selector, "#b")

    def test_navigate_blocks_file_scheme_before_backend(self) -> None:
        backend = _FakeBackend([_page("https://unused.test")])
        svc = BrowserToolService(backend=backend)

        with self.assertRaises(ValueError):
            svc.navigate("sess1", "file:///etc/passwd")

        self.assertEqual(backend.navigated, [])

    def test_navigate_blocks_chrome_scheme_before_backend(self) -> None:
        backend = _FakeBackend([_page("https://unused.test")])
        svc = BrowserToolService(backend=backend)

        with self.assertRaises(ValueError):
            svc.navigate("sess1", "chrome://version")

        self.assertEqual(backend.navigated, [])

    def test_navigate_blocks_non_http_schemes_and_malformed_urls_before_backend(self) -> None:
        blocked_urls = (
            "chrome-extension://abc/options.html",
            "data:text/html,<h1>x</h1>",
            "javascript:alert(1)",
            "about:blank",
            "ftp://example.com/file",
            "gopher://example.com/file",
            "https:///missing-host",
            "example.com/no-scheme",
        )
        for url in blocked_urls:
            with self.subTest(url=url):
                backend = _FakeBackend([_page("https://unused.test")])
                svc = BrowserToolService(backend=backend)

                with self.assertRaises(ValueError):
                    svc.navigate("sess1", url)

                self.assertEqual(backend.navigated, [])

    def test_navigate_allows_https_scheme(self) -> None:
        backend = _FakeBackend([_page("https://example.com")])
        svc = BrowserToolService(backend=backend)

        result = svc.navigate("sess1", "https://example.com")

        self.assertTrue(result.success)
        self.assertEqual(backend.navigated, ["https://example.com"])


class SessionRegistryThreadSafetyTests(unittest.TestCase):
    def test_concurrent_same_session_creates_one_session(self) -> None:
        svc = BrowserToolService(backend=_FakeBackend([_page("https://x.test")]))
        original_session_type = browser_tools_mod.BrowserToolSession
        entered_constructor = threading.Event()
        release_constructor = threading.Event()
        start_contenders = threading.Event()
        constructor_count = 0
        count_lock = threading.Lock()

        def slow_session_constructor(*args, **kwargs):
            nonlocal constructor_count
            with count_lock:
                constructor_count += 1
                current = constructor_count
            if current == 1:
                entered_constructor.set()
                if not release_constructor.wait(timeout=2):
                    raise AssertionError("timed out waiting to release session constructor")
            return original_session_type(*args, **kwargs)

        def get_same_session():
            self.assertTrue(start_contenders.wait(timeout=2))
            return svc._session("same")

        browser_tools_mod.BrowserToolSession = slow_session_constructor
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=9)
        try:
            first = executor.submit(svc._session, "same")
            self.assertTrue(entered_constructor.wait(timeout=1))
            contenders = [executor.submit(get_same_session) for _ in range(8)]
            start_contenders.set()

            time.sleep(0.02)
            with count_lock:
                self.assertEqual(constructor_count, 1)

            release_constructor.set()
            sessions = [first.result(timeout=2), *(f.result(timeout=2) for f in contenders)]
        finally:
            release_constructor.set()
            start_contenders.set()
            browser_tools_mod.BrowserToolSession = original_session_type
            executor.shutdown(wait=True, cancel_futures=True)

        self.assertEqual({id(sess) for sess in sessions}, {id(sessions[0])})
        with svc._sessions_lock:
            self.assertEqual(set(svc._sessions), {"same"})

    def test_concurrent_different_session_ids_do_not_corrupt_registry(self) -> None:
        svc = BrowserToolService(backend=_FakeBackend([_page("https://x.test")]))
        session_ids = [f"s{i}" for i in range(32)]

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            sessions = list(executor.map(svc._session, session_ids))

        self.assertEqual({sess.session_id for sess in sessions}, set(session_ids))
        self.assertEqual(len({id(sess) for sess in sessions}), len(session_ids))
        with svc._sessions_lock:
            self.assertEqual(set(svc._sessions), set(session_ids))

    def test_ref_maps_remain_isolated_per_session(self) -> None:
        p1 = _page("https://a.test", RawElement("#a", "button", "A", "A", None, None))
        p2 = _page("https://b.test", RawElement("#b", "button", "B", "B", None, None))
        svc = BrowserToolService(backend=_FakeBackend([p1, p2]))

        svc.navigate("session-a", "https://a.test")
        svc.navigate("session-b", "https://b.test")

        session_a = svc._session("session-a")
        session_b = svc._session("session-b")
        self.assertIsNot(session_a, session_b)
        self.assertEqual(session_a.refs["@e1"].selector, "#a")
        self.assertEqual(session_b.refs["@e1"].selector, "#b")
        self.assertEqual(session_a.ref_version, 1)
        self.assertEqual(session_b.ref_version, 1)


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
    def test_redact_url_strips_userinfo_query_and_fragment(self) -> None:
        redacted = _redact_url("https://user:pass@example.com:8443/a?token=x#frag")

        self.assertEqual(redacted, "https://example.com:8443/a")
        self.assertNotIn("user", redacted)
        self.assertNotIn("pass", redacted)
        self.assertNotIn("token", redacted)
        self.assertNotIn("frag", redacted)

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
