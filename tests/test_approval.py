from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from claw_v2.approval import APPROVAL_TTL_SECONDS, ApprovalManager
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

    def test_archive_removes_approval_from_pending_without_deleting_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")
            pending = manager.create("deploy", "Deploy to production")

            archived = manager.archive(pending.approval_id, reason="duplicate")

            self.assertTrue(archived)
            self.assertEqual(manager.list_pending(), [])
            payload = manager.read(pending.approval_id)
            self.assertEqual(payload["status"], "archived")
            self.assertEqual(payload["archive_reason"], "duplicate")
            self.assertIn("archived_at", payload)

    # MED-2: approval tokens are single-use; a resolved record is immutable.
    def test_valid_token_approves_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("social_publish:acme", "post")
            self.assertTrue(m.approve(p.approval_id, p.token))
            self.assertEqual(m.status(p.approval_id), "approved")

    def test_replay_with_valid_token_does_not_reapprove(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("social_publish:acme", "post")
            self.assertTrue(m.approve(p.approval_id, p.token))
            # Single-use: replaying the same token must NOT re-approve.
            self.assertFalse(m.approve(p.approval_id, p.token))
            self.assertEqual(m.status(p.approval_id), "approved")

    def test_wrong_token_after_approval_does_not_corrupt_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("deploy", "x")
            self.assertTrue(m.approve(p.approval_id, p.token))
            # A wrong token after approval must NOT flip approved -> rejected.
            self.assertFalse(m.approve(p.approval_id, "wrong-token"))
            self.assertEqual(m.status(p.approval_id), "approved")

    def test_wrong_token_while_pending_rejects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("deploy", "x")
            self.assertFalse(m.approve(p.approval_id, "wrong-token"))
            self.assertEqual(m.status(p.approval_id), "rejected")

    def test_expired_pending_returns_false_and_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("deploy", "x")
            path = m._path_for(p.approval_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created_at"] = time.time() - (APPROVAL_TTL_SECONDS + 60)
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(m.approve(p.approval_id, p.token))
            self.assertEqual(m.status(p.approval_id), "expired")

    def test_approve_does_not_persist_result_side_channel(self) -> None:
        # The _result return side-channel must never be written to the record.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("deploy", "x")
            m.approve(p.approval_id, p.token)
            raw = json.loads(m._path_for(p.approval_id).read_text(encoding="utf-8"))
            self.assertNotIn("_result", raw)
            self.assertEqual(raw["status"], "approved")

    def test_resolved_record_is_content_immutable_on_replay(self) -> None:
        # A resolved record's persisted bytes must not change on replay /
        # wrong-token-after-approval.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("social_publish:acme", "post")
            m.approve(p.approval_id, p.token)
            path = m._path_for(p.approval_id)
            before = path.read_text(encoding="utf-8")
            m.approve(p.approval_id, p.token)        # replay valid token
            m.approve(p.approval_id, "wrong-token")  # wrong token after approval
            after = path.read_text(encoding="utf-8")
            self.assertEqual(before, after)

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

    def test_telegram_gate_logs_notifier_exception(self) -> None:
        """C4: notifier failure must be visible (logger.exception)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")

            def raising_notifier(pending):
                raise RuntimeError("notifier boom")

            gate = build_telegram_approval_gate(manager, notifier=raising_notifier)
            definition = ToolDefinition(
                name="GPTImage",
                description="Generate an image",
                allowed_agent_classes=("operator",),
                handler=lambda args: {"ok": True},
                tier=TIER_REQUIRES_APPROVAL,
            )
            with self.assertLogs("claw_v2.approval_gate", level="ERROR") as captured:
                with self.assertRaises(ApprovalPending) as ctx:
                    gate(definition, {"prompt": "x"})
            self.assertEqual(ctx.exception.tool, "GPTImage")
            joined = "\n".join(captured.output)
            self.assertIn("RuntimeError", joined)
            self.assertIn("notifier boom", joined)
            self.assertIn(ctx.exception.approval_id, joined)
            self.assertEqual(len(manager.list_pending()), 1)


if __name__ == "__main__":
    unittest.main()
