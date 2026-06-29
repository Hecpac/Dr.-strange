from __future__ import annotations

import unittest

from claw_v2.automation_contracts import (
    AutomationExecutor,
    AutomationIntent,
    AutomationOutcome,
    AutomationRequest,
    AutomationStatus,
    AutomationSurface,
    CapabilityGrant,
)


class CapabilityGrantTests(unittest.TestCase):
    def test_browser_request_serializes_stable_fields(self) -> None:
        request = AutomationRequest.browser(
            request_id="req-1",
            session_id="s1",
            task_id="t1",
            objective="open x",
            mode="browse",
            intent=AutomationIntent.OPEN_URL,
            target_url="https://x.com/home",
            target_domains=["https://x.com/home", "x.com"],
            requested_actions=["navigate", "screenshot"],
            evidence_required=["url", "title", "screenshot"],
            time_budget_seconds=120,
        )

        payload = request.to_dict()

        self.assertEqual(payload["surface"], "browser")
        self.assertEqual(payload["intent"], "open_url")
        self.assertEqual(payload["target_domains"], ["x.com"])
        self.assertEqual(payload["requested_actions"], ["navigate", "screenshot"])
        self.assertEqual(payload["model_policy"], "subscription_first")

    def test_browser_read_grant_serializes_domains_and_actions(self) -> None:
        grant = CapabilityGrant.browser_read(
            domains=["https://Example.com/path", "example.com", "x.com"],
            reason="delegated browser read",
            auto_approved=True,
        )

        payload = grant.to_dict()

        self.assertEqual(payload["surface"], "browser")
        self.assertEqual(payload["approved_domains"], ["example.com", "x.com"])
        self.assertEqual(payload["allowed_high_risk_actions"], ["evaluate", "save_as_pdf"])
        self.assertTrue(payload["allow_high_risk_actions"])
        self.assertTrue(payload["auto_approved"])

    def test_browser_read_grant_allows_evaluate_on_approved_domain(self) -> None:
        grant = CapabilityGrant.browser_read(
            domains=["x.com"],
            reason="read feed",
            auto_approved=True,
        )

        self.assertTrue(
            grant.allows_browser_use_action(
                "evaluate",
                url="https://x.com/home",
                params={},
            )
        )

    def test_browser_read_grant_blocks_upload_even_on_approved_domain(self) -> None:
        grant = CapabilityGrant.browser_read(
            domains=["x.com"],
            reason="read feed",
            auto_approved=True,
        )

        self.assertFalse(
            grant.allows_browser_use_action(
                "upload_file",
                url="https://x.com/home",
                params={"path": "/tmp/image.png"},
            )
        )

    def test_browser_read_grant_blocks_unapproved_domain(self) -> None:
        grant = CapabilityGrant.browser_read(
            domains=["x.com"],
            reason="read feed",
            auto_approved=True,
        )

        self.assertFalse(
            grant.allows_browser_use_action(
                "evaluate",
                url="https://ads.google.com/campaigns",
                params={},
            )
        )


class AutomationOutcomeTests(unittest.TestCase):
    def test_outcome_dict_uses_stable_status_and_executor_values(self) -> None:
        outcome = AutomationOutcome.passed(
            surface=AutomationSurface.BROWSER,
            executor=AutomationExecutor.DETERMINISTIC_BROWSER,
            summary="opened",
            artifacts=["/tmp/open.png"],
        )

        payload = outcome.to_dict()

        self.assertEqual(payload["status"], AutomationStatus.PASSED.value)
        self.assertEqual(payload["surface"], "browser")
        self.assertEqual(payload["executor"], "deterministic_browser")
        self.assertEqual(payload["summary"], "opened")
        self.assertEqual(payload["reason_code"], "passed")
        self.assertEqual(payload["artifacts"], ["/tmp/open.png"])


if __name__ == "__main__":
    unittest.main()
