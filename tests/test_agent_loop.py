"""Tests for claw_v2.agent_loop (brain-bypass refactor commit #7)."""
from __future__ import annotations

import unittest

from claw_v2.agent_loop import (
    AgentLoop,
    AgentLoopOutcome,
    IterationTrace,
    VerifierVerdict,
)
from claw_v2.agents import SubAgentResult


def _make_result(summary: str, status: str = "succeeded") -> SubAgentResult:
    return SubAgentResult(status=status, summary=summary)


class AgentLoopTests(unittest.TestCase):
    def test_passes_on_first_iteration_when_verifier_passes(self) -> None:
        loop = AgentLoop(
            planner=lambda goal, history: f"plan:{goal}",
            executor=lambda plan: _make_result(f"did:{plan}"),
            verifier=lambda result, obs: VerifierVerdict(status="passed"),
        )

        outcome = loop.run("ship feature")

        self.assertEqual(outcome.status, "passed")
        self.assertIsNotNone(outcome.final_result)
        assert outcome.final_result is not None
        self.assertEqual(outcome.final_result.summary, "did:plan:ship feature")
        self.assertEqual(len(outcome.history), 1)
        self.assertEqual(outcome.history[0].iteration, 1)
        self.assertEqual(outcome.history[0].verdict.status, "passed")

    def test_replans_after_failed_verdict_and_passes_on_retry(self) -> None:
        attempts: list[str] = []

        def planner(goal: str, history: tuple[IterationTrace, ...]) -> str:
            return f"attempt-{len(history) + 1}"

        def executor(plan: str) -> SubAgentResult:
            attempts.append(plan)
            return _make_result(f"ran:{plan}")

        def verifier(result: SubAgentResult, observation: str) -> VerifierVerdict:
            if "attempt-1" in result.summary:
                return VerifierVerdict(status="failed", reason="missing tests")
            return VerifierVerdict(status="passed")

        critiques: list[str] = []

        def critic(history: tuple[IterationTrace, ...]) -> str:
            critique = f"critique-after-{len(history)}"
            critiques.append(critique)
            return critique

        loop = AgentLoop(
            planner=planner,
            executor=executor,
            verifier=verifier,
            critic=critic,
            max_iterations=4,
        )

        outcome = loop.run("ship feature")

        self.assertEqual(outcome.status, "passed")
        self.assertEqual(attempts, ["attempt-1", "attempt-2"])
        self.assertEqual(len(outcome.history), 2)
        self.assertEqual(outcome.history[0].verdict.status, "failed")
        self.assertEqual(outcome.history[0].critique, "critique-after-1")
        self.assertEqual(outcome.history[1].verdict.status, "passed")
        self.assertIsNone(outcome.history[1].critique)
        self.assertEqual(critiques, ["critique-after-1"])

    def test_exhausts_after_max_iterations_without_pass(self) -> None:
        loop = AgentLoop(
            planner=lambda goal, history: "p",
            executor=lambda plan: _make_result("nope", status="failed"),
            verifier=lambda result, obs: VerifierVerdict(status="failed", reason="nope"),
            max_iterations=2,
        )

        outcome = loop.run("ship feature")

        self.assertEqual(outcome.status, "exhausted")
        self.assertEqual(len(outcome.history), 2)
        self.assertIn("max_iterations=2", outcome.reason)

    def test_default_observer_uses_result_summary(self) -> None:
        seen_observations: list[str] = []

        def verifier(result: SubAgentResult, observation: str) -> VerifierVerdict:
            seen_observations.append(observation)
            return VerifierVerdict(status="passed")

        loop = AgentLoop(
            planner=lambda goal, history: "plan",
            executor=lambda plan: _make_result("hello world"),
            verifier=verifier,
        )

        loop.run("ship feature")

        self.assertEqual(seen_observations, ["hello world"])

    def test_custom_observer_sees_result_and_passes_to_verifier(self) -> None:
        loop = AgentLoop(
            planner=lambda goal, history: "plan",
            executor=lambda plan: _make_result("hello"),
            observer=lambda result: f"observed:{result.summary}",
            verifier=lambda result, observation: VerifierVerdict(
                status="passed" if observation.startswith("observed:") else "failed",
                reason=observation,
            ),
        )

        outcome = loop.run("ship feature")

        self.assertEqual(outcome.status, "passed")
        self.assertEqual(outcome.history[0].observation, "observed:hello")

    def test_critic_is_skipped_when_none(self) -> None:
        loop = AgentLoop(
            planner=lambda goal, history: "plan",
            executor=lambda plan: _make_result("nope", status="failed"),
            verifier=lambda result, obs: VerifierVerdict(status="failed"),
            critic=None,
            max_iterations=2,
        )

        outcome = loop.run("ship feature")

        self.assertEqual(outcome.status, "exhausted")
        for trace in outcome.history:
            self.assertIsNone(trace.critique)


if __name__ == "__main__":
    unittest.main()
