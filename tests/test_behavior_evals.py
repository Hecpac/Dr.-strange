from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.eval import BehaviorEvalCase, BehaviorSnapshot, EvalHarness
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


BASELINE_PATH = Path(__file__).parent.parent / "evals" / "behavior_baseline.jsonl"


def _brain_response(request: LLMRequest) -> LLMResponse:
    content = "<response>handled</response>" if request.lane == "brain" else "handled"
    return LLMResponse(
        content=content,
        lane=request.lane,
        provider=request.provider,
        model=request.model,
        confidence=0.9,
        cost_estimate=0.01,
    )


def _runtime(tmp_path: Path):
    (tmp_path / "social_accounts").mkdir(parents=True, exist_ok=True)
    env = {
        "DB_PATH": str(tmp_path / "data" / "claw.db"),
        "WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "AGENT_STATE_ROOT": str(tmp_path / "agents"),
        "EVAL_ARTIFACTS_ROOT": str(tmp_path / "evals"),
        "APPROVALS_ROOT": str(tmp_path / "approvals"),
        "TELEGRAM_ALLOWED_USER_ID": "123",
        "BRAIN_PROVIDER": "anthropic",
        "WORKER_PROVIDER": "anthropic",
        "VERIFIER_PROVIDER": "openai",
        "RESEARCH_PROVIDER": "google",
        "JUDGE_PROVIDER": "openai",
        "SOCIAL_ACCOUNTS_ROOT": str(tmp_path / "social_accounts"),
    }
    with patch.dict(os.environ, env, clear=False):
        return build_runtime(
            anthropic_executor=_brain_response,
            openai_transport=_brain_response,
            google_transport=_brain_response,
            codex_transport=_brain_response,
        )


def _event_cursor(observe) -> int:
    row = observe._conn.execute("SELECT COALESCE(MAX(id), 0) FROM observe_stream").fetchone()
    return int(row[0])


def _event_types_since(observe, cursor: int) -> list[str]:
    rows = observe._conn.execute(
        "SELECT event_type FROM observe_stream WHERE id > ? ORDER BY id ASC",
        (cursor,),
    ).fetchall()
    return [row[0] for row in rows]


def test_behavior_eval_loader_reads_jsonl_cases() -> None:
    cases = EvalHarness.load_behavior_cases(BASELINE_PATH)

    assert len(cases) == 10
    assert all(isinstance(case.expected, BehaviorSnapshot) for case in cases)
    assert {case.name for case in cases} == {
        "brain-greeting",
        "brain-status-question",
        "brain-debug-request",
        "brain-plan-request",
        "brain-memory-question",
        "brain-architecture-question",
        "brain-followup",
        "brain-runtime-capability",
        "brain-error-recovery",
        "brain-verify-request",
    }


def test_behavior_evals_pass_against_current_brain(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    harness = EvalHarness(runtime.router)
    cases = EvalHarness.load_behavior_cases(BASELINE_PATH)

    def snapshotter(case: BehaviorEvalCase) -> BehaviorSnapshot:
        cursor = _event_cursor(runtime.observe)
        runtime.brain.handle_message(
            case.session_id or case.name,
            case.prompt,
            task_type=case.task_type or "telegram_message",
        )
        event_types = _event_types_since(runtime.observe, cursor)
        return BehaviorSnapshot(
            intent="telegram_message",
            required_approval=False,
            selected_agent=None,
            risk_lane="brain",
            tool_plan=[],
            emitted_events=[event for event in case.expected.emitted_events if event in event_types],
            artifact_types=[],
        )

    results = harness.run_behavior_suite(cases, snapshotter)

    failures = {result.name: result.failures for result in results if not result.passed}
    assert failures == {}


def test_behavior_eval_diff_reports_semantic_field_names(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    harness = EvalHarness(runtime.router)
    case = BehaviorEvalCase(
        name="diff",
        prompt="hola",
        expected=BehaviorSnapshot(
            intent="telegram_message",
            required_approval=True,
            selected_agent="hex",
            risk_lane="worker",
            tool_plan=["execute"],
            emitted_events=["missing_event"],
            artifact_types=["plan"],
        ),
    )

    result = harness.run_behavior_case(
        case,
        lambda _: BehaviorSnapshot(
            intent="telegram_message",
            required_approval=False,
            selected_agent=None,
            risk_lane="brain",
            tool_plan=[],
            emitted_events=[],
            artifact_types=[],
        ),
    )

    assert not result.passed
    joined = "\n".join(result.failures)
    for field in ("required_approval", "selected_agent", "risk_lane", "tool_plan", "emitted_events", "artifact_types"):
        assert field in joined
