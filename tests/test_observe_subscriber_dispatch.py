from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.observe import ObserveStream


class ObserveSubscriberDispatchTests(unittest.TestCase):
    def test_subscribers_run_even_when_persistence_dropped(self) -> None:
        # A transient SQLite lock makes _persist_event return False. Subscribers
        # (e.g. autonomous_task_completed -> Telegram notification) MUST still
        # fire — a dropped diagnostic write must not silently swallow a
        # task-completion notification to the user.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            received: list[dict] = []
            observe.subscribe("autonomous_task_completed", lambda p: received.append(p))
            with patch.object(observe, "_persist_event", return_value=False):
                observe.emit("autonomous_task_completed", payload={"task_id": "t1"})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["task_id"], "t1")

    def test_subscribers_still_run_on_successful_persistence(self) -> None:
        # Regression guard: the normal (persisted) path keeps dispatching.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "test.db")
            received: list[dict] = []
            observe.subscribe("autonomous_task_completed", lambda p: received.append(p))
            observe.emit("autonomous_task_completed", payload={"task_id": "t2"})
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["task_id"], "t2")


if __name__ == "__main__":
    unittest.main()
