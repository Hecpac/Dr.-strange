"""P0-E: task_outcomes.outcome must reflect agent_tasks verification status.

Behavioral audit found 144 task_outcomes rows with `outcome='success'`
while 91 agent_tasks for the same window had `status='completed_unverified'`.
That divergence happens because `_record_learning_outcome` is called with
a hardcoded `outcome="success"` for every non-empty brain reply, even
when the brain tool-use ledger marks the task `completed_unverified` or
`failed`.

Fix contract:
  - When the brain reply produced tools but the ledger row ended in
    `completed_unverified` (or `needs_verification`), the recorded
    learning outcome must be `usable_reply_unverified`, not `success`.
  - When the ledger row ended in `failed`, learning outcome must be
    `failure`.
  - When no tool calls happened (pure chat), `success` is still correct.
  - The schema must allow `usable_reply_unverified` so the row can be
    persisted at all.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.bot import BotService
from claw_v2.memory import MemoryStore


class _StubRecord:
    def __init__(self, status: str, verification_status: str = "needs_verification") -> None:
        self.status = status
        self.verification_status = verification_status


class TaskOutcomeAlignmentTests(unittest.TestCase):
    def test_schema_allows_usable_reply_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            memory = MemoryStore(Path(tmp) / "m.db")
            try:
                memory.store_task_outcome(
                    task_type="telegram_message",
                    task_id="t1",
                    description="brain produced reply with unverified tools",
                    approach="brain.handle_message",
                    outcome="usable_reply_unverified",
                    lesson="Reply was usable but verifier did not pass.",
                    error_snippet=None,
                    retries=0,
                )
            except sqlite3.IntegrityError as exc:  # pragma: no cover - failure marker
                self.fail(f"schema rejects usable_reply_unverified: {exc!r}")
            rows = memory._conn.execute(  # type: ignore[attr-defined]
                "SELECT outcome FROM task_outcomes WHERE task_id='t1'"
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["usable_reply_unverified"])

    def test_classify_brain_outcome_no_task_record_is_success(self) -> None:
        outcome = BotService._classify_brain_outcome_value(None, fallback="success")
        self.assertEqual(outcome, "success")

    def test_classify_brain_outcome_completed_unverified_is_usable_reply_unverified(self) -> None:
        record = _StubRecord(status="completed_unverified")
        outcome = BotService._classify_brain_outcome_value(record, fallback="success")
        self.assertEqual(outcome, "usable_reply_unverified")

    def test_classify_brain_outcome_needs_verification_is_usable_reply_unverified(self) -> None:
        record = _StubRecord(status="running", verification_status="needs_verification")
        outcome = BotService._classify_brain_outcome_value(record, fallback="success")
        self.assertEqual(outcome, "usable_reply_unverified")

    def test_classify_brain_outcome_failed_is_failure(self) -> None:
        record = _StubRecord(status="failed", verification_status="failed")
        outcome = BotService._classify_brain_outcome_value(record, fallback="success")
        self.assertEqual(outcome, "failure")

    def test_classify_brain_outcome_succeeded_passed_is_success(self) -> None:
        record = _StubRecord(status="succeeded", verification_status="passed")
        outcome = BotService._classify_brain_outcome_value(record, fallback="success")
        self.assertEqual(outcome, "success")

    def test_classify_brain_outcome_failure_fallback_is_failure(self) -> None:
        outcome = BotService._classify_brain_outcome_value(None, fallback="failure")
        self.assertEqual(outcome, "failure")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
