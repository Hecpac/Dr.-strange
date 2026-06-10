"""Tests for the 2026-06-10 audit, group 6 (turn/data reliability).

1. M2 — task ledger / job queue rebuild migrations survive a crash that
   left the orphan ``*_old`` / ``*_legacy_*`` table: rows are drained, not
   silently lost.
2. A6 — reply_context expires after its TTL and is consumed single-use.
3. B10 — the provider circuit breaker decays failures outside the rolling
   window and ignores user-content/budget errors.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.adapters.base import AdapterError, record_tools_executed
from claw_v2.jobs import JobService
from claw_v2.llm import _non_provider_fault_reason
from claw_v2.retry_policy import ProviderCircuitBreaker
from claw_v2.state_handler import REPLY_CONTEXT_TTL_SECONDS, reply_context_fresh
from claw_v2.task_ledger import TaskLedger


class OrphanMigrationRecoveryTests(unittest.TestCase):
    def test_task_ledger_orphan_old_table_is_drained_not_lost(self) -> None:
        db_path = Path(tempfile.mkdtemp()) / "ledger.db"
        ledger = TaskLedger(db_path)
        ledger.create(task_id="t1", session_id="s1", objective="obj", runtime="brain")
        # Simulate the crash state the old migration left behind: the live
        # table was renamed to agent_tasks_old and the process died before
        # copying. On the next boot the schema creates an empty agent_tasks.
        with ledger._lock:
            ledger._conn.execute("ALTER TABLE agent_tasks RENAME TO agent_tasks_old")
            ledger._conn.commit()
            ledger._conn.close()

        recovered = TaskLedger(db_path)
        records = recovered.list(limit=10)
        self.assertEqual([r.task_id for r in records], ["t1"])
        orphan = recovered._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_tasks_old'"
        ).fetchone()
        self.assertIsNone(orphan)

    def test_job_queue_orphan_legacy_table_is_drained_not_lost(self) -> None:
        db_path = Path(tempfile.mkdtemp()) / "jobs.db"
        service = JobService(db_path)
        record = service.enqueue(kind="wiki_research", payload={"q": "x"})
        with service._lock:
            service._conn.execute("ALTER TABLE agent_jobs RENAME TO agent_jobs_legacy_deadbeef")
            service._conn.commit()
            service._conn.close()

        recovered = JobService(db_path)
        restored = recovered.get(record.job_id)
        self.assertIsNotNone(restored)
        orphan = recovered._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'agent_jobs_legacy_%'"
        ).fetchone()
        self.assertIsNone(orphan)


class ReplyContextTtlTests(unittest.TestCase):
    def test_fresh_reply_context_is_accepted(self) -> None:
        self.assertTrue(reply_context_fresh({"text": "x", "created_at": time.time()}))

    def test_expired_and_legacy_reply_context_are_stale(self) -> None:
        expired = {"text": "x", "created_at": time.time() - REPLY_CONTEXT_TTL_SECONDS - 1}
        self.assertFalse(reply_context_fresh(expired))
        self.assertFalse(reply_context_fresh({"text": "x"}))


class CircuitBreakerHardeningTests(unittest.TestCase):
    def test_failures_decay_outside_rolling_window(self) -> None:
        now = [1_000.0]
        breaker = ProviderCircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=120.0,
            failure_window_seconds=600.0,
            clock=lambda: now[0],
        )
        breaker.record_failure("anthropic", "boom-1")
        breaker.record_failure("anthropic", "boom-2")
        # A week later, one more failure must NOT open the circuit.
        now[0] += 7 * 24 * 3600.0
        transition = breaker.record_failure("anthropic", "boom-3")
        self.assertEqual(transition.status, "closed")
        self.assertEqual(transition.failures, 1)
        self.assertTrue(breaker.check("anthropic").allowed)

    def test_three_failures_inside_window_still_open_the_circuit(self) -> None:
        now = [1_000.0]
        breaker = ProviderCircuitBreaker(failure_threshold=3, clock=lambda: now[0])
        breaker.record_failure("anthropic", "boom-1")
        breaker.record_failure("anthropic", "boom-2")
        transition = breaker.record_failure("anthropic", "boom-3")
        self.assertEqual(transition.status, "open")
        self.assertFalse(breaker.check("anthropic").allowed)

    def test_non_provider_faults_are_classified(self) -> None:
        budget = AdapterError("aborted", metadata={"reason": "budget_exceeded"})
        self.assertEqual(_non_provider_fault_reason(budget), "budget_exceeded")
        image = AdapterError(
            "API Error: an image in the conversation could not be processed"
        )
        self.assertEqual(_non_provider_fault_reason(image), "user_content_image")
        provider = AdapterError("rate limit exceeded")
        record_tools_executed(provider, ["Bash"])
        self.assertIsNone(_non_provider_fault_reason(provider))


if __name__ == "__main__":
    unittest.main()
