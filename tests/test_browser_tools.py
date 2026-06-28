from __future__ import annotations

import concurrent.futures
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.acted: list[tuple[str, str, str | None, bool]] = []
        self.screenshots: list[str] = []

    def navigate(self, url: str) -> RawPage:
        self.navigated.append(url)
        self._i += 1
        return self._pages[min(self._i, len(self._pages) - 1)]

    def snapshot(self, full: bool = False) -> RawPage:
        return self._pages[min(self._i, len(self._pages) - 1)]

    def act(
        self, selector: str, action: str, text: str | None = None, *, clear: bool = True
    ) -> RawPage:
        self.acted.append((selector, action, text, clear))
        self._i += 1
        return self._pages[min(self._i, len(self._pages) - 1)]

    def screenshot(self, path: str) -> bool:
        self.screenshots.append(path)
        return True

    def console(self, clear: bool = False) -> list[str]:
        return ["log: ok"]


def _page(url: str, *elements: RawElement, text: str = "body text", login: bool = False) -> RawPage:
    return RawPage(url=url, title=url, text=text, elements=list(elements), login_or_challenge=login)


class _ConcurrentNavigateBackend:
    name = "fake"

    def __init__(
        self, *, block_first: bool = False, block_all: bool = False, fail_first: bool = False
    ) -> None:
        self.block_first = block_first
        self.block_all = block_all
        self.fail_first = fail_first
        self.calls: list[str] = []
        self.first_entered = threading.Event()
        self.two_active = threading.Event()
        self.release_first = threading.Event()
        self.release_all = threading.Event()
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def navigate(self, url: str) -> RawPage:
        with self._lock:
            index = len(self.calls)
            self.calls.append(url)
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if index == 0:
                self.first_entered.set()
            if self._active >= 2:
                self.two_active.set()
        try:
            if self.block_all:
                self.release_all.wait(timeout=2)
            elif self.block_first and index == 0:
                self.release_first.wait(timeout=2)
            if self.fail_first and index == 0:
                raise RuntimeError("net::ERR loading https://x.test/p?token=secret")
            slug = url.removeprefix("https://").split(".", 1)[0]
            return _page(
                url, RawElement(f"#{slug}", "button", slug.upper(), slug.upper(), None, None)
            )
        finally:
            with self._lock:
                self._active -= 1

    def snapshot(self, full: bool = False) -> RawPage:
        raise AssertionError("unused")

    def act(
        self, selector: str, action: str, text: str | None = None, *, clear: bool = True
    ) -> RawPage:
        raise AssertionError("unused")

    def screenshot(self, path: str) -> bool:
        with self._lock:
            self.calls.append(f"screenshot:{path}")
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            if self._active >= 2:
                self.two_active.set()
        try:
            return True
        finally:
            with self._lock:
                self._active -= 1

    def console(self, clear: bool = False) -> list[str]:
        return []


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


