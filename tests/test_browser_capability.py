from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claw_v2.browser_capability import BrowserCapability, BrowserCapabilityError

_VERSION_OK = b'{"Browser":"Chrome/148","User-Agent":"Chrome"}'
_VERSION_HEADLESS = b'{"Browser":"HeadlessChrome/148","User-Agent":"HeadlessChrome"}'
_NEW_TAB_OK = b'{"id":"probe-tab-1"}'


class _JsonResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self._body


class _ClosableResponse:
    status = 200

    def __init__(self) -> None:
        self.closed = False

    def read(self, _limit: int = -1) -> bytes:
        return b'{"Browser":"Chrome/148"}'

    def close(self) -> None:
        self.closed = True


class _FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict) -> None:
        self.events.append((event_type, payload))


def _request_url(request: object) -> str:
    return getattr(request, "full_url", str(request))


def _request_method(request: object) -> str:
    get_method = getattr(request, "get_method", None)
    return get_method() if callable(get_method) else "GET"


class _FakeCdpEndpoint:
    """Routes the preflight's CDP HTTP calls (/json/version, /json/new,
    /json/close/<id>) with scriptable per-call behavior.

    ``version`` and ``new_tab`` are lists consumed one item per call; an item
    is either a response body (bytes), an Exception to raise, or a
    _JsonResponse. The last item repeats once a list is exhausted.
    """

    def __init__(
        self,
        *,
        version: list[object] | None = None,
        new_tab: list[object] | None = None,
        close: list[object] | None = None,
    ) -> None:
        self.version = list(version or [_VERSION_OK])
        self.new_tab = list(new_tab or [_NEW_TAB_OK])
        self.close = list(close or [b""])
        self.calls: list[tuple[str, str]] = []

    def _next(self, queue: list[object]) -> object:
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, bytes):
            return _JsonResponse(item)
        return item

    def __call__(self, request: object, *, timeout: float) -> object:
        url = _request_url(request)
        method = _request_method(request)
        self.calls.append((method, url))
        if url.endswith("/json/version"):
            return self._next(self.version)
        if "/json/new" in url:
            return self._next(self.new_tab)
        if "/json/close/" in url:
            return self._next(self.close)
        raise AssertionError(f"unexpected CDP probe URL: {url}")

    def urls(self) -> list[str]:
        return [url for _method, url in self.calls]


