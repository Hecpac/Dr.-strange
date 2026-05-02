from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.action_events import load_events
from claw_v2.evidence_ledger import (
    CLAIM_SCHEMA_VERSION,
    EvidenceRef,
    load_claims,
    record_claim,
)


class FakeObserve:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_: object) -> None:
        self.events.append((event_type, payload or {}))


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_claim_persists_claim(self) -> None:
        claim = record_claim(
            self.root,
            goal_id="g_1",
            claim_text="Tests passed",
            claim_type="fact",
            evidence_refs=[EvidenceRef(kind="tool_call", ref="pytest -q")],
            verification_status="verified",
            confidence=0.99,
        )

        self.assertEqual(claim.schema_version, CLAIM_SCHEMA_VERSION)
        loaded = load_claims(self.root)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].claim_text, "Tests passed")
        self.assertEqual(loaded[0].evidence_refs[0].kind, "tool_call")
        events = load_events(self.root)
        self.assertEqual(events[0].event_type, "claim_recorded")
        self.assertEqual(events[0].claims, [claim.claim_id])
        self.assertEqual(events[0].evidence_refs, ["tool_call:pytest -q"])

    def test_verified_claim_requires_tool_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "verified claims require"):
            record_claim(
                self.root,
                goal_id="g_1",
                claim_text="Unsupported fact",
                claim_type="fact",
                verification_status="verified",
            )

    def test_inference_can_exist_without_direct_evidence(self) -> None:
        claim = record_claim(
            self.root,
            goal_id="g_1",
            claim_text="Likely root cause is stale state",
            claim_type="inference",
            verification_status="unverified",
            depends_on=["c_1"],
        )

        self.assertEqual(claim.depends_on, ["c_1"])
        self.assertEqual(claim.evidence_refs, [])

    def test_observe_receives_claim_recorded(self) -> None:
        observe = FakeObserve()
        record_claim(
            self.root,
            goal_id="g_1",
            claim_text="Observed",
            claim_type="assumption",
            observe=observe,
        )

        self.assertEqual(observe.events[0][0], "claim_recorded")
        self.assertEqual(observe.events[0][1]["schema_version"], CLAIM_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
