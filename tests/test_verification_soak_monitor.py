"""Tests for the Petri verifier soak monitor (commit #9)."""
from __future__ import annotations

import unittest

from claw_v2.verification import (
    DimensionRawResponse,
    SoakSummary,
    THRESHOLDS_DOC,
    summarize_petri_scores,
)


def _report(
    overall: str,
    *,
    state_amnesia: tuple[int, bool] = (1, False),
    verification_drift: tuple[int, bool] = (1, False),
) -> dict:
    return {
        "task_id": "stub",
        "overall_status": overall,
        "failures": [],
        "scores": [
            {
                "name": "state_amnesia",
                "score": state_amnesia[0],
                "reason": "",
                "threshold_fail": 3,
                "failed": state_amnesia[1],
            },
            {
                "name": "verification_drift",
                "score": verification_drift[0],
                "reason": "",
                "threshold_fail": 3,
                "failed": verification_drift[1],
            },
        ],
    }


class SoakMonitorTests(unittest.TestCase):
    def test_empty_input_yields_zero_summary(self) -> None:
        summary = summarize_petri_scores([])
        self.assertEqual(summary.total_reports, 0)
        self.assertEqual(summary.overall_fail_rate, 0.0)
        self.assertEqual(summary.per_dimension, ())

    def test_per_dimension_aggregation(self) -> None:
        reports = [
            _report("passed", state_amnesia=(1, False), verification_drift=(1, False)),
            _report("failed", state_amnesia=(1, False), verification_drift=(7, True)),
            _report("passed", state_amnesia=(2, False), verification_drift=(2, False)),
            _report("failed", state_amnesia=(8, True), verification_drift=(1, False)),
        ]
        summary = summarize_petri_scores(reports)
        self.assertIsInstance(summary, SoakSummary)
        self.assertEqual(summary.total_reports, 4)
        self.assertEqual(summary.overall_fail_rate, 0.5)
        by_name = {d.name: d for d in summary.per_dimension}
        self.assertEqual(by_name["state_amnesia"].samples, 4)
        self.assertEqual(by_name["state_amnesia"].fail_rate, 0.25)
        self.assertEqual(by_name["state_amnesia"].score_max, 8)
        self.assertEqual(by_name["verification_drift"].fail_rate, 0.25)
        self.assertEqual(by_name["verification_drift"].score_max, 7)

    def test_format_human_includes_dimension_lines(self) -> None:
        reports = [_report("passed", state_amnesia=(1, False), verification_drift=(1, False))]
        text = summarize_petri_scores(reports).format_human()
        self.assertIn("Petri soak summary: 1 reports", text)
        self.assertIn("state_amnesia", text)
        self.assertIn("verification_drift", text)
        self.assertIn("fail_rate=", text)

    def test_to_dict_roundtrip_shape(self) -> None:
        reports = [_report("failed", verification_drift=(6, True))]
        payload = summarize_petri_scores(reports).to_dict()
        self.assertEqual(payload["total_reports"], 1)
        self.assertEqual(payload["overall_fail_rate"], 1.0)
        names = {d["name"] for d in payload["per_dimension"]}
        self.assertEqual(names, {"state_amnesia", "verification_drift"})

    def test_skips_malformed_score_entries(self) -> None:
        reports = [
            {
                "overall_status": "passed",
                "scores": [
                    {"name": "good", "score": 2, "failed": False},
                    {"name": "", "score": 5, "failed": True},  # empty name -> skipped
                    {"name": "bad", "score": "NaN", "failed": False},  # bad score -> skipped
                    "not a dict",
                ],
            },
            {"overall_status": "passed", "scores": "not a list"},
        ]
        summary = summarize_petri_scores(reports)
        names = {d.name for d in summary.per_dimension}
        self.assertEqual(names, {"good"})

    def test_thresholds_doc_present(self) -> None:
        # The doc string is referenced from the spec — keep it discoverable.
        self.assertIn("state_amnesia", THRESHOLDS_DOC)
        self.assertIn("verification_drift", THRESHOLDS_DOC)
        self.assertIn("CLAW_PETRI_VERIFIER_ENABLED", THRESHOLDS_DOC)


if __name__ == "__main__":
    unittest.main()
    # silence unused import: DimensionRawResponse is re-exported sanity-check
    _ = DimensionRawResponse
