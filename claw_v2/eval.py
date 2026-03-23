from __future__ import annotations

from dataclasses import dataclass, field

from claw_v2.llm import LLMRouter
from claw_v2.types import LLMResponse


@dataclass(slots=True)
class EvalCase:
    name: str
    prompt: str
    lane: str = "judge"
    expected_substrings: tuple[str, ...] = ()
    forbidden_substrings: tuple[str, ...] = ()
    evidence_pack: dict | None = None


@dataclass(slots=True)
class EvalResult:
    name: str
    passed: bool
    failures: list[str]
    response: LLMResponse


@dataclass(slots=True)
class EvalSuiteResult:
    name: str
    passed: int
    failed: int
    results: list[EvalResult] = field(default_factory=list)


class EvalHarness:
    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    def run_case(self, case: EvalCase) -> EvalResult:
        evidence_pack = case.evidence_pack
        if case.lane in {"verifier", "research", "judge"} and evidence_pack is None:
            evidence_pack = {"eval_case": case.name}
        response = self.router.ask(case.prompt, lane=case.lane, evidence_pack=evidence_pack)
        lowered = response.content.lower()
        failures: list[str] = []
        for expected in case.expected_substrings:
            if expected.lower() not in lowered:
                failures.append(f"missing expected substring: {expected}")
        for forbidden in case.forbidden_substrings:
            if forbidden.lower() in lowered:
                failures.append(f"found forbidden substring: {forbidden}")
        return EvalResult(case.name, not failures, failures, response)

    def run_suite(self, name: str, cases: list[EvalCase]) -> EvalSuiteResult:
        results = [self.run_case(case) for case in cases]
        passed = sum(1 for result in results if result.passed)
        failed = len(results) - passed
        return EvalSuiteResult(name=name, passed=passed, failed=failed, results=results)

    def run_self_improvement_gate(self, *, plan: str, diff: str, test_output: str) -> EvalResult:
        case = EvalCase(
            name="self-improvement-gate",
            prompt="Review the proposed self-improvement change and state whether it should proceed.",
            lane="verifier",
            expected_substrings=("proceed",),
            evidence_pack={"plan": plan, "diff": diff, "test_output": test_output},
        )
        return self.run_case(case)
