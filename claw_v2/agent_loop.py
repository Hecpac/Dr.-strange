"""Brain-bypass refactor commit #7: minimal AgentLoop.

Replaces the legacy pattern of "execute once, wait for the user to say
'sigue'" with an explicit plan -> act -> observe -> verify -> replan cycle.
Each phase is a callable injected at construction so the loop is trivially
testable and so different agent classes can supply different planners,
verifiers, and critics without subclassing.

The loop itself is intentionally small. It owns iteration accounting, status
propagation, and a structured trace of what happened. It does not own model
choice, prompt engineering, or evidence persistence — those belong to the
injected callables and to the surrounding services (SubAgentService,
TaskLedger, EvidenceVerifier).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from claw_v2.agents import SubAgentResult


Plan = str
Observation = str


@dataclass(frozen=True, slots=True)
class VerifierVerdict:
    """Outcome of verifying a single iteration's result."""

    status: str  # "passed" | "failed" | "partial"
    reason: str = ""


@dataclass(frozen=True, slots=True)
class IterationTrace:
    """Per-iteration breadcrumb so callers can audit what the loop did."""

    iteration: int
    plan: Plan
    result: SubAgentResult
    observation: Observation
    verdict: VerifierVerdict
    critique: str | None = None


@dataclass(frozen=True, slots=True)
class AgentLoopOutcome:
    """Final result of running an AgentLoop."""

    status: str  # "passed" | "failed" | "exhausted"
    final_result: SubAgentResult | None
    history: tuple[IterationTrace, ...]
    reason: str = ""


@dataclass(slots=True)
class AgentLoop:
    """Plan -> act -> observe -> verify -> replan cycle.

    Args:
        planner: ``(goal, history) -> Plan``. Produces the next plan based on
            the goal and what has happened so far. On the first iteration the
            history is empty.
        executor: ``(plan) -> SubAgentResult``. Runs the plan and returns a
            structured result.
        observer: ``(result) -> Observation``. Summarizes what the executor
            produced into a short string the verifier and critic can read.
            Defaults to ``result.summary``.
        verifier: ``(result, observation) -> VerifierVerdict``. Decides
            whether the iteration passed, failed, or was partial.
        critic: ``(history) -> str``. On failed/partial verdicts, produces a
            critique that the planner can read to revise the next plan. May
            be ``None`` if no critique is wanted.
        max_iterations: hard cap on the number of cycles. Reaching it returns
            an ``"exhausted"`` outcome.
    """

    planner: Callable[[str, tuple[IterationTrace, ...]], Plan]
    executor: Callable[[Plan], SubAgentResult]
    verifier: Callable[[SubAgentResult, Observation], VerifierVerdict]
    observer: Callable[[SubAgentResult], Observation] = field(
        default=lambda result: result.summary
    )
    critic: Callable[[tuple[IterationTrace, ...]], str] | None = None
    max_iterations: int = 3

    def run(self, goal: str) -> AgentLoopOutcome:
        history: list[IterationTrace] = []
        for iteration in range(1, self.max_iterations + 1):
            plan = self.planner(goal, tuple(history))
            result = self.executor(plan)
            observation = self.observer(result)
            verdict = self.verifier(result, observation)
            critique: str | None = None
            if verdict.status != "passed" and self.critic is not None:
                critique = self.critic(
                    tuple(
                        history
                        + [
                            IterationTrace(
                                iteration=iteration,
                                plan=plan,
                                result=result,
                                observation=observation,
                                verdict=verdict,
                            )
                        ]
                    )
                )
            history.append(
                IterationTrace(
                    iteration=iteration,
                    plan=plan,
                    result=result,
                    observation=observation,
                    verdict=verdict,
                    critique=critique,
                )
            )
            if verdict.status == "passed":
                return AgentLoopOutcome(
                    status="passed",
                    final_result=result,
                    history=tuple(history),
                    reason=verdict.reason,
                )
        last = history[-1] if history else None
        return AgentLoopOutcome(
            status="exhausted",
            final_result=last.result if last else None,
            history=tuple(history),
            reason=f"max_iterations={self.max_iterations} reached without pass",
        )