class BrowserCapabilityTests(unittest.TestCase):
    def test_ensure_ready_focuses_existing_visible_cdp_when_cdp_already_responds(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint()
        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        endpoint = capability.ensure_ready()

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        self.assertEqual(
            cdp.calls,
            [
                ("GET", "http://127.0.0.1:9250/json/version"),
                ("PUT", "http://127.0.0.1:9250/json/new?about:blank"),
                ("GET", "http://127.0.0.1:9250/json/close/probe-tab-1"),
            ],
        )
        chrome_factory.assert_called_once()
        self.assertEqual(chrome_factory.call_args.kwargs["port"], 9250)
        self.assertEqual(chrome_factory.call_args.kwargs["profile_dir"], "~/.claw/chrome-profile")
        chrome.ensure.assert_called_once_with(headless=False)
        self.assertEqual(
            [event_type for event_type, _ in observe.events],
            [
                "browser_capability_preflight_started",
                "browser_capability_preflight_ok",
            ],
        )
        self.assertFalse(observe.events[-1][1]["started_chrome"])
        self.assertTrue(observe.events[-1][1]["focused_chrome"])

    def test_ensure_ready_can_skip_visible_focus_for_readonly_probe(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint()
        chrome_factory = MagicMock()
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        endpoint = capability.ensure_ready(visible=False)

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        chrome_factory.assert_not_called()
        self.assertFalse(observe.events[-1][1]["started_chrome"])
        self.assertFalse(observe.events[-1][1]["focused_chrome"])

    def test_ensure_ready_starts_managed_chrome_when_initial_probe_fails(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint(version=[OSError("connection refused"), _VERSION_OK])
        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        endpoint = capability.ensure_ready(profile_dir="/tmp/profile")

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        # Dead endpoint: version fails -> respawn -> re-probe covers version
        # AND the tab lifecycle.
        self.assertEqual(
            cdp.urls(),
            [
                "http://127.0.0.1:9250/json/version",
                "http://127.0.0.1:9250/json/version",
                "http://127.0.0.1:9250/json/new?about:blank",
                "http://127.0.0.1:9250/json/close/probe-tab-1",
            ],
        )
        chrome_factory.assert_called_once()
        chrome.ensure.assert_called_once_with(headless=False)
        self.assertEqual(
            [event_type for event_type, _ in observe.events],
            [
                "browser_capability_preflight_started",
                "browser_capability_preflight_ok",
            ],
        )
        self.assertTrue(observe.events[-1][1]["started_chrome"])

    def test_ensure_ready_emits_failed_with_human_error_when_chrome_cannot_start(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint(version=[OSError("connection refused")])
        chrome = MagicMock()
        chrome.ensure.side_effect = RuntimeError("profile belongs to another Chrome")
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        with self.assertRaises(BrowserCapabilityError) as ctx:
            capability.ensure_ready()

        self.assertIn("Necesito abrir/login Chrome", str(ctx.exception))
        self.assertIn("profile belongs to another Chrome", str(ctx.exception))
        self.assertEqual(ctx.exception.endpoint, "http://127.0.0.1:9250")
        self.assertEqual(
            [event_type for event_type, _ in observe.events],
            [
                "browser_capability_preflight_started",
                "browser_capability_preflight_failed",
            ],
        )
        self.assertEqual(observe.events[-1][1]["stage"], "start_chrome")

    def test_ensure_ready_relaunches_when_existing_cdp_is_headless(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint(version=[_VERSION_HEADLESS, _VERSION_OK])
        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        endpoint = capability.ensure_ready()

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        chrome.ensure.assert_called_once_with(headless=False)
        self.assertTrue(observe.events[-1][1]["started_chrome"])
        # Headless rejection happens at the version stage: no tab probe is
        # attempted against the headless Chrome.
        self.assertEqual(
            cdp.urls()[:2],
            [
                "http://127.0.0.1:9250/json/version",
                "http://127.0.0.1:9250/json/version",
            ],
        )

    def test_ensure_ready_rejects_port_above_tcp_range(self) -> None:
        urlopen = MagicMock(return_value=_JsonResponse(_VERSION_OK))
        chrome_factory = MagicMock()
        capability = BrowserCapability(
            chrome_factory=chrome_factory,
            urlopen=urlopen,
        )

        with self.assertRaises(BrowserCapabilityError) as ctx:
            capability.ensure_ready(port=65536)

        self.assertIn("puerto CDP invalido", str(ctx.exception))
        urlopen.assert_not_called()
        chrome_factory.assert_not_called()

    def test_probe_closes_non_context_manager_response(self) -> None:
        response = _ClosableResponse()
        capability = BrowserCapability(urlopen=MagicMock(return_value=response))

        self.assertIsNone(capability._probe_json_version("http://127.0.0.1:9250"))

        self.assertTrue(response.closed)


class BrowserCapabilityZombieTests(unittest.TestCase):
    """A zombie Chrome answers /json/version but cannot open tabs
    ("Failed to open new tab - no browser is open"): the preflight must treat
    it as unhealthy and respawn via ManagedChrome.ensure()."""

    def test_zombie_cdp_triggers_respawn_and_reprobe(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint(
            new_tab=[
                RuntimeError(
                    "{'code': -32000, 'message': 'Failed to open new tab - no browser is open'}"
                ),
                _NEW_TAB_OK,
            ],
        )
        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        endpoint = capability.ensure_ready()

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        chrome_factory.assert_called_once()
        chrome.ensure.assert_called_once_with(headless=False)
        # First round: version OK, tab create fails (zombie). Second round
        # (after respawn): version + tab create + tab close all healthy.
        self.assertEqual(
            cdp.urls(),
            [
                "http://127.0.0.1:9250/json/version",
                "http://127.0.0.1:9250/json/new?about:blank",
                "http://127.0.0.1:9250/json/version",
                "http://127.0.0.1:9250/json/new?about:blank",
                "http://127.0.0.1:9250/json/close/probe-tab-1",
            ],
        )
        event_types = [event_type for event_type, _ in observe.events]
        self.assertIn("browser_capability_probe_zombie", event_types)
        self.assertIn("Failed to open new tab", observe.events[1][1]["error"])
        self.assertEqual(event_types[-1], "browser_capability_preflight_ok")
        self.assertTrue(observe.events[-1][1]["started_chrome"])

    def test_zombie_persisting_after_respawn_raises(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint(
            new_tab=[RuntimeError("Failed to open new tab - no browser is open")],
        )
        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        with self.assertRaises(BrowserCapabilityError) as ctx:
            capability.ensure_ready()

        self.assertIn("no puede crear pestañas", str(ctx.exception))
        chrome.ensure.assert_called_once_with(headless=False)
        failed = [
            payload
            for event_type, payload in observe.events
            if event_type == "browser_capability_preflight_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["stage"], "verify_after_start")

    def test_missing_target_id_is_treated_as_zombie(self) -> None:
        observe = _FakeObserve()
        cdp = _FakeCdpEndpoint(new_tab=[b"{}", _NEW_TAB_OK])
        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=cdp,
        )

        endpoint = capability.ensure_ready()

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        chrome.ensure.assert_called_once_with(headless=False)
        self.assertIn(
            "browser_capability_probe_zombie",
            [event_type for event_type, _ in observe.events],
        )

    def test_healthy_probe_always_closes_created_tab(self) -> None:
        cdp = _FakeCdpEndpoint()
        capability = BrowserCapability(chrome_factory=MagicMock(), urlopen=cdp)

        endpoint = capability.ensure_ready(visible=False)

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        self.assertIn(("GET", "http://127.0.0.1:9250/json/close/probe-tab-1"), cdp.calls)

    def test_tab_close_failure_does_not_make_endpoint_unhealthy(self) -> None:
        cdp = _FakeCdpEndpoint(close=[OSError("close boom")])
        chrome_factory = MagicMock()
        capability = BrowserCapability(chrome_factory=chrome_factory, urlopen=cdp)

        endpoint = capability.ensure_ready(visible=False)

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        chrome_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
