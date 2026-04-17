from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.llm import LLMRouter
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse


@dataclass(slots=True)
class EvalCase:
    name: str
    prompt: str | list[dict[str, Any]]
    lane: str = "judge"
    expected_substrings: tuple[str, ...] = ()
    forbidden_substrings: tuple[str, ...] = ()
    evidence_pack: dict | None = None
    system_prompt: str | None = None
    expected_response: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
        response = self.router.ask(
            case.prompt,
            lane=case.lane,
            evidence_pack=evidence_pack,
            system_prompt=case.system_prompt,
        )
        lowered = response.content.lower()
        failures: list[str] = []
        if case.expected_response is not None and _normalize_text(response.content) != _normalize_text(case.expected_response):
            failures.append("response did not match expected_response")
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

    @staticmethod
    def capture_trace_case(observe: ObserveStream, trace_id: str, *, name: str | None = None) -> EvalCase:
        events = observe.trace_events(trace_id)
        if not events:
            raise ValueError(f"trace not found: {trace_id}")
        decision_event = next((event for event in events if event["event_type"] == "llm_decision"), None)
        response_event = next((event for event in reversed(events) if event["event_type"] == "llm_response"), None)
        if decision_event is None or response_event is None:
            raise ValueError(f"trace {trace_id} does not contain a replayable llm decision/response pair")
        payload = decision_event["payload"]
        prompt_snapshot = payload.get("prompt_snapshot")
        if prompt_snapshot is None:
            raise ValueError(f"trace {trace_id} is missing prompt_snapshot")
        return EvalCase(
            name=name or f"trace-{trace_id[:12]}",
            prompt=prompt_snapshot,
            lane=decision_event["lane"] or "judge",
            evidence_pack=payload.get("evidence_pack_snapshot") or {},
            system_prompt=payload.get("system_prompt_snapshot"),
            expected_response=response_event["payload"].get("response_text") or response_event["payload"].get("content") or None,
            metadata={
                "trace_id": trace_id,
                "provider": decision_event.get("provider"),
                "model": decision_event.get("model"),
                "artifact_id": decision_event.get("artifact_id"),
            },
        )

    @staticmethod
    def save_case(case: EvalCase, path: Path | str) -> Path:
        target = Path(path)
        if target.suffix != ".json":
            target = target / f"{case.name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(case), indent=2, ensure_ascii=False), encoding="utf-8")
        return target

    @staticmethod
    def load_case(path: Path | str) -> EvalCase:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return EvalCase(**data)


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
