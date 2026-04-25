from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

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
