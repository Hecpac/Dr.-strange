from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from claw_v2.browser_capability import BrowserCapability, BrowserCapabilityError


class _FakeResponse:
    status = 200

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return b'{"Browser":"Chrome/148","User-Agent":"HeadlessChrome"}'


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


class BrowserCapabilityTests(unittest.TestCase):
    def test_ensure_ready_does_not_relaunch_when_cdp_already_responds(self) -> None:
        observe = _FakeObserve()
        urlopen = MagicMock(return_value=_FakeResponse())
        chrome_factory = MagicMock()
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=urlopen,
        )

        endpoint = capability.ensure_ready()

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        urlopen.assert_called_once_with(
            "http://127.0.0.1:9250/json/version",
            timeout=2.0,
        )
        chrome_factory.assert_not_called()
        self.assertEqual(
            [event_type for event_type, _ in observe.events],
            [
                "browser_capability_preflight_started",
                "browser_capability_preflight_ok",
            ],
        )
        self.assertFalse(observe.events[-1][1]["started_chrome"])

    def test_ensure_ready_starts_managed_chrome_when_initial_probe_fails(self) -> None:
        observe = _FakeObserve()
        probe_calls: list[str] = []

        def fake_urlopen(url: str, *, timeout: float) -> _FakeResponse:
            probe_calls.append(url)
            if len(probe_calls) == 1:
                raise OSError("connection refused")
            return _FakeResponse()

        chrome = MagicMock()
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=fake_urlopen,
        )

        endpoint = capability.ensure_ready(profile_dir="/tmp/profile")

        self.assertEqual(endpoint, "http://127.0.0.1:9250")
        self.assertEqual(
            probe_calls,
            [
                "http://127.0.0.1:9250/json/version",
                "http://127.0.0.1:9250/json/version",
            ],
        )
        chrome_factory.assert_called_once()
        chrome.ensure.assert_called_once()
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

        def fake_urlopen(url: str, *, timeout: float) -> _FakeResponse:
            raise OSError("connection refused")

        chrome = MagicMock()
        chrome.ensure.side_effect = RuntimeError("profile belongs to another Chrome")
        chrome_factory = MagicMock(return_value=chrome)
        capability = BrowserCapability(
            observe=observe,
            chrome_factory=chrome_factory,
            urlopen=fake_urlopen,
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

    def test_ensure_ready_rejects_port_above_tcp_range(self) -> None:
        urlopen = MagicMock(return_value=_FakeResponse())
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


if __name__ == "__main__":
    unittest.main()
