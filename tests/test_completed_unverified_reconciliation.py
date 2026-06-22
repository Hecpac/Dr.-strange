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
from unittest import mock

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
        self.assertNotEqual(case_by_id["tg-tA"]["recommended_action"], "require_human_verification")
        # tg-tB had Bash+Write → require_human_verification
        self.assertEqual(case_by_id["tg-tB"]["recommended_action"], "require_human_verification")

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

    @staticmethod
    def _fake_scan_record(task_id, tools, *, error="", age_seconds=48 * 3600):
        # A record the scan (stale-ly) yields as a read-only candidate,
        # regardless of the row's real state — used to drive TOCTOU re-checks.
        from types import SimpleNamespace

        return SimpleNamespace(
            task_id=task_id,
            artifacts={"evidence_manifest": {"tools_run": list(tools)}},
            error=error,
            completed_at=time.time() - age_seconds,
        )

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
        self.assertEqual(self._verification_status("ro-overdue"), "auto_closed_unverified_lookup")
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
        self.assertEqual(s["skipped_classification_changed"], 0)

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

    def test_classification_revalidated_under_lock_fail_closed(self) -> None:
        # Row is ACTUALLY mutating (Bash/Write) + overdue + pending, but the scan
        # (stale-ly) yields it as a read-only candidate. The lock-held
        # re-classification on fresh data must FAIL CLOSED and not drain it.
        self._seed_unverified("toctou", ["Bash", "Write"])
        with mock.patch.object(
            self.ledger,
            "_scan_drainable_candidates",
            return_value=([self._fake_scan_record("toctou", ["Read"])], False),
        ):
            result = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result["drained_count"], 0)
        self.assertEqual(result["skipped_classification_changed"], 1)
        self.assertEqual(result["skipped_state_changed"], 0)
        self.assertEqual(self.ledger.get("toctou").status, "completed_unverified")
        self.assertEqual(self._payloads("pending_verification_readonly_auto_closed"), [])

    def test_state_revalidated_under_lock_skips_drifted_row(self) -> None:
        # Row drifted to a non-pending verification_status after a stale scan;
        # the status-pair guard skips it as state-changed (not classification).
        self._seed_unverified("drift", ["Read", "Grep"], verification_status="passed")
        with mock.patch.object(
            self.ledger,
            "_scan_drainable_candidates",
            return_value=([self._fake_scan_record("drift", ["Read"])], False),
        ):
            result = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result["drained_count"], 0)
        self.assertEqual(result["skipped_state_changed"], 1)
        self.assertEqual(result["skipped_classification_changed"], 0)
        self.assertEqual(self.ledger.get("drift").status, "completed_unverified")

    def test_max_apply_caps_closures_per_call(self) -> None:
        # D guardrail: never drain the whole backlog in one call.
        for i in range(4):
            self._seed_unverified(f"ro{i}", ["Read", "Grep"])
        result = self.ledger.drain_reconcilable_unverified(apply=True, max_apply=2)
        self.assertEqual(result["eligible_count"], 4)
        self.assertEqual(result["drained_count"], 2)
        self.assertEqual(result["skipped_over_max_apply"], 2)
        cancelled = sum(1 for i in range(4) if self.ledger.get(f"ro{i}").status == "cancelled")
        self.assertEqual(cancelled, 2)

    def test_eligible_behind_scan_cap_reachable_with_larger_scan(self) -> None:
        # Older mutating rows must not hide a (newer, still-overdue) read-only row
        # forever: a small scan misses it; a larger scan reaches it.
        self._seed_unverified("m1", ["Bash"], overdue=False)
        self._backdate("m1", 100 * 3600)
        self._seed_unverified("m2", ["Write"], overdue=False)
        self._backdate("m2", 99 * 3600)
        self._seed_unverified("ro", ["Read", "Grep"], overdue=False)
        self._backdate("ro", 50 * 3600)  # >24h overdue, but newest of the three
        small = self.ledger.drain_reconcilable_unverified(apply=True, max_scan=2)
        self.assertEqual(small["drained_count"], 0)
        self.assertTrue(small["scan_capped"])
        self.assertEqual(self.ledger.get("ro").status, "completed_unverified")
        big = self.ledger.drain_reconcilable_unverified(apply=True, max_scan=10)
        self.assertFalse(big["scan_capped"])
        self.assertEqual(big["drained_count"], 1)
        self.assertEqual(self.ledger.get("ro").status, "cancelled")

    def test_scan_capped_is_exact_via_limit_plus_one(self) -> None:
        # Exactly max_scan rows must NOT report capped (limit+1 proof); one more does.
        self._seed_unverified("a", ["Bash"])
        self._seed_unverified("b", ["Bash"])
        at_cap = self.ledger.drain_reconcilable_unverified(apply=False, max_scan=2)
        self.assertFalse(at_cap["scan_capped"])
        self.assertEqual(at_cap["scanned"], 2)
        over_cap = self.ledger.drain_reconcilable_unverified(apply=False, max_scan=1)
        self.assertTrue(over_cap["scan_capped"])
        self.assertEqual(over_cap["scanned"], 1)

    def test_apply_rolls_back_batch_on_midbatch_failure(self) -> None:
        # A failure after the 1st row's UPDATE (json.dumps is called once per row
        # to build the UPDATE param, only in the apply loop) must roll the whole
        # batch back and propagate — no partial drain.
        import json as _json

        self._seed_unverified("r1", ["Read", "Grep"])
        self._seed_unverified("r2", ["Read", "Glob"])
        real_dumps = _json.dumps
        state = {"n": 0}

        def flaky_dumps(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 2:
                raise RuntimeError("boom mid-batch")
            return real_dumps(*args, **kwargs)

        with mock.patch("claw_v2.task_ledger.json.dumps", side_effect=flaky_dumps):
            with self.assertRaises(RuntimeError):
                self.ledger.drain_reconcilable_unverified(apply=True)
        # Both rows were rolled back — neither drained, no per-row events.
        self.assertEqual(self.ledger.get("r1").status, "completed_unverified")
        self.assertEqual(self.ledger.get("r2").status, "completed_unverified")

    def test_second_apply_is_noop(self) -> None:
        self._seed_unverified("ro-overdue", ["Read", "Grep"])
        self.ledger.drain_reconcilable_unverified(apply=True)
        result2 = self.ledger.drain_reconcilable_unverified(apply=True)
        self.assertEqual(result2["drained_count"], 0)
        self.assertEqual(result2["eligible_task_ids"], [])

    def test_failure_review_reconciles_only_error_bearing_rows(self) -> None:
        self._seed_unverified("mut-error", ["Bash", "Write"], error="write failed")
        self._seed_unverified("readonly-error", ["Read"], error="lookup failed")
        self._seed_unverified("delegate-review", ["mcp__claw__delegate_task"])
        self._seed_unverified("mut-no-error", ["Bash", "Write"])

        result = self.ledger.reconcile_failed_unverified(apply=True)

        self.assertEqual(result["eligible_task_ids"], ["mut-error", "readonly-error"])
        self.assertEqual(result["reconciled_count"], 2)
        self.assertEqual(self.ledger.get("mut-error").status, "failed")
        self.assertEqual(self._verification_status("mut-error"), "failed")
        self.assertTrue(self.ledger.get("mut-error").metadata.get("reconciled_failure_review"))
        self.assertEqual(self.ledger.get("readonly-error").status, "failed")
        self.assertEqual(self.ledger.get("delegate-review").status, "completed_unverified")
        self.assertEqual(self._verification_status("delegate-review"), "needs_verification")
        self.assertEqual(self.ledger.get("mut-no-error").status, "completed_unverified")

        events = self._payloads("pending_verification_failure_reconciled")
        self.assertEqual({event["task_id"] for event in events}, {"mut-error", "readonly-error"})
        self.assertTrue(all(event["new_status"] == "failed" for event in events))

    def test_failure_review_dry_run_lists_without_mutation(self) -> None:
        self._seed_unverified("mut-error", ["Bash", "Write"], error="write failed")

        result = self.ledger.reconcile_failed_unverified(apply=False)

        self.assertEqual(result["eligible_task_ids"], ["mut-error"])
        self.assertEqual(result["reconciled_count"], 0)
        self.assertEqual(self.ledger.get("mut-error").status, "completed_unverified")
        self.assertEqual(self._payloads("pending_verification_failure_reconciled"), [])
        summaries = self._payloads("pending_verification_failure_review_summary")
        self.assertEqual(summaries[-1]["eligible"], 1)
        self.assertFalse(summaries[-1]["apply"])

    def test_failure_review_revalidates_classification_under_lock(self) -> None:
        self._seed_unverified("delegate-review", ["mcp__claw__delegate_task"])
        with mock.patch.object(
            self.ledger,
            "_scan_drainable_candidates",
            return_value=(
                [self._fake_scan_record("delegate-review", ["Read"], error="stale error")],
                False,
            ),
        ):
            result = self.ledger.reconcile_failed_unverified(apply=True)

        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["reconciled_count"], 0)
        self.assertEqual(result["skipped_classification_changed"], 1)
        self.assertEqual(self.ledger.get("delegate-review").status, "completed_unverified")
        self.assertEqual(self._payloads("pending_verification_failure_reconciled"), [])

    def test_failure_review_max_apply_caps_reconciliations(self) -> None:
        for i in range(3):
            self._seed_unverified(f"err{i}", ["Bash"], error=f"boom {i}")

        result = self.ledger.reconcile_failed_unverified(apply=True, max_apply=2)

        self.assertEqual(result["eligible_count"], 3)
        self.assertEqual(result["reconciled_count"], 2)
        self.assertEqual(result["skipped_over_max_apply"], 1)
        failed = sum(1 for i in range(3) if self.ledger.get(f"err{i}").status == "failed")
        self.assertEqual(failed, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