class SessionActionSerializationTests(unittest.TestCase):
    def test_same_session_actions_do_not_enter_backend_concurrently(self) -> None:
        backend = _ConcurrentNavigateBackend(block_first=True)
        svc = BrowserToolService(backend=backend)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(svc.navigate, "same", "https://a.test")
            self.assertTrue(backend.first_entered.wait(timeout=1))
            second = executor.submit(svc.navigate, "same", "https://b.test")
            time.sleep(0.05)
            self.assertEqual(backend.calls, ["https://a.test"])
            self.assertEqual(backend.max_active, 1)

            backend.release_first.set()
            results = [first.result(timeout=2), second.result(timeout=2)]

        self.assertTrue(all(result.success for result in results))
        self.assertEqual(backend.calls, ["https://a.test", "https://b.test"])
        self.assertEqual(backend.max_active, 1)

    def test_different_sessions_can_enter_backend_concurrently(self) -> None:
        backend = _ConcurrentNavigateBackend(block_all=True)
        svc = BrowserToolService(backend=backend)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(svc.navigate, "session-a", "https://a.test")
            second = executor.submit(svc.navigate, "session-b", "https://b.test")
            try:
                self.assertTrue(backend.two_active.wait(timeout=1))
            finally:
                backend.release_all.set()
            results = [first.result(timeout=2), second.result(timeout=2)]

        self.assertTrue(all(result.success for result in results))
        self.assertEqual(backend.max_active, 2)
        self.assertEqual(svc._session("session-a").refs["@e1"].selector, "#a")
        self.assertEqual(svc._session("session-b").refs["@e1"].selector, "#b")

    def test_error_path_releases_session_action_lock_and_emits_events(self) -> None:
        backend = _ConcurrentNavigateBackend(block_first=True, fail_first=True)
        svc = BrowserToolService(backend=backend)
        events: list[tuple[str, dict]] = []

        class _Obs:
            def emit(self, event_type, payload=None):
                events.append((event_type, payload or {}))

        svc.observe = _Obs()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(svc.navigate, "same", "https://boom.test")
            self.assertTrue(backend.first_entered.wait(timeout=1))
            second = executor.submit(svc.navigate, "same", "https://ok.test")
            time.sleep(0.05)
            self.assertEqual(backend.calls, ["https://boom.test"])

            backend.release_first.set()
            failed = first.result(timeout=2)
            recovered = second.result(timeout=2)

        self.assertFalse(failed.success)
        self.assertTrue(recovered.success)
        self.assertEqual(backend.calls, ["https://boom.test", "https://ok.test"])
        self.assertEqual(backend.max_active, 1)
        kinds = [kind for kind, _ in events]
        self.assertIn("browser_tool_action_failed", kinds)
        self.assertIn("browser_tool_action_completed", kinds)
        for _, payload in events:
            self.assertNotIn("token=secret", str(payload))

    def test_same_session_screenshot_serializes_with_navigation(self) -> None:
        backend = _ConcurrentNavigateBackend(block_first=True)
        svc = BrowserToolService(backend=backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            shot_path = str(Path(tmpdir) / "shot.png")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(svc.navigate, "same", "https://a.test")
                self.assertTrue(backend.first_entered.wait(timeout=1))
                second = executor.submit(svc.screenshot, "same", shot_path)
                time.sleep(0.05)
                self.assertEqual(backend.calls, ["https://a.test"])
                self.assertEqual(backend.max_active, 1)

                backend.release_first.set()
                navigated = first.result(timeout=2)
                screenshot = second.result(timeout=2)

        self.assertTrue(navigated.success)
        self.assertTrue(screenshot.success)
        self.assertEqual(screenshot.screenshot_path, shot_path)
        self.assertEqual(backend.max_active, 1)


class ObserveIsolationTests(unittest.TestCase):
    def test_concurrent_sessions_emit_only_to_their_explicit_observer(self) -> None:
        backend = _ConcurrentNavigateBackend(block_all=True)
        svc = BrowserToolService(backend=backend)
        events_a: list[tuple[str, dict]] = []
        events_b: list[tuple[str, dict]] = []

        class _Obs:
            def __init__(self, target: list[tuple[str, dict]]) -> None:
                self._target = target

            def emit(self, event_type, payload=None):
                self._target.append((event_type, payload or {}))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                svc.navigate,
                "session-a",
                "https://a.test",
                observe=_Obs(events_a),
            )
            second = executor.submit(
                svc.navigate,
                "session-b",
                "https://b.test",
                observe=_Obs(events_b),
            )
            try:
                self.assertTrue(backend.two_active.wait(timeout=1))
            finally:
                backend.release_all.set()
            self.assertTrue(first.result(timeout=2).success)
            self.assertTrue(second.result(timeout=2).success)

        rendered_a = "\n".join(str(payload) for _, payload in events_a)
        rendered_b = "\n".join(str(payload) for _, payload in events_b)
        self.assertIn("a.test", rendered_a)
        self.assertNotIn("b.test", rendered_a)
        self.assertIn("b.test", rendered_b)
        self.assertNotIn("a.test", rendered_b)

    def test_explicit_observer_receives_events_for_all_browser_actions(self) -> None:
        page = _page("https://x.test", RawElement("#q", "textbox", "Search", "", None, "text"))
        svc = BrowserToolService(backend=_FakeBackend([page, page, page]))
        events: list[tuple[str, dict]] = []

        class _Obs:
            def emit(self, event_type, payload=None):
                events.append((event_type, payload or {}))

        observe = _Obs()
        svc.navigate("session-a", "https://x.test", observe=observe)
        svc.snapshot("session-a", observe=observe)
        svc.click("session-a", "@e1", observe=observe)
        svc.type("session-a", "@e1", "hello", observe=observe)
        with tempfile.TemporaryDirectory() as tmpdir:
            svc.screenshot("session-a", str(Path(tmpdir) / "shot.png"), observe=observe)

        started_actions = [
            payload.get("action")
            for kind, payload in events
            if kind == "browser_tool_action_started"
        ]
        self.assertEqual(
            started_actions,
            ["navigate", "snapshot", "click", "type", "screenshot"],
        )


