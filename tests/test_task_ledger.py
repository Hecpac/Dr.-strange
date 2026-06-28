from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger


class TaskLedgerTests(unittest.TestCase):
    def test_prune_terminal_deletes_only_old_terminal_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)
            now = 1_000_000.0
            old = now - (31 * 86400)
            recent = now - (10 * 86400)

            for task_id, status in (
                ("task-succeeded-old", "succeeded"),
                ("task-failed-old", "failed"),
                ("task-cancelled-old", "cancelled"),
                ("task-recent", "succeeded"),
                ("task-queued-old", "queued"),
                ("task-running-old", "running"),
            ):
                ledger.create(
                    task_id=task_id,
                    session_id="s1",
                    objective=f"retention {task_id}",
                    runtime="coordinator",
                    status=status,
                )

            old_terminal_ids = (
                "task-succeeded-old",
                "task-failed-old",
                "task-cancelled-old",
            )
            active_ids = ("task-queued-old", "task-running-old")
            with ledger._lock:
                ledger._conn.execute(
                    "UPDATE agent_tasks SET completed_at = ?, updated_at = ? "
                    f"WHERE task_id IN ({', '.join('?' for _ in old_terminal_ids)})",
                    (old, old, *old_terminal_ids),
                )
                ledger._conn.execute(
                    "UPDATE agent_tasks SET updated_at = ? "
                    f"WHERE task_id IN ({', '.join('?' for _ in active_ids)})",
                    (old, *active_ids),
                )
                ledger._conn.execute(
                    "UPDATE agent_tasks SET completed_at = ?, updated_at = ? WHERE task_id = ?",
                    (recent, recent, "task-recent"),
                )
                ledger._conn.commit()

            deleted = ledger.prune_terminal(retention_days=30, max_rows=10, now=now)

            self.assertEqual(deleted, 3)
            for task_id in old_terminal_ids:
                self.assertIsNone(ledger.get(task_id))
            for task_id in (*active_ids, "task-recent"):
                self.assertIsNotNone(ledger.get(task_id))
            events = [event["event_type"] for event in observe.recent_events(limit=10)]
            self.assertIn("agent_tasks_pruned", events)

    def test_prune_terminal_is_bounded_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            now = 1_000_000.0
            old = now - (31 * 86400)
            task_ids = [f"task-old-{index}" for index in range(3)]
            for task_id in task_ids:
                ledger.create(
                    task_id=task_id,
                    session_id="s1",
                    objective="retention",
                    runtime="coordinator",
                    status="failed",
                )
            with ledger._lock:
                ledger._conn.execute(
                    "UPDATE agent_tasks SET completed_at = ?, updated_at = ? "
                    f"WHERE task_id IN ({', '.join('?' for _ in task_ids)})",
                    (old, old, *task_ids),
                )
                ledger._conn.commit()

            self.assertEqual(ledger.prune_terminal(retention_days=30, max_rows=2, now=now), 2)
            self.assertEqual(ledger.summary(), {"failed": 1})
            self.assertEqual(ledger.prune_terminal(retention_days=30, max_rows=2, now=now), 1)
            self.assertEqual(ledger.summary(), {})

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

    def test_terminal_status_is_immutable_for_late_writers(self) -> None:
        # LOW (2026-06-12, hardened per PR #95 review): a late writer
        # (reconciliation marking lost, duplicated completion path) must not
        # flip an already-terminal row; check+write happen under one lock.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)
            ledger.create(
                task_id="task-1",
                session_id="tg-123",
                objective="fix login",
                mode="coding",
                runtime="coordinator",
                provider="codex",
                model="gpt-5.3-codex",
                status="running",
                route={"channel": "telegram"},
            )
            first = ledger.mark_terminal(
                "task-1",
                status="failed",
                summary="broke",
                error="boom",
                verification_status="failed",
            )
            self.assertEqual(first.status, "failed")

            # Late writer with full evidence still cannot resurrect the row.
            second = ledger.mark_terminal(
                "task-1",
                status="succeeded",
                summary="late success",
                verification_status="passed",
                artifacts={"commit": "abc123"},
            )

            self.assertIsNotNone(second)
            self.assertEqual(second.status, "failed")
            self.assertEqual(second.summary, "broke")
            events = [e["event_type"] for e in observe.recent_events(limit=20)]
            self.assertIn("task_ledger_terminal_transition_blocked", events)

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

            events = [
                e
                for e in observe.recent_events(limit=20)
                if e["event_type"] == "task_false_success_prevented"
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["task_id"], "task-fs")
            self.assertEqual(events[0]["payload"]["requested_status"], "succeeded")

    def test_succeeded_with_pending_outcome_manifest_redirects_to_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)
            ledger.create(
                task_id="task-pending-outcome",
                session_id="s1",
                objective="publish async artifact",
                runtime="coordinator",
                status="running",
            )

            result = ledger.mark_terminal(
                "task-pending-outcome",
                status="succeeded",
                summary="Publish job created.",
                verification_status="passed",
                artifacts={
                    "publish_job_id": "job-1",
                    "outcome_manifest": {
                        "version": 1,
                        "final_outcome": "pending",
                        "pending_async_jobs": [
                            {"job_id": "job-1", "status": "running"},
                        ],
                        "blockers": [],
                        "verifications": [{"kind": "handler", "result": "passed"}],
                    },
                },
            )

            self.assertEqual(result.status, "running")
            self.assertEqual(result.verification_status, "needs_verification")
            self.assertIn("outcome_manifest_has_pending_async_jobs", result.error)
            events = [
                e
                for e in observe.recent_events(limit=20)
                if e["event_type"] == "task_false_success_prevented"
            ]
            self.assertEqual(len(events), 1)
            self.assertEqual(
                events[0]["payload"]["reason"], "outcome_manifest_has_pending_async_jobs"
            )

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

    def test_completed_unverified_is_terminal_and_not_reaped_lost(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            ledger.create(
                task_id="task-1",
                session_id="s1",
                objective="brain tool use",
                runtime="brain_fallback",
                status="running",
            )
            terminal = ledger.mark_terminal(
                "task-1",
                status="completed_unverified",
                summary="tool calls finished without verifier pass",
                verification_status="needs_verification",
                artifacts={
                    "evidence_manifest": {
                        "origin": "brain_fallback",
                        "tools_run": ["Read"],
                        "trace_id": "t",
                    }
                },
            )
            self.assertEqual(terminal.status, "completed_unverified")
            with ledger._lock:
                ledger._conn.execute(
                    "UPDATE agent_tasks SET updated_at = ? WHERE task_id = ?",
                    (time.time() - 600, "task-1"),
                )
                ledger._conn.commit()

            changed = ledger.mark_stale_running_lost(older_than_seconds=300)

            self.assertEqual(changed, 0)
            record = ledger.get("task-1")
            self.assertEqual(record.status, "completed_unverified")
            self.assertEqual(record.verification_status, "needs_verification")

    def test_stale_running_reconciliation_emits_terminal_event_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)
            ledger.create(
                task_id="task-1",
                session_id="tg-123",
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
            terminal_events = [
                event
                for event in observe.recent_events(limit=10, event_type="task_ledger_terminal")
                if event["payload"].get("task_id") == "task-1"
            ]
            self.assertEqual(len(terminal_events), 1)
            self.assertEqual(terminal_events[0]["payload"]["status"], "lost")

    def test_mark_stale_running_lost_skips_terminal_for_rescued_rows(self) -> None:
        # Issue #37: a cross-process writer (heartbeat/checkpoint/resume) can
        # advance a row's updated_at past cutoff between the SELECT and the
        # conditional UPDATE (self._lock is thread-local, not a DB lock). The
        # snapshot then includes a row the UPDATE does NOT flip to lost; the fix
        # re-queries only rows now 'lost' so rescued rows never receive a false
        # task_ledger_terminal event.
        with tempfile.TemporaryDirectory() as tmpdir:
            observe = ObserveStream(Path(tmpdir) / "observe.db")
            ledger = TaskLedger(Path(tmpdir) / "claw.db", observe=observe)
            old = time.time() - 1000
            for task_id in ("task-keep-1", "task-rescue", "task-keep-2"):
                ledger.create(
                    task_id=task_id,
                    session_id="s1",
                    objective="long task",
                    runtime="coordinator",
                    status="running",
                )
            with ledger._lock:
                ledger._conn.execute(
                    "UPDATE agent_tasks SET updated_at = ? "
                    "WHERE task_id IN ('task-keep-1','task-rescue','task-keep-2')",
                    (old,),
                )
                ledger._conn.commit()

            # sqlite3.Connection.execute is a read-only C attribute, so wrap
            # the connection to inject the rescue right before the lost-UPDATE.
            class _RacingConnection:
                def __init__(self, real):
                    self._real = real
                    self._rescued = False

                def execute(self, sql, params=()):
                    if (
                        not self._rescued
                        and isinstance(sql, str)
                        and "SET status = 'lost'" in sql
                    ):
                        self._real.execute(
                            "UPDATE agent_tasks SET updated_at = ? WHERE task_id = ?",
                            (time.time(), "task-rescue"),
                        )
                        self._real.commit()
                        self._rescued = True
                    return self._real.execute(sql, params)

                def commit(self):
                    return self._real.commit()

                def rollback(self):
                    return self._real.rollback()

                def __getattr__(self, name):
                    return getattr(self._real, name)

            real_conn = ledger._conn
            ledger._conn = _RacingConnection(real_conn)
            try:
                changed = ledger.mark_stale_running_lost(older_than_seconds=300)
            finally:
                ledger._conn = real_conn

            self.assertEqual(changed, 2)
            self.assertEqual(ledger.get("task-rescue").status, "running")
            self.assertEqual(ledger.get("task-keep-1").status, "lost")
            self.assertEqual(ledger.get("task-keep-2").status, "lost")
            terminal_events = observe.recent_events(
                limit=20, event_type="task_ledger_terminal"
            )
            terminal_ids = {e["payload"].get("task_id") for e in terminal_events}
            self.assertEqual(terminal_ids, {"task-keep-1", "task-keep-2"})
            self.assertNotIn("task-rescue", terminal_ids)

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
            self.assertEqual(record.status, "running")
            self.assertIsNone(record.completed_at)
            self.assertEqual(record.verification_status, "pending")
            self.assertTrue(record.metadata["reconciled_false_success"])
            self.assertEqual(record.metadata["reconciled_from_status"], "succeeded")
            events = [
                e
                for e in observe.recent_events(limit=20)
                if e["event_type"] == "task_false_success_reconciled"
            ]
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

    def test_redacts_sensitive_objective_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = TaskLedger(Path(tmpdir) / "claw.db")
            token = "Aa1234567890Bb1234567890Cc1234567890"

            ledger.create(
                task_id="task-sensitive",
                session_id="s1",
                objective=f"Objective: {token}",
                runtime="coordinator",
                status="running",
                route={"channel": "telegram", "approval_token": token},
                metadata={"source_message": f"Run {token}"},
                artifacts={"output": f"Result {token}"},
            )
            ledger.mark_terminal(
                "task-sensitive",
                status="failed",
                summary=f"Blocked {token}",
                error=f"Error {token}",
                verification_status="blocked",
                artifacts={"log": f"Trace {token}"},
            )

            record = ledger.get("task-sensitive")
            self.assertIsNotNone(record)
            assert record is not None
            payload = json.dumps(record.to_dict(), sort_keys=True)
            self.assertNotIn(token, payload)
            self.assertIn("[REDACTED]", payload)


if __name__ == "__main__":
    unittest.main()
