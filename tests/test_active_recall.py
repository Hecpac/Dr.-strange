from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.active_recall import (
    RECALL_REQUEST_SCHEMA_VERSION,
    RECALL_RESULT_SCHEMA_VERSION,
    RecallResult,
    load_recall_records,
    quality_gate_for_reflection,
    record_recall_result,
    request_recall,
    search_recall_hits,
)


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class ActiveRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_request_and_result_are_persisted(self) -> None:
        observe = FakeObserve()
        request = request_recall(
            self.root,
            goal_id="g_1",
            session_id="tg-1",
            query="telemetry redaction",
            risk_level="high",
            action_tier="tier_2_5",
            observe=observe,
        )
        result = RecallResult(
            request_id=request.request_id,
            goal_id="g_1",
            hits=search_recall_hits("telemetry redaction", [
                {"memory_id": "m_1", "summary": "Telemetry redaction must hide tokens", "source": "MEMORY.md"}
            ]),
        )
        record_recall_result(self.root, result, observe=observe)

        records = load_recall_records(self.root)
        self.assertEqual(records[0]["schema_version"], RECALL_REQUEST_SCHEMA_VERSION)
        self.assertEqual(records[1]["schema_version"], RECALL_RESULT_SCHEMA_VERSION)
        self.assertEqual([event[0] for event in observe.events], ["recall_requested", "recall_result_recorded"])

    def test_search_orders_relevant_hits(self) -> None:
        hits = search_recall_hits("deploy verification telemetry", [
            {"memory_id": "m_low", "summary": "Only deploy notes", "source": "MEMORY.md"},
            {"memory_id": "m_high", "summary": "Deploy verification telemetry evidence", "source": "MEMORY.md"},
        ])

        self.assertEqual(hits[0].memory_id, "m_high")

    def test_quality_gate_rejects_missing_evidence(self) -> None:
        gate = quality_gate_for_reflection(
            goal_id="g_1",
            outcome="passed",
            evidence_refs=[],
            lesson="Always verify telemetry writes with JSONL reads",
        )

        self.assertFalse(gate.passed)

    def test_quality_gate_rejects_sensitive_lesson(self) -> None:
        gate = quality_gate_for_reflection(
            goal_id="g_1",
            outcome="passed",
            evidence_refs=["e_1"],
            lesson="Token was sk-abc123def456ghi789jkl012mno345",
        )

        self.assertFalse(gate.passed)

    def test_quality_gate_accepts_generalizable_lesson(self) -> None:
        gate = quality_gate_for_reflection(
            goal_id="g_1",
            outcome="failed",
            evidence_refs=["e_1"],
            lesson="When telemetry changes, verify both append writes and readback parsing",
        )

        self.assertTrue(gate.passed)


if __name__ == "__main__":
    unittest.main()

