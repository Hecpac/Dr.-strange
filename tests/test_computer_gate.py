import unittest
import tempfile
from pathlib import Path

from claw_v2.approval import ApprovalManager


class ApprovalRejectTests(unittest.TestCase):
    def test_reject_sets_status_to_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "test-secret")
            pending = manager.create(action="click", summary="Click buy button")
            manager.reject(pending.approval_id)
            self.assertEqual(manager.status(pending.approval_id), "rejected")

    def test_reject_does_not_require_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "test-secret")
            pending = manager.create(action="click", summary="Click buy button")
            manager.reject(pending.approval_id)
            payload = manager.read(pending.approval_id)
            self.assertEqual(payload["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
