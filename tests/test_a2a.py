from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from claw_v2.a2a import A2AService


class A2AServiceTests(unittest.TestCase):
    def test_inbox_is_reloaded_from_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = A2AService(root=root)
            result = service.receive_task(
                {
                    "id": "task-1",
                    "from_agent": "peer",
                    "action": "wiki_search",
                    "payload": {"query": "hello"},
                }
            )
            self.assertEqual(result["status"], "accepted")

            reloaded = A2AService(root=root)
            self.assertEqual(reloaded.stats()["inbox_total"], 1)
            self.assertEqual(reloaded._inbox[0].id, "task-1")
            self.assertEqual(reloaded._inbox[0].status, "accepted")

    def test_process_inbox_marks_failures_as_failed_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            router = Mock()
            router.ask.side_effect = RuntimeError("router exploded")
            service = A2AService(root=root, router=router)
            service.receive_task(
                {
                    "id": "task-2",
                    "from_agent": "peer",
                    "action": "wiki_search",
                    "payload": {"query": "hello"},
                }
            )

            result = service.process_inbox()

            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(service._inbox[0].status, "failed")
            self.assertIn("router exploded", service._inbox[0].result["error"])

            reloaded = A2AService(root=root, router=SimpleNamespace(ask=lambda *args, **kwargs: None))
            self.assertEqual(reloaded._inbox[0].status, "failed")


if __name__ == "__main__":
    unittest.main()