class InteractionTests(unittest.TestCase):
    def test_click_resolves_ref_to_selector(self) -> None:
        p1 = _page("https://x.test", RawElement("#post", "button", "Post", "Post", None, None))
        p2 = _page("https://x.test/done", RawElement("#ok", "button", "OK", "OK", None, None))
        backend = _FakeBackend([p1, p2])
        svc = BrowserToolService(backend=backend)
        svc.navigate("s", "https://x.test")
        r = svc.click("s", "@e1")
        self.assertTrue(r.success)
        self.assertEqual(backend.acted[-1], ("#post", "click", None, True))

    def test_type_passes_text(self) -> None:
        p1 = _page("https://x.test", RawElement("#q", "textbox", "Search", "", None, "text"))
        backend = _FakeBackend([p1, p1])
        svc = BrowserToolService(backend=backend)
        svc.navigate("s", "https://x.test")
        r = svc.type("s", "@e1", "hello")
        self.assertTrue(r.success)
        self.assertEqual(backend.acted[-1], ("#q", "type", "hello", True))

    def test_type_clear_false_appends_without_clearing(self) -> None:
        p1 = _page("https://x.test", RawElement("#q", "textbox", "Search", "", None, "text"))
        backend = _FakeBackend([p1, p1])
        svc = BrowserToolService(backend=backend)
        svc.navigate("s", "https://x.test")
        r = svc.type("s", "@e1", " appended", clear=False)
        self.assertTrue(r.success)
        self.assertEqual(backend.acted[-1], ("#q", "type", " appended", False))

    def test_stale_ref_after_version_change_fails_clearly(self) -> None:
        p1 = _page("https://a.test", RawElement("#a", "button", "A", "A", None, None))
        p2 = _page("https://b.test", RawElement("#b", "button", "B", "B", None, None))
        svc = BrowserToolService(backend=_FakeBackend([p1, p2]))
        svc.navigate("s", "https://a.test")
        svc.navigate("s", "https://b.test")  # ref map replaced
        r = svc.click("s", "@e99")
        self.assertFalse(r.success)
        self.assertEqual(r.error, "stale_ref: @e99 not in current snapshot")

    def test_screenshot_routes_through_service_backend_adapter(self) -> None:
        backend = _FakeBackend([_page("https://x.test")])
        svc = BrowserToolService(backend=backend)

        with tempfile.TemporaryDirectory() as tmpdir:
            shot_path = str(Path(tmpdir) / "shot.png")
            result = svc.screenshot("s", shot_path)

        self.assertTrue(result.success)
        self.assertEqual(result.screenshot_path, shot_path)
        self.assertEqual(backend.screenshots, [shot_path])

    def test_screenshot_uses_session_aware_backend_when_available(self) -> None:
        calls: list[tuple[str, str]] = []

        class _SessionAwareBackend(_FakeBackend):
            def screenshot_for_session(self, session_id: str, path: str) -> bool:
                calls.append((session_id, path))
                return True

            def screenshot(self, path: str) -> bool:
                raise AssertionError("service bypassed session-aware screenshot")

        svc = BrowserToolService(backend=_SessionAwareBackend([_page("https://x.test")]))

        with tempfile.TemporaryDirectory() as tmpdir:
            shot_path = str(Path(tmpdir) / "shot.png")
            result = svc.screenshot("session-a", shot_path)

        self.assertTrue(result.success)
        self.assertEqual(calls, [("session-a", shot_path)])


from claw_v2.browser_tools import (
    SNAPSHOT_BACKEND_TEXT_CHARS,
    SNAPSHOT_FULL_TEXT_CHARS,
    SNAPSHOT_MAX_ELEMENTS,
    SNAPSHOT_MAX_TEXT_CHARS,
    ChromeCdpBrowserBackend,
)


