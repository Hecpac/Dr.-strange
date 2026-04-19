from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claw_v2.approval import ApprovalManager


class ApprovalManagerTests(unittest.TestCase):
    def test_internal_approval_bypass_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")
            pending = manager.create("deploy", "Deploy to production")

            with self.assertRaises(PermissionError):
                manager.approve_internal(pending.approval_id)

            self.assertEqual(manager.status(pending.approval_id), "pending")

    def test_read_waits_for_writer_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")
            pending = manager.create("deploy", "Deploy to production")
            path = Path(tmpdir) / f"{pending.approval_id}.json"
            fd = os.open(str(path), os.O_RDWR)
            started = threading.Event()
            finished = threading.Event()
            result: dict[str, object] = {}

            def reader() -> None:
                started.set()
                result["payload"] = manager.read(pending.approval_id)
                finished.set()

            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                thread = threading.Thread(target=reader)
                thread.start()
                started.wait(timeout=1.0)
                time.sleep(0.05)
                self.assertFalse(finished.is_set())
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

            thread.join(timeout=1.0)
            self.assertTrue(finished.is_set())
            self.assertEqual(result["payload"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
