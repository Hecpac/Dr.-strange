"""P0-C: completed_unverified rows must produce a dry-run reconciliation
report with a deadline.

Behavioral audit found 91 ledger rows in ``status='completed_unverified'``
state. The audit recommendation (R2 / R3) is to convert that into a
work-queue with an SLA. This module materialises the queue as a JSON
report — no DB mutation, no automatic closure. The agent emits a
``pending_verification_reconciliation`` event each time the report is
generated so the loop has visibility.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from claw_v2.reconciliation import (
    DEFAULT_RECONCILIATION_DEADLINE_SECONDS,
    build_reconciliation_report,
    recommend_reconciliation_action,
    write_reconciliation_report,
)
from claw_v2.task_ledger import TaskLedger


class _RecordingObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, **kwargs: object) -> None:
        self.events.append((event_type, dict(kwargs)))


class CompletedUnverifiedReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ledger = TaskLedger(Path(self._tmp.name) / "claw.db")
        # Seed two unverified tasks (read-only tools and write tools) plus
        # one normal succeeded row to confirm it is not picked up.
        self.ledger.create(
            task_id="tg-tA",
            session_id="tg-1",
            objective="audita",
            runtime="telegram",
            mode="brain_fallback",
            status="running",
            route={"channel": "telegram", "external_session_id": "tg-1"},
            artifacts={
                "evidence_manifest": {"tools_run": ["Read", "Grep"]},
            },
        )
        self.ledger.mark_terminal(
            "tg-tA",
            status="completed_unverified",
            summary="brain tool-use turn: 2 tool calls (unverified)",
            verification_status="needs_verification",
            artifacts={"evidence_manifest": {"tools_run": ["Read", "Grep"]}},
        )
        self.ledger.create(
            task_id="tg-tB",
            session_id="tg-1",
            objective="dale dispara",
            runtime="telegram",
            mode="brain_fallback",
            status="running",
            route={"channel": "telegram", "external_session_id": "tg-1"},
            artifacts={"evidence_manifest": {"tools_run": ["Bash", "Write"]}},
        )
        self.ledger.mark_terminal(
            "tg-tB",
            status="completed_unverified",
            summary="brain tool-use turn: 19 tool calls (unverified)",
            verification_status="needs_verification",
            artifacts={"evidence_manifest": {"tools_run": ["Bash", "Write"]}},
        )
        self.ledger.create(
            task_id="ok-ok",
            session_id="tg-1",
            objective="happy path",
            runtime="telegram",
            mode="brain_fallback",
            status="running",
        )
        self.ledger.mark_terminal(
            "ok-ok",
            status="succeeded",
            summary="ok",
            verification_status="passed",
            artifacts={"evidence_manifest": {"verification_result": "passed"}},
        )

    def test_recommend_action_for_readonly_tools_is_auto_close_safe(self) -> None:
        action = recommend_reconciliation_action(tools=["Read", "Grep", "Glob"], error="")
        self.assertIn(action, {"auto_close_as_unverified_lookup", "needs_evidence_review"})
        self.assertNotEqual(action, "require_human_verification")

    def test_recommend_action_for_mutating_tools_requires_human(self) -> None:
        action = recommend_reconciliation_action(tools=["Bash", "Write"], error="")
        self.assertEqual(action, "require_human_verification")

    def test_recommend_action_with_error_investigates(self) -> None:
        action = recommend_reconciliation_action(tools=["Read"], error="boom")
        self.assertEqual(action, "investigate_failure")

    def test_completed_unverified_has_reconciliation_deadline(self) -> None:
        report = build_reconciliation_report(
            self.ledger, deadline_seconds=DEFAULT_RECONCILIATION_DEADLINE_SECONDS
        )
        # only the two unverified tasks (not the succeeded one)
        self.assertEqual(len(report["cases"]), 2)
        ids = {case["task_id"] for case in report["cases"]}
        self.assertEqual(ids, {"tg-tA", "tg-tB"})
        for case in report["cases"]:
            self.assertIn("task_id", case)
            self.assertIn("channel", case)
            self.assertIn("tools", case)
            self.assertIn("verification_status", case)
            self.assertIn("summary", case)
            self.assertIn("recommended_action", case)
            self.assertIn("deadline_at", case)
            # deadline must be future-dated
            self.assertGreater(case["deadline_at_epoch"], time.time())

    def test_recommended_action_per_case_uses_evidence_manifest_tools(self) -> None:
        report = build_reconciliation_report(self.ledger)
        case_by_id = {case["task_id"]: case for case in report["cases"]}
        # tg-tA had Read+Grep → not require_human_verification
        self.assertNotEqual(
            case_by_id["tg-tA"]["recommended_action"], "require_human_verification"
        )
        # tg-tB had Bash+Write → require_human_verification
        self.assertEqual(
            case_by_id["tg-tB"]["recommended_action"], "require_human_verification"
        )

    def test_emits_pending_verification_reconciliation_event(self) -> None:
        observe = _RecordingObserve()
        build_reconciliation_report(self.ledger, observe=observe)
        event_types = [e[0] for e in observe.events]
        self.assertIn("pending_verification_reconciliation", event_types)
        payload = next(p for et, p in observe.events if et == "pending_verification_reconciliation")
        self.assertIn("payload", payload)
        self.assertEqual(payload["payload"]["unverified_count"], 2)

    def test_write_reconciliation_report_dumps_json(self) -> None:
        with tempfile.TemporaryDirectory() as out_tmp:
            out_path = Path(out_tmp) / "report.json"
            written = write_reconciliation_report(self.ledger, out_path)
            self.assertEqual(written, out_path)
            data = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("generated_at", data)
            self.assertIn("cases", data)
            self.assertEqual(len(data["cases"]), 2)


class DrainReconcilableUnverifiedTests(unittest.TestCase):
    """PR2 Checkpoint C: gated drain of the SAFE subset (read-only, no-error,
    overdue) of the completed_unverified backlog.

    Eligible rows transition to the existing terminal ``status='cancelled'``
    with ``verification_status='auto_closed_unverified_lookup'`` (reuse-states;
    respects ``brain_tooluse_verify_flag_gated``). Mutating/error/not-yet-overdue
    rows are never touched. Off by default (``apply=False`` is a dry run) and
    not wired into the daemon at this checkpoint.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.observe = _RecordingObserve()
        self.ledger = TaskLedger(Path(self._tmp.name) / "claw.db", observe=self.observe)

    def _seed_unverified(
        self, task_id, tools, *, error="", overdue=True, verification_status="needs_verification"
    ) -> None:
        manifest = {"evidence_manifest": {"tools_run": list(tools)}}
        self.ledger.create(
            task_id=task_id,
            session_id="tg-1",
            objective="x",
            runtime="telegram",
            mode="brain_fallback",
            status="running",
            route={"channel": "telegram", "external_session_id": "tg-1"},
            artifacts=manifest,
        )
        self.ledger.mark_terminal(
            task_id,
            status="completed_unverified",
            summary="brain tool-use turn (unverified)",
            error=error,
            verification_status=verification_status,
            artifacts=manifest,
        )
        if overdue:
            self._backdate(task_id, 48 * 3600)

    def _backdate(self, task_id, seconds_ago) -> None:
        old = time.time() - seconds_ago
        with self.ledger._lock:
            self.ledger._conn.execute(
                "UPDATE agent_tasks SET completed_at = ?, updated_at = ? WHERE task_id = ?",
                (old, old, task_id),
            )
            self.ledger._conn.commit()

    def _verification_status(self, task_id):
        record = self.ledger.get(task_id)
        return record.verification_status if record else None

    def test_dry_run_lists_eligible_without_mutation(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self._seed_unverified("mut-overdue", ["Bash", "Write"])
        result = self.ledger.drain_reconcilable_unverified(apply=False)
        self.assertEqual(result["apply"], False)
        self.assertEqual(result["eligible_task_ids"], ["ro-overdue"])
        self.assertEqual(result["drained_count"], 0)
        # Dry run mutates nothing and closes no row (no per-row event).
        self.assertEqual(self._verification_status("ro-overdue"), "needs_verification")
        self.assertEqual(self._verification_status("mut-overdue"), "needs_verification")
        self.assertEqual(self._payloads("pending_verification_readonly_auto_closed"), [])

    def test_apply_drains_only_readonly_overdue_no_error(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self._seed_unverified("mut-overdue", ["Bash", "Write"])
        self._seed_unverified("ro-err-overdue", ["Read"], error="boom")
        self._seed_unverified("ro-fresh", ["Read", "Glob"], overdue=False)
        result = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result["drained_task_ids"], ["ro-overdue"])
        self.assertEqual(result["drained_count"], 1)
        # Drained row: cancelled + auto_closed_unverified_lookup (prod convention),
        # provenance stamped. completed_at preserved (terminal already).
        self.assertEqual(self.ledger.get("ro-overdue").status, "cancelled")
        self.assertEqual(
            self._verification_status("ro-overdue"), "auto_closed_unverified_lookup"
        )
        self.assertTrue(self.ledger.get("ro-overdue").metadata.get("reconciled_drained"))
        # Mutating / error / not-yet-overdue rows are left untouched.
        self.assertEqual(self._verification_status("mut-overdue"), "needs_verification")
        self.assertEqual(self._verification_status("ro-err-overdue"), "needs_verification")
        self.assertEqual(self._verification_status("ro-fresh"), "needs_verification")

    def test_drained_row_leaves_active_reconciliation_report(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=True)
        report = build_reconciliation_report(self.ledger)
        ids = {case["task_id"] for case in report["cases"]}
        self.assertNotIn("ro-overdue", ids)
        self.assertEqual(report["unverified_count"], 0)

    def _payloads(self, event_type):
        return [p["payload"] for et, p in self.observe.events if et == event_type]

    def test_apply_emits_per_row_auto_closed_event(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=True)
        rows = self._payloads("pending_verification_readonly_auto_closed")
        self.assertEqual(len(rows), 1)
        pl = rows[0]
        self.assertEqual(pl["task_id"], "ro-overdue")
        self.assertEqual(pl["previous_status"], "completed_unverified")
        self.assertEqual(pl["previous_verification_status"], "needs_verification")
        self.assertEqual(pl["new_status"], "cancelled")
        self.assertEqual(pl["new_verification_status"], "auto_closed_unverified_lookup")
        self.assertEqual(pl["recommended_action"], "auto_close_as_unverified_lookup")
        self.assertTrue(pl["apply"])
        self.assertFalse(pl["dry_run"])
        self.assertGreater(pl["age_seconds"], 0)

    def test_apply_emits_summary_with_skip_breakdown(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self._seed_unverified("mut-overdue", ["Bash", "Write"])
        self._seed_unverified("ro-err-overdue", ["Read"], error="boom")
        self._seed_unverified("ro-fresh", ["Read", "Glob"], overdue=False)
        self.ledger.drain_reconcilable_unverified(apply=True)
        summaries = self._payloads("pending_verification_readonly_drain_summary")
        self.assertEqual(len(summaries), 1)
        s = summaries[0]
        self.assertEqual(s["eligible"], 1)
        self.assertEqual(s["closed"], 1)
        self.assertEqual(s["skipped_mutating"], 1)
        self.assertEqual(s["skipped_error"], 1)
        self.assertEqual(s["skipped_not_overdue"], 1)
        self.assertTrue(s["apply"])
        self.assertFalse(s["dry_run"])

    def test_dry_run_emits_summary_but_no_per_row(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=False)
        self.assertEqual(self._payloads("pending_verification_readonly_auto_closed"), [])
        summaries = self._payloads("pending_verification_readonly_drain_summary")
        self.assertEqual(len(summaries), 1)
        s = summaries[0]
        self.assertEqual(s["eligible"], 1)
        self.assertEqual(s["closed"], 0)
        self.assertFalse(s["apply"])
        self.assertTrue(s["dry_run"])

    def test_second_apply_emits_noop_summary_no_per_row(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=True)
        self.observe.events.clear()  # isolate the second run
        self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(self._payloads("pending_verification_readonly_auto_closed"), [])
        summaries = self._payloads("pending_verification_readonly_drain_summary")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["closed"], 0)
        self.assertEqual(summaries[0]["eligible"], 0)

    def test_summary_limit_matches_real_scan_cap(self) -> None:
        # The reported limit/scan_capped must agree with the reconciler's real
        # per-call row cap (100), not a dead 500. Small data => not capped.
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=True)
        s = self._payloads("pending_verification_readonly_drain_summary")[0]
        self.assertEqual(s["limit"], 100)
        self.assertFalse(s["scan_capped"])
        self.assertEqual(s["skipped_state_changed"], 0)

    def test_needs_verify_alias_is_eligible_and_drained(self) -> None:
        # task_completion treats 'needs_verify' as a pending alias; classify and
        # apply must agree on it so it is not silently reported-eligible-but-skipped.
        self._seed_unverified("ro-nv", ["Read", "Grep"], verification_status="needs_verify")
        result = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result["drained_task_ids"], ["ro-nv"])
        self.assertEqual(self.ledger.get("ro-nv").status, "cancelled")

    def test_non_pending_verification_status_is_not_eligible(self) -> None:
        # A read-only overdue row that is NOT pending verification (e.g. already
        # 'passed') must not be counted eligible nor drained.
        self._seed_unverified("ro-passed", ["Read", "Grep"], verification_status="passed")
        result = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result["eligible_count"], 0)
        self.assertEqual(result["drained_count"], 0)
        self.assertEqual(self.ledger.get("ro-passed").status, "completed_unverified")

    def test_second_apply_is_noop(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=True)
        result2 = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result2["drained_count"], 0)
        self.assertEqual(result2["eligible_task_ids"], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
