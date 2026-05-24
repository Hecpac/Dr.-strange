"""Semantic approval fields: risk_basis, requested_by, visible_to_user, resolved_by.

The 2026-05-23 behavioral audit (recommendation R3, also called out in
the canonical report §5) found that approvals today blur three signals:
- *why* the gate fired (policy vs verifier vs kairos vs user-visible)
- *who* requested it
- *whether* the user can see it (vs purely internal kairos approvals)
- *how* it was resolved (human vs system-auto vs expired)

This PR makes those four dimensions first-class on every
``ApprovalManager.create`` and preserved on disk, while keeping
backwards compatibility with approvals already on disk that lack the
new fields.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.approval import (
    ApprovalManager,
    APPROVAL_TTL_SECONDS,
    DEFAULT_REQUESTED_BY,
    RESOLVED_BY_EXPIRED,
    RESOLVED_BY_HUMAN,
    RESOLVED_BY_SYSTEM_AUTO,
)


class SemanticApprovalsCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.am = ApprovalManager(Path(self._tmp.name), secret="s")

    def test_create_persists_all_semantic_fields(self) -> None:
        rec = self.am.create(
            "promote_perf-optimizer",
            "promote agent if metric improved",
            metadata={"agent": "perf-optimizer"},
            risk_basis="verifier:disagreement",
            requested_by="verifier",
            visible_to_user=True,
        )
        payload = self.am.read(rec.approval_id)
        self.assertEqual(payload["risk_basis"], "verifier:disagreement")
        self.assertEqual(payload["requested_by"], "verifier")
        self.assertTrue(payload["visible_to_user"])
        self.assertIsNone(payload["resolved_by"])
        self.assertEqual(payload["status"], "pending")

    def test_create_defaults_visible_to_user_true_and_requested_by_unknown(self) -> None:
        rec = self.am.create("a", "b")
        payload = self.am.read(rec.approval_id)
        self.assertTrue(payload["visible_to_user"])
        self.assertEqual(payload["requested_by"], DEFAULT_REQUESTED_BY)
        self.assertIsNone(payload["risk_basis"])
        self.assertIsNone(payload["resolved_by"])

    def test_kairos_internal_approval_can_hide_from_user(self) -> None:
        rec = self.am.create(
            "auto_publish_social",
            "kairos draft posted with autonomy",
            requested_by="kairos",
            visible_to_user=False,
            risk_basis="kairos:auto_publish",
        )
        payload = self.am.read(rec.approval_id)
        self.assertFalse(payload["visible_to_user"])
        self.assertEqual(payload["requested_by"], "kairos")
        self.assertEqual(payload["risk_basis"], "kairos:auto_publish")


class ResolvedByTrackingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.am = ApprovalManager(Path(self._tmp.name), secret="s")

    def test_approve_sets_resolved_by_human(self) -> None:
        rec = self.am.create("a", "b")
        ok = self.am.approve(rec.approval_id, rec.token)
        self.assertTrue(ok)
        payload = self.am.read(rec.approval_id)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["resolved_by"], RESOLVED_BY_HUMAN)
        self.assertIn("resolved_at", payload)

    def test_approve_internal_sets_resolved_by_system_auto(self) -> None:
        rec = self.am.create("a", "b")
        ok = self.am.approve_internal(rec.approval_id)
        self.assertTrue(ok)
        payload = self.am.read(rec.approval_id)
        self.assertEqual(payload["status"], "approved")
        self.assertEqual(payload["resolved_by"], RESOLVED_BY_SYSTEM_AUTO)

    def test_reject_sets_resolved_by_human(self) -> None:
        rec = self.am.create("a", "b")
        self.am.reject(rec.approval_id)
        payload = self.am.read(rec.approval_id)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["resolved_by"], RESOLVED_BY_HUMAN)

    def test_expired_approval_sets_resolved_by_expired(self) -> None:
        rec = self.am.create("a", "b")
        # Backdate created_at past the TTL window.
        path = Path(self._tmp.name) / f"{rec.approval_id}.json"
        data = json.loads(path.read_text())
        data["created_at"] = time.time() - (APPROVAL_TTL_SECONDS + 60)
        path.write_text(json.dumps(data))
        ok = self.am.approve(rec.approval_id, rec.token)
        self.assertFalse(ok)
        payload = self.am.read(rec.approval_id)
        self.assertEqual(payload["status"], "expired")
        self.assertEqual(payload["resolved_by"], RESOLVED_BY_EXPIRED)


class BackwardsCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.am = ApprovalManager(Path(self._tmp.name), secret="s")

    def test_legacy_approval_json_without_semantic_fields_reads_with_defaults(self) -> None:
        """Approvals already on disk from the wave-1 release lack the new
        fields. ``read()`` must inject defaults so downstream code (telegram
        notifier, backpressure counter) keeps working unchanged."""
        legacy = {
            "approval_id": "legacy-1",
            "action": "tool:Bash",
            "summary": "legacy summary",
            "metadata": {},
            "token_hash": "x" * 64,
            "status": "pending",
            "created_at": time.time(),
        }
        (Path(self._tmp.name) / "legacy-1.json").write_text(json.dumps(legacy))
        payload = self.am.read("legacy-1")
        self.assertTrue(payload["visible_to_user"])
        self.assertEqual(payload["requested_by"], DEFAULT_REQUESTED_BY)
        self.assertIsNone(payload["risk_basis"])
        self.assertIsNone(payload["resolved_by"])

    def test_list_pending_returns_legacy_with_semantic_defaults(self) -> None:
        legacy = {
            "approval_id": "legacy-2",
            "action": "promote_perf-optimizer",
            "summary": "wave-1 pending",
            "metadata": {},
            "token_hash": "x" * 64,
            "status": "pending",
            "created_at": time.time(),
        }
        (Path(self._tmp.name) / "legacy-2.json").write_text(json.dumps(legacy))
        items = self.am.list_pending()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["requested_by"], DEFAULT_REQUESTED_BY)
        self.assertTrue(items[0]["visible_to_user"])


class VisibleToUserFilteringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.am = ApprovalManager(Path(self._tmp.name), secret="s")

    def test_list_pending_visible_filters_to_user_facing_only(self) -> None:
        self.am.create("a", "s", visible_to_user=True, requested_by="policy")
        self.am.create("b", "s", visible_to_user=False, requested_by="kairos")
        self.am.create("c", "s", visible_to_user=True, requested_by="verifier")
        visible = self.am.list_pending_visible_to_user()
        actions = sorted(p["action"] for p in visible)
        self.assertEqual(actions, ["a", "c"])
        # The underlying total stays the same.
        self.assertEqual(len(self.am.list_pending()), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
