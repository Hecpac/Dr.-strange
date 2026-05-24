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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
