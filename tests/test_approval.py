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
from claw_v2.approval_sensitivity import classify_sensitive_change
from claw_v2.approval_gate import (
    ApprovalPending,
    approval_args_hash,
    approved_tool_invocation,
    build_telegram_approval_gate,
)
from claw_v2.tools import TIER_REQUIRES_APPROVAL, ToolDefinition


class ApprovalManagerTests(unittest.TestCase):
    def test_sensitive_classifier_detects_runtime_policy_and_lockfile_changes(self) -> None:
        diff = """diff --git a/claw_v2/runtime_policy.py b/claw_v2/runtime_policy.py
--- a/claw_v2/runtime_policy.py
+++ b/claw_v2/runtime_policy.py
@@
+TOKEN = "sk-abcdefghijklmnopqrstuvwxyz123456"
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@
+{}
"""
        classification = classify_sensitive_change(diff=diff, action="pipeline:HEC-1")

        self.assertTrue(classification.sensitive)
        self.assertIn("runtime_policy", classification.categories)
        self.assertIn("lockfile", classification.categories)
        self.assertIn("claw_v2/runtime_policy.py", classification.sensitive_paths)
        self.assertIn("[REDACTED]", classification.diff_summary)
        self.assertRegex(classification.required_confirmation or "", r"^CONFIRMO RISK-[0-9A-F]{8}$")

    def test_sensitive_approval_requires_exact_confirmation_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")
            pending = manager.create(
                "pipeline:HEC-1",
                "Runtime policy change",
                diff="diff --git a/claw_v2/runtime_policy.py b/claw_v2/runtime_policy.py\n",
            )
            payload = manager.read(pending.approval_id)
            required = payload["metadata"]["required_confirmation"]

            self.assertTrue(required.startswith("CONFIRMO RISK-"))
            self.assertFalse(manager.approve_confirmation(pending.approval_id, "ok"))
            self.assertEqual(manager.status(pending.approval_id), "pending")
            self.assertFalse(manager.approve(pending.approval_id, pending.token))
            self.assertEqual(manager.status(pending.approval_id), "rejected")

    def test_sensitive_approval_accepts_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ApprovalManager(Path(tmpdir), "secret")
            pending = manager.create(
                "pipeline:HEC-1",
                "Approval gate change",
                diff="diff --git a/claw_v2/approval.py b/claw_v2/approval.py\n",
            )
            required = manager.read(pending.approval_id)["metadata"]["required_confirmation"]

            self.assertTrue(manager.approve(pending.approval_id, required))
            self.assertEqual(manager.status(pending.approval_id), "approved")

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

    # AH1 (2026-06-11): an "approved" status on disk only counts if the
    # manager stamped the resolution — a forged record must not verify.
    def test_verify_resolution_accepts_manager_approved_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            pending = m.create("deploy", "Deploy to production")
            self.assertTrue(m.approve(pending.approval_id, pending.token))
            payload = m.read(pending.approval_id)
            self.assertTrue(m.verify_resolution(payload))

    def test_verify_resolution_rejects_forged_approved_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            pending = m.create("deploy", "Deploy to production")
            path = Path(tmpdir) / f"{pending.approval_id}.json"
            payload = json.loads(path.read_text())
            payload["status"] = "approved"
            payload["resolved_by"] = "human"
            payload["resolved_at"] = time.time()
            path.write_text(json.dumps(payload))

            self.assertFalse(m.verify_resolution(m.read(pending.approval_id)))
            # A no-op manager pass over the forged record (replayed token on a
            # non-pending record) must not stamp a signature retroactively.
            self.assertFalse(m.approve(pending.approval_id, pending.token))
            self.assertFalse(m.verify_resolution(m.read(pending.approval_id)))

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

    def test_expired_pending_uses_configured_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret", ttl_seconds=5)
            p = m.create("deploy", "x")
            path = m._path_for(p.approval_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created_at"] = time.time() - 6
            path.write_text(json.dumps(data), encoding="utf-8")

            self.assertFalse(m.approve(p.approval_id, p.token))
            self.assertEqual(m.status(p.approval_id), "expired")

    def test_reject_does_not_mutate_terminal_states(self) -> None:
        terminal_states = ("approved", "rejected", "expired", "archived")
        for status in terminal_states:
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    m = ApprovalManager(Path(tmpdir), "secret")
                    p = m.create("deploy", "x")
                    path = m._path_for(p.approval_id)
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["status"] = status
                    data["resolved_by"] = "test"
                    data["resolved_at"] = 123.0
                    if status == "archived":
                        data["archived_at"] = 123.0
                    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    before = path.read_text(encoding="utf-8")

                    self.assertFalse(m.reject(p.approval_id))

                    self.assertEqual(m.status(p.approval_id), status)
                    self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_reject_mutates_pending_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("deploy", "x")

            self.assertTrue(m.reject(p.approval_id))

            payload = m.read(p.approval_id)
            self.assertEqual(payload["status"], "rejected")
            self.assertEqual(payload["resolved_by"], "human")

    def test_expire_due_expires_only_pending_due_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret", ttl_seconds=10)
            due = m.create("deploy", "due")
            fresh = m.create("deploy", "fresh")
            approved = m.create("deploy", "approved")
            rejected = m.create("deploy", "rejected")
            expired = m.create("deploy", "expired")
            archived = m.create("deploy", "archived")
            old_created = time.time() - 20
            fresh_created = time.time()
            for approval, status, created in (
                (due, "pending", old_created),
                (fresh, "pending", fresh_created),
                (approved, "approved", old_created),
                (rejected, "rejected", old_created),
                (expired, "expired", old_created),
                (archived, "archived", old_created),
            ):
                path = m._path_for(approval.approval_id)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["status"] = status
                data["created_at"] = created
                if status != "pending":
                    data["resolved_at"] = old_created
                    data["resolved_by"] = "test"
                if status == "archived":
                    data["archived_at"] = old_created
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")

            self.assertEqual(m.expire_due(now=fresh_created), 1)

            self.assertEqual(m.status(due.approval_id), "expired")
            self.assertEqual(m.status(fresh.approval_id), "pending")
            self.assertEqual(m.status(approved.approval_id), "approved")
            self.assertEqual(m.status(rejected.approval_id), "rejected")
            self.assertEqual(m.status(expired.approval_id), "expired")
            self.assertEqual(m.status(archived.approval_id), "archived")
            self.assertEqual(m.read(archived.approval_id)["archived_at"], old_created)

    def test_expire_due_tolerates_corrupt_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret", ttl_seconds=1)
            due = m.create("deploy", "due")
            data = json.loads(m._path_for(due.approval_id).read_text(encoding="utf-8"))
            data["created_at"] = time.time() - 10
            m._path_for(due.approval_id).write_text(json.dumps(data), encoding="utf-8")
            (Path(tmpdir) / "bad.json").write_text("{not-json", encoding="utf-8")

            self.assertEqual(m.expire_due(), 1)
            self.assertEqual(m.status(due.approval_id), "expired")

    def test_swept_expired_record_cannot_be_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret", ttl_seconds=1)
            pending = m.create("deploy", "due")
            path = m._path_for(pending.approval_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created_at"] = time.time() - 10
            path.write_text(json.dumps(data), encoding="utf-8")

            self.assertEqual(m.expire_due(), 1)
            self.assertFalse(m.approve(pending.approval_id, pending.token))
            self.assertEqual(m.status(pending.approval_id), "expired")

    def test_expire_due_emits_flat_events(self) -> None:
        class Observe:
            def __init__(self) -> None:
                self.events: list[str] = []
                self.payloads: list[dict] = []

            def emit(self, event_type: str, *, payload: dict) -> None:
                self.events.append(event_type)
                self.payloads.append(payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            observe = Observe()
            m = ApprovalManager(Path(tmpdir), "secret", ttl_seconds=1, observe=observe)
            p = m.create("deploy", "x")
            path = m._path_for(p.approval_id)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created_at"] = time.time() - 10
            path.write_text(json.dumps(data), encoding="utf-8")

            self.assertEqual(m.expire_due(), 1)

        self.assertIn("approval_expired", observe.events)
        self.assertIn("approval_sweep_completed", observe.events)
        self.assertTrue(all("." not in event for event in observe.events))

    def test_create_redacts_sensitive_record_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            pending = m.create(
                "deploy",
                "deploy with OPENAI_API_KEY=sk-secretsecretsecretsecret",
                metadata={
                    "api_key": "sk-secretsecretsecretsecret",
                    "authorization": "Bearer abcdefghijklmnop",
                    "nested": {"password": "super-secret-password"},
                },
            )

            payload = m.read(pending.approval_id)

        self.assertNotIn("sk-secret", json.dumps(payload))
        self.assertEqual(payload["metadata"]["api_key"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["authorization"], "[REDACTED]")
        self.assertEqual(payload["metadata"]["nested"]["password"], "[REDACTED]")

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

    def test_approve_internal_refuses_non_pending_single_use(self) -> None:
        # #13 / MED-2 parity: approve_internal must respect single-use like
        # approve()/_approve_human_without_token()/archive(). A rejected (or
        # already-resolved) record must not be resurrected by a later internal
        # auto-approve within the TTL window.
        with tempfile.TemporaryDirectory() as tmpdir:
            m = ApprovalManager(Path(tmpdir), "secret")
            p = m.create("deploy", "x")
            m.reject(p.approval_id)
            self.assertFalse(m.approve_internal(p.approval_id))
            self.assertEqual(m.status(p.approval_id), "rejected")

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

    def test_approved_tool_invocation_requires_matching_args_hash_when_present(self) -> None:
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
                args_hash=approval_args_hash({"prompt": "original"}),
            ):
                with self.assertRaises(ApprovalPending):
                    gate(definition, {"prompt": "changed"})

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
