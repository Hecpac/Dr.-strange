from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claw_v2.approval import ApprovalManager
from claw_v2.approval_gate import ApprovalPending, approved_tool_invocation, build_telegram_approval_gate
from claw_v2.tools import TIER_REQUIRES_APPROVAL, ToolDefinition


class ApprovalManagerTests(unittest.TestCase):
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

    def test_approved_tool_invocation_allows_one_matching_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")
            gate = build_telegram_approval_gate(manager)
            definition = ToolDefinition(
                name="GPTImage",
                description="Generate an image",
                allowed_agent_classes=("operator",),
                handler=lambda args: {"ok": True},
                tier=TIER_REQUIRES_APPROVAL,
            )

            with approved_tool_invocation(
                tool="GPTImage",
                approval_id="approval-1",
                reason="test",
            ):
                gate(definition, {"prompt": "ok"})
                with self.assertRaises(ApprovalPending):
                    gate(definition, {"prompt": "second"})


if __name__ == "__main__":
    unittest.main()
