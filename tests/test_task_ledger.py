from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger


class TaskLedgerTests(unittest.TestCase):
    def test_create_list_and_mark_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")

            record = ledger.create(
                task_id="task-1",
                session_id="tg-123",
                objective="fix login",
                mode="coding",
                runtime="coordinator",
                provider="codex",
                model="gpt-5.3-codex",
                status="running",
                route={
                    "channel": "telegram",
                    "external_session_id": "123",
                    "external_user_id": "u1",
                },
            )

            self.assertEqual(record.status, "running")
            self.assertEqual(record.channel, "telegram")
            self.assertIsNotNone(record.started_at)

            terminal = ledger.mark_terminal(
                "task-1",
                status="succeeded",
                summary="fixed login",
                verification_status="passed",
                artifacts={"commit": "abc123"},
            )

            self.assertIsNotNone(terminal)
            self.assertEqual(terminal.status, "succeeded")
            self.assertEqual(terminal.verification_status, "passed")
            self.assertEqual(terminal.artifacts["commit"], "abc123")
            self.assertEqual(ledger.summary(session_id="tg-123"), {"succeeded": 1})
            self.assertEqual(ledger.list(session_id="tg-123")[0].task_id, "task-1")

    def test_emits_task_events_with_job_and_artifact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)

            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="fix login",
                runtime="coordinator",
                status="running",
                artifacts={
                    "lifecycle": {
                        "job": {
                            "kind": "job",
                            "artifact_id": "job:abc",
                            "task_id": "task-1",
                            "session_id": "s1",
                            "lifecycle_status": "running",
                            "artifact_ids": ["plan:abc"],
                        }
                    }
                },
            )

            events = observe.job_events("task-1")

            self.assertEqual(events[0]["event_type"], "task_ledger_created")
            self.assertEqual(events[0]["job_id"], "task-1")
            self.assertEqual(events[0]["artifact_id"], "job:abc")

    def test_succeeded_without_evidence_redirects_to_running_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)

            ledger.create(
                task_id="task-fs",
                session_id="s1",
                objective="ship feature",
                runtime="coordinator",
                status="running",
            )

            result = ledger.mark_terminal(
                "task-fs",
                status="succeeded",
                summary="Step 1: plan...\nStep 2: execute...",
                verification_status="pending",
            )

            self.assertEqual(result.status, "running")
            self.assertNotEqual(result.verification_status, "passed")

            events = [e for e in observe.recent_events(limit=20) if e["event_type"] == "task_false_success_prevented"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["task_id"], "task-fs")
            self.assertEqual(events[0]["payload"]["requested_status"], "succeeded")

    def test_succeeded_with_evidence_and_passed_verification_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            ledger.create(
                task_id="task-ok",
                session_id="s1",
                objective="ship feature",
                runtime="coordinator",
                status="running",
            )
            terminal = ledger.mark_terminal(
                "task-ok",
                status="succeeded",
                summary="Done.",
                verification_status="passed",
                artifacts={"pr_url": "https://example.com/pr/1"},
            )
            self.assertEqual(terminal.status, "succeeded")
            self.assertEqual(terminal.verification_status, "passed")

    def test_marks_stale_running_tasks_lost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="long task",
                runtime="coordinator",
                status="running",
            )
            with ledger._lock:
                ledger._conn.execute(
                    "UPDATE agent_tasks SET updated_at = ? WHERE task_id = ?",
                    (time.time() - 600, "task-1"),
                )
                ledger._conn.commit()

            changed = ledger.mark_stale_running_lost(older_than_seconds=300)

            self.assertEqual(changed, 1)
            record = ledger.get("task-1")
            self.assertEqual(record.status, "lost")
            self.assertEqual(record.verification_status, "failed")

    def test_reconciles_historical_succeeded_pending_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)
            ledger.create(
                task_id="task-false-success",
                session_id="s1",
                objective="ship feature",
                runtime="coordinator",
                status="running",
                metadata={"autonomous": True},
                artifacts={"pr_url": "https://example.com/pr/1"},
            )
            with ledger._lock:
                ledger._conn.execute(
                    """
                    UPDATE agent_tasks
                    SET status = 'succeeded',
                        verification_status = 'pending',
                        completed_at = ?
                    WHERE task_id = ?
                    """,
                    (time.time(), "task-false-success"),
                )
                ledger._conn.commit()

            changed = ledger.reconcile_false_successes()

            self.assertEqual(changed, 1)
            record = ledger.get("task-false-success")
            self.assertEqual(record.status, "failed")
            self.assertEqual(record.verification_status, "pending")
            self.assertTrue(record.metadata["reconciled_false_success"])
            self.assertEqual(record.metadata["reconciled_from_status"], "succeeded")
            events = [e for e in observe.recent_events(limit=20) if e["event_type"] == "task_false_success_reconciled"]
            self.assertEqual(events[0]["payload"]["count"], 1)

    def test_reconcile_keeps_verified_succeeded_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            ledger.create(
                task_id="task-good",
                session_id="s1",
                objective="ship feature",
                runtime="coordinator",
                status="running",
                artifacts={"pr_url": "https://example.com/pr/1"},
            )
            terminal = ledger.mark_terminal(
                "task-good",
                status="succeeded",
                summary="Done.",
                verification_status="passed",
                artifacts={"pr_url": "https://example.com/pr/1"},
            )

            changed = ledger.reconcile_false_successes()

            self.assertEqual(changed, 0)
            self.assertEqual(ledger.get("task-good").status, terminal.status)

    def test_rejects_invalid_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")

            with self.assertRaises(ValueError):
                ledger.create(
                    task_id="task-1",
                    session_id="s1",
                    objective="bad",
                    runtime="coordinator",
                    status="done",
                )


if __name__ == "__main__":
    unittest.main()
