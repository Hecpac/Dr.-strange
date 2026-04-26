from __future__ import annotations

import unittest

from claw_v2.retry_policy import ProviderCircuitBreaker, RetryStuckPolicy


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


class ProviderCircuitBreakerTests(unittest.TestCase):
    def test_opens_after_threshold_and_blocks_until_cooldown(self) -> None:
        now = [100.0]
        circuit = ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=lambda: now[0])

        self.assertTrue(circuit.check("openai").allowed)
        first = circuit.record_failure("openai", "timeout")
        self.assertEqual(first.status, "closed")
        second = circuit.record_failure("openai", "timeout")
        self.assertEqual(second.status, "open")
        self.assertTrue(second.changed)

        blocked = circuit.check("openai")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.status, "open")

        now[0] = 131.0
        half_open = circuit.check("openai")
        self.assertTrue(half_open.allowed)
        self.assertEqual(half_open.status, "half_open")

    def test_success_after_open_recovers_circuit(self) -> None:
        now = [100.0]
        circuit = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=lambda: now[0])
        circuit.record_failure("anthropic", "boom")
        now[0] = 111.0

        transition = circuit.record_success("anthropic")

        self.assertTrue(transition.changed)
        self.assertTrue(circuit.check("anthropic").allowed)
        self.assertEqual(circuit.check("anthropic").status, "closed")


if __name__ == "__main__":
    unittest.main()
