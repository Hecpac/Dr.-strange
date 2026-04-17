from __future__ import annotations

import unittest

from claw_v2.retry_policy import RetryStuckPolicy


class RetryStuckPolicyTests(unittest.TestCase):
    def test_same_tool_switches_after_three_failures(self) -> None:
        policy = RetryStuckPolicy()

        self.assertEqual(policy.record_failure("webfetch").action, "retry")
        self.assertEqual(policy.record_failure("webfetch").action, "retry")
        decision = policy.record_failure("webfetch")

        self.assertEqual(decision.action, "switch_tool")
        self.assertEqual(decision.failures_for_tool, 3)
        self.assertFalse(decision.truly_stuck)

    def test_three_distinct_failed_tools_means_truly_stuck(self) -> None:
        policy = RetryStuckPolicy()

        self.assertEqual(policy.record_failure("webfetch").action, "retry")
        self.assertEqual(policy.record_failure("firecrawl").action, "retry")
        decision = policy.record_failure("cdp")

        self.assertEqual(decision.action, "ask_user")
        self.assertEqual(decision.distinct_failed_tools, 3)
        self.assertTrue(decision.truly_stuck)

    def test_reset_tool_clears_only_that_tool(self) -> None:
        policy = RetryStuckPolicy()

        policy.record_failure("webfetch")
        policy.record_failure("firecrawl")
        policy.reset_tool("webfetch")
        decision = policy.record_failure("webfetch")

        self.assertEqual(decision.action, "retry")
        self.assertEqual(decision.failures_for_tool, 1)
        self.assertEqual(decision.distinct_failed_tools, 2)


if __name__ == "__main__":
    unittest.main()
