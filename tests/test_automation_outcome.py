from __future__ import annotations

import unittest

from claw_v2.automation_outcome import AssertionResult, AutomationOutcome


class AutomationOutcomeTests(unittest.TestCase):
    def test_passed_requires_final_url_screenshot_and_positive_assertion(self) -> None:
        assertion = (AssertionResult(name="browser_reached_page", passed=True),)
        with self.assertRaises(ValueError):
            AutomationOutcome(
                status="passed",
                reason_code="ok",
                human_summary="ok",
                screenshot_artifact_id="shot.png",
                assertions=assertion,
            )
        with self.assertRaises(ValueError):
            AutomationOutcome(
                status="passed",
                reason_code="ok",
                human_summary="ok",
                final_url="https://example.com/",
                assertions=assertion,
            )
        with self.assertRaises(ValueError):
            AutomationOutcome(
                status="passed",
                reason_code="ok",
                human_summary="ok",
                final_url="https://example.com/",
                screenshot_artifact_id="shot.png",
            )

        outcome = AutomationOutcome.passed(
            human_summary="ok",
            final_url="https://example.com/",
            screenshot_artifact_id="shot.png",
            assertions=assertion,
        )

        self.assertTrue(outcome.is_passed_validated())

    def test_assertion_failure_invalidates_passed(self) -> None:
        with self.assertRaises(ValueError):
            AutomationOutcome.passed(
                human_summary="wrong page",
                final_url="https://evil.example/",
                screenshot_artifact_id="shot.png",
                assertions=(
                    AssertionResult(
                        name="expected_url_reached",
                        passed=False,
                        reason_code="wrong_page",
                    ),
                ),
            )

    def test_reason_code_and_summary_are_required(self) -> None:
        with self.assertRaises(ValueError):
            AutomationOutcome(status="no_result", reason_code="", human_summary="x")
        with self.assertRaises(ValueError):
            AutomationOutcome(status="no_result", reason_code="no_result", human_summary="")

    def test_evidence_ids_cannot_be_empty(self) -> None:
        with self.assertRaises(ValueError):
            AutomationOutcome(
                status="no_result",
                reason_code="no_result",
                human_summary="x",
                evidence_artifact_ids=("ok", ""),
            )

    def test_legacy_success_text_without_evidence_is_no_result(self) -> None:
        opened = AutomationOutcome.from_legacy_text("Navegador abierto")
        done = AutomationOutcome.from_legacy_text("Done")

        self.assertEqual(opened.status, "no_result")
        self.assertEqual(opened.reason_code, "missing_final_url")
        self.assertEqual(done.status, "no_result")
        self.assertEqual(done.reason_code, "missing_final_url")

    def test_legacy_text_with_url_screenshot_and_assertion_can_pass(self) -> None:
        outcome = AutomationOutcome.from_legacy_text(
            "Navegador abierto\nURL final: https://example.com/\nCaptura guardada: shot.png",
            objective="Abre https://example.com",
        )

        self.assertEqual(outcome.status, "passed")
        self.assertTrue(outcome.is_passed_validated())
        self.assertEqual(outcome.assertions[0].name, "expected_url_reached")

    def test_legacy_url_outside_objective_fails_wrong_page(self) -> None:
        outcome = AutomationOutcome.from_legacy_text(
            "Navegador abierto\nURL final: https://evil.example/\nCaptura guardada: shot.png",
            objective="Abre https://example.com",
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason_code, "wrong_page")

    def test_legacy_malformed_url_port_fails_without_crashing(self) -> None:
        outcome = AutomationOutcome.from_legacy_text(
            "Navegador abierto\nURL final: https://example.com:notaport/\nCaptura guardada: shot.png",
            objective="Abre https://example.com",
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason_code, "wrong_page")

    def test_legacy_invalid_idna_host_fails_without_crashing(self) -> None:
        bad_url = "https://" + chr(0xDCFF) + ".example/"
        outcome = AutomationOutcome.from_legacy_text(
            f"Navegador abierto\nURL final: {bad_url}\nCaptura guardada: shot.png",
            objective="Abre https://example.com",
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason_code, "wrong_page")

    def test_legacy_accented_login_marker_blocks(self) -> None:
        outcome = AutomationOutcome.from_legacy_text(
            "Inicia sesión para continuar\n"
            "URL final: https://example.com/\n"
            "Captura guardada: shot.png",
            objective="Abre https://example.com",
        )

        self.assertEqual(outcome.status, "needs_login")
        self.assertEqual(outcome.reason_code, "login_required")

    def test_legacy_challenge_marker_blocks(self) -> None:
        outcome = AutomationOutcome.from_legacy_text(
            "muro de verificación en la página\n"
            "URL final: https://example.com/checkpoint\n"
            "Captura guardada: shot.png",
            objective="Abre https://example.com",
        )

        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.reason_code, "challenge_required")


if __name__ == "__main__":
    unittest.main()
