from __future__ import annotations

import unittest

from claw_v2.no_fudge import validate_no_fudge_factors


def _diff(path: str, added_line: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        f"+{added_line}\n"
    )


class NoFudgeTests(unittest.TestCase):
    def test_blocks_unjustified_numeric_constant_in_production_diff(self) -> None:
        report = validate_no_fudge_factors(_diff("claw_v2/risk.py", "score *= 1.37"))

        self.assertFalse(report.passed)
        self.assertEqual(report.status, "blocked")
        self.assertTrue(report.requires_human_approval)
        self.assertEqual(report.findings[0].file_path, "claw_v2/risk.py")
        self.assertEqual(report.findings[0].line_number, 1)
        self.assertEqual(report.findings[0].constants, ("1.37",))

    def test_allows_trivial_zero_one_and_negative_one(self) -> None:
        report = validate_no_fudge_factors(
            _diff("claw_v2/logic.py", "return 0 if value == -1 else 1")
        )

        self.assertTrue(report.passed)

    def test_allows_named_all_caps_constants(self) -> None:
        report = validate_no_fudge_factors(_diff("claw_v2/retry.py", "MAX_RETRIES = 3"))

        self.assertTrue(report.passed)

    def test_allows_trivial_length_checks(self) -> None:
        report = validate_no_fudge_factors(_diff("claw_v2/parser.py", "if len(parts) != 2:"))

        self.assertTrue(report.passed)

    def test_allows_enum_or_literal_context(self) -> None:
        literal_report = validate_no_fudge_factors(
            _diff("claw_v2/types.py", "Mode = Literal[2, 3]")
        )
        enum_report = validate_no_fudge_factors(
            _diff("claw_v2/types.py", "class RiskLevel(IntEnum):")
        )

        self.assertTrue(literal_report.passed)
        self.assertTrue(enum_report.passed)

    def test_allows_explicit_test_fixtures(self) -> None:
        report = validate_no_fudge_factors(_diff("tests/fixtures/risk.py", "score *= 1.37"))

        self.assertTrue(report.passed)

    def test_allows_evidence_level_justification(self) -> None:
        report = validate_no_fudge_factors(
            _diff("claw_v2/risk.py", "score *= 1.37"),
            evidence={"no_fudge_justification": "calibrated against benchmark fixture"},
        )

        self.assertTrue(report.passed)

    def test_allows_inline_justification_marker(self) -> None:
        report = validate_no_fudge_factors(
            _diff("claw_v2/risk.py", "score *= 1.37  # no-fudge: benchmark calibrated")
        )

        self.assertTrue(report.passed)


if __name__ == "__main__":
    unittest.main()
