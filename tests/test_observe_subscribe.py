from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream


class ObserveSubscribeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stream = ObserveStream(Path(self._tmp.name) / "observe.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_subscribe_callback_invoked_on_emit(self) -> None:
        received: list[dict] = []
        self.stream.subscribe("daemon_health_check_notification", received.append)
        self.stream.emit(
            "daemon_health_check_notification", payload={"status": "ok"}
        )
        self.assertEqual(received, [{"status": "ok"}])

    def test_subscribe_callback_not_invoked_for_other_events(self) -> None:
        received: list[dict] = []
        self.stream.subscribe("daemon_health_check_notification", received.append)
        self.stream.emit("unrelated_event", payload={"x": 1})
        self.assertEqual(received, [])

    def test_subscribe_callback_exception_does_not_break_emit(self) -> None:
        def boom(_payload: dict) -> None:
            raise RuntimeError("subscriber failed")

        good_received: list[dict] = []
        self.stream.subscribe("evt", boom)
        self.stream.subscribe("evt", good_received.append)
        self.stream.emit("evt", payload={"n": 1})
        self.assertEqual(good_received, [{"n": 1}])

    def test_emit_persists_even_when_no_subscribers(self) -> None:
        self.stream.emit("lonely_event", payload={"a": 1})
        events = self.stream.recent_events(limit=1)
        self.assertEqual(events[0]["event_type"], "lonely_event")
        self.assertEqual(events[0]["payload"], {"a": 1})

    def test_multiple_subscribers_all_invoked(self) -> None:
        a: list[dict] = []
        b: list[dict] = []
        self.stream.subscribe("evt", a.append)
        self.stream.subscribe("evt", b.append)
        self.stream.emit("evt", payload={"k": "v"})
        self.assertEqual(a, [{"k": "v"}])
        self.assertEqual(b, [{"k": "v"}])


if __name__ == "__main__":
    unittest.main()
