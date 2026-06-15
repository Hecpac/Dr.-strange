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

import time
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
    observer: Callable[[SubAgentResult], Observation] = field(default=lambda result: result.summary)
    critic: Callable[[tuple[IterationTrace, ...]], str] | None = None
    max_iterations: int = 3
    # Wave 2.2: budget guards alongside max_iterations. Iteration count is a
    # poor proxy for spend when each iteration is an Opus call. cost_tracker
    # returns CUMULATIVE USD spent since loop start; AgentLoop subtracts the
    # baseline at .run() entry so the loop only accounts for its own spend.
    # max_wallclock_s caps total wall-time (planner + executor + verifier).
    # If the corresponding tracker is None, that guard is disabled.
    max_cost_usd: float | None = None
    max_wallclock_s: float | None = None
    cost_tracker: Callable[[], float] | None = None
    clock: Callable[[], float] = field(default=time.time)

    def run(self, goal: str) -> AgentLoopOutcome:
        history: list[IterationTrace] = []
        start_at = self.clock()
        cost_baseline = self.cost_tracker() if self.cost_tracker is not None else 0.0
        for iteration in range(1, self.max_iterations + 1):
            exhausted = self._budget_exhausted(start_at, cost_baseline)
            if exhausted is not None:
                last = history[-1] if history else None
                return AgentLoopOutcome(
                    status="exhausted",
                    final_result=last.result if last else None,
                    history=tuple(history),
                    reason=exhausted,
                )
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

    def _budget_exhausted(self, start_at: float, cost_baseline: float) -> str | None:
        if self.max_wallclock_s is not None:
            elapsed = self.clock() - start_at
            if elapsed >= self.max_wallclock_s:
                return f"wallclock_exhausted: elapsed={elapsed:.1f}s >= max={self.max_wallclock_s:.1f}s"
        if self.max_cost_usd is not None and self.cost_tracker is not None:
            cost_used = max(self.cost_tracker() - cost_baseline, 0.0)
            if cost_used >= self.max_cost_usd:
                return f"budget_exhausted: cost=${cost_used:.4f} >= max=${self.max_cost_usd:.4f}"
        return None