class ChromeCdpConnectionCleanupTests(unittest.TestCase):
    def _run_with_fake_cdp(self, callback, *, connect_error: Exception | None = None):
        events: list[str] = []

        class _FakePage:
            def __init__(self, context, marker: str) -> None:
                self.context = context
                self.marker = marker
                self.url = "about:blank"
                self.closed = False

            def goto(self, url, wait_until=None, timeout=None):
                self.url = url

            def wait_for_load_state(self, state, timeout=None):
                return None

            def evaluate(self, script, limit=None):
                label = self.url.removeprefix("https://").split(".", 1)[0] or self.marker
                return {
                    "url": self.url,
                    "title": self.url,
                    "text": f"text {label}",
                    "elements": [
                        {
                            "selector": f"#{label}",
                            "role": "button",
                            "label": label.upper(),
                            "text": label.upper(),
                            "href": None,
                            "input_type": None,
                        }
                    ],
                }

            def click(self, selector, timeout=None):
                return None

            def fill(self, selector, text, timeout=None):
                events.append(f"fill:{self.marker}:{selector}:{text}")

            def type(self, selector, text, timeout=None):
                events.append(f"type:{self.marker}:{selector}:{text}")

            def screenshot(self, path, full_page=True):
                events.append(f"screenshot:{self.marker}:{path}")

            def is_closed(self):
                return self.closed

        class _FakeContext:
            def __init__(self, index: int) -> None:
                self.index = index
                self.pages: list[_FakePage] = []
                self.closed = False

            def new_page(self):
                page = _FakePage(self, f"page-{self.index}-{len(self.pages) + 1}")
                self.pages.append(page)
                return page

            def close(self):
                if self.closed:
                    return
                self.closed = True
                for page in self.pages:
                    page.closed = True
                events.append(f"context.close:{self.index}")

        class _FakeBrowser:
            def __init__(self) -> None:
                self.contexts: list[_FakeContext] = []
                self.closed = False

            def new_context(self, **kwargs):
                context = _FakeContext(len(self.contexts) + 1)
                self.contexts.append(context)
                return context

            def is_connected(self):
                return not self.closed

            def close(self):
                self.closed = True
                events.append("browser.close")

        class _FakePlaywright:
            pass

        class _FakePlaywrightManager:
            def start(self):
                events.append("playwright.start")
                return _FakePlaywright()

            def stop(self):
                events.append("playwright.stop")

        def _connect(*args, **kwargs):
            if connect_error is not None:
                raise connect_error
            return fake_browser

        fake_browser = _FakeBrowser()
        with (
            patch(
                "claw_v2.browser._require_sync_playwright", return_value=_FakePlaywrightManager()
            ),
            patch("claw_v2.browser._cdp_connect", side_effect=_connect),
        ):
            backend = ChromeCdpBrowserBackend(cdp_endpoint="http://127.0.0.1:0")
            try:
                return callback(backend, fake_browser, events)
            finally:
                backend.close()

    def test_service_sessions_get_distinct_cdp_contexts_and_pages(self) -> None:
        def _case(backend, fake_browser, events):
            svc = BrowserToolService(backend=backend)

            a = svc.navigate("session-a", "https://a.test")
            b = svc.navigate("session-b", "https://b.test")

            self.assertTrue(a.success)
            self.assertTrue(b.success)
            self.assertEqual(svc._session("session-a").refs["@e1"].selector, "#a")
            self.assertEqual(svc._session("session-b").refs["@e1"].selector, "#b")
            self.assertEqual(len(fake_browser.contexts), 2)
            self.assertIsNot(fake_browser.contexts[0], fake_browser.contexts[1])
            self.assertIsNot(fake_browser.contexts[0].pages[0], fake_browser.contexts[1].pages[0])

        self._run_with_fake_cdp(_case)

    def test_same_session_reuses_owned_cdp_page(self) -> None:
        def _case(backend, fake_browser, events):
            first = backend._with_page("same", lambda page: page)
            second = backend._with_page("same", lambda page: page)

            self.assertIs(first, second)
            self.assertEqual(len(fake_browser.contexts), 1)

        self._run_with_fake_cdp(_case)

    def test_run_on_worker_submits_while_lifecycle_lock_is_held(self) -> None:
        backend = ChromeCdpBrowserBackend(cdp_endpoint="http://127.0.0.1:0")
        real_executor = backend._executor
        submit_lock_states: list[bool] = []

        class _Future:
            def __init__(self, value) -> None:
                self._value = value

            def result(self):
                return self._value

        class _Executor:
            def submit(self, fn):
                submit_lock_states.append(backend._lifecycle_lock.locked())
                return _Future(fn())

        backend._executor = _Executor()
        try:
            self.assertEqual(backend._run_on_worker(lambda: "ok"), "ok")
        finally:
            real_executor.shutdown(wait=False, cancel_futures=True)

        self.assertEqual(submit_lock_states, [True])

    def test_read_page_uses_full_text_limit_when_requested(self) -> None:
        backend = ChromeCdpBrowserBackend(cdp_endpoint="http://127.0.0.1:0")
        marker = "END-MARKER"
        body = ("x" * (SNAPSHOT_BACKEND_TEXT_CHARS + 50)) + marker
        limits: list[int] = []

        class _FakePage:
            def evaluate(self, script, limit):
                limits.append(limit)
                return {
                    "url": "https://x.test",
                    "title": "Example",
                    "text": body[:limit],
                    "elements": [],
                }

        try:
            capped = backend._read_page(_FakePage(), full=False)
            full = backend._read_page(_FakePage(), full=True)
        finally:
            backend.close()

        self.assertEqual(limits, [SNAPSHOT_BACKEND_TEXT_CHARS, SNAPSHOT_FULL_TEXT_CHARS])
        self.assertNotIn(marker, capped.text)
        self.assertIn(marker, full.text)

    def test_different_sessions_are_serialized_on_cdp_worker_with_distinct_pages(self) -> None:
        def _case(backend, fake_browser, events):
            entered_first = threading.Event()
            entered_second = threading.Event()
            release_first = threading.Event()
            pages = []

            def first(page):
                pages.append(page)
                entered_first.set()
                self.assertTrue(release_first.wait(timeout=2))
                return page.marker

            def second(page):
                pages.append(page)
                entered_second.set()
                return page.marker

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(backend._with_page, "session-a", first)
                self.assertTrue(entered_first.wait(timeout=1))
                second_future = executor.submit(backend._with_page, "session-b", second)
                time.sleep(0.05)
                self.assertFalse(entered_second.is_set())
                release_first.set()
                self.assertEqual(first_future.result(timeout=2), "page-1-1")
                self.assertEqual(second_future.result(timeout=2), "page-2-1")

            self.assertEqual(len(pages), 2)
            self.assertIsNot(pages[0], pages[1])

        self._run_with_fake_cdp(_case)

    def test_with_page_does_not_discard_session_on_callback_error(self) -> None:
        def _case(backend, fake_browser, events):
            seen = []

            def boom(page):
                seen.append(page)
                raise RuntimeError("callback failed")

            with self.assertRaises(RuntimeError):
                backend._with_page("same", boom)

            self.assertFalse(seen[0].is_closed())
            self.assertNotIn("context.close:1", events)
            recovered = backend._with_page("same", lambda page: page)
            self.assertIs(recovered, seen[0])
            self.assertEqual(len(fake_browser.contexts), 1)

        self._run_with_fake_cdp(_case)

    def test_type_clear_controls_fill_vs_append_type(self) -> None:
        def _case(backend, fake_browser, events):
            svc = BrowserToolService(backend=backend)

            svc.navigate("same", "https://a.test")
            self.assertTrue(svc.type("same", "@e1", "replace").success)
            self.assertTrue(svc.type("same", "@e1", " append", clear=False).success)

            self.assertIn("fill:page-1-1:#a:replace", events)
            self.assertIn("type:page-1-1:#a: append", events)

        self._run_with_fake_cdp(_case)

    def test_connect_failure_stops_playwright_manager(self) -> None:
        def _case(backend, fake_browser, events):
            with self.assertRaisesRegex(RuntimeError, "connect failed"):
                backend._with_page("same", lambda page: page)
            self.assertIn("playwright.start", events)
            self.assertIn("playwright.stop", events)
            self.assertNotIn("browser.close", events)

        self._run_with_fake_cdp(_case, connect_error=RuntimeError("connect failed"))

    def test_close_cleans_cdp_browser_connection_and_playwright(self) -> None:
        def _case(backend, fake_browser, events):
            self.assertEqual(backend._with_page("same", lambda page: page.marker), "page-1-1")

            backend.close()

            self.assertIn("context.close:1", events)
            self.assertIn("browser.close", events)
            self.assertIn("playwright.stop", events)

        self._run_with_fake_cdp(_case)


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

    def test_full_snapshot_bypasses_body_text_cap(self) -> None:
        marker = "END-MARKER"
        page = _page(
            "https://x.test",
            text=("x" * (SNAPSHOT_MAX_TEXT_CHARS + 250)) + marker,
        )
        svc = BrowserToolService(backend=_FakeBackend([page]))

        capped = svc.snapshot("s", full=False)
        full = svc.snapshot("s", full=True)

        self.assertNotIn(marker, capped.snapshot)
        self.assertIn(marker, full.snapshot)

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

            def act(self, s, a, t=None, *, clear=True):
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
