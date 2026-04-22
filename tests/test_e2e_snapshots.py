from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from claw_v2.adapters.base import LLMRequest
from claw_v2.bot_commands import CommandContext
from claw_v2.coordinator import WorkerTask
from claw_v2.linear import LinearIssue
from claw_v2.main import build_runtime
from claw_v2.pipeline import PipelineService
from claw_v2.types import LLMResponse


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "e2e_command_snapshots.json"


@dataclass(frozen=True)
class BehaviorSnapshot:
    intent: str
    required_approval: bool
    selected_agent: str | None
    risk_lane: str
    tool_plan: list[str]
    emitted_events: list[str]
    artifact_types: list[str]


def _load_fixtures() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _expected_case(name: str) -> BehaviorSnapshot:
    for case in _load_fixtures()["behavior_cases"]:
        if case["name"] == name:
            return BehaviorSnapshot(
                intent=case["intent"],
                required_approval=case["required_approval"],
                selected_agent=case["selected_agent"],
                risk_lane=case["risk_lane"],
                tool_plan=case["tool_plan"],
                emitted_events=case["emitted_events"],
                artifact_types=case["artifact_types"],
            )
    raise AssertionError(f"missing behavior fixture: {name}")


def _event_cursor(observe: Any) -> int:
    row = observe._conn.execute("SELECT COALESCE(MAX(id), 0) FROM observe_stream").fetchone()
    return int(row[0])


def _events_since(observe: Any, cursor: int) -> list[dict[str, Any]]:
    rows = observe._conn.execute(
        """
        SELECT event_type, lane, provider, model, payload
        FROM observe_stream
        WHERE id > ?
        ORDER BY id ASC
        """,
        (cursor,),
    ).fetchall()
    return [
        {
            "event_type": row[0],
            "lane": row[1],
            "provider": row[2],
            "model": row[3],
            "payload": json.loads(row[4]),
        }
        for row in rows
    ]


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    return [event["event_type"] for event in events]


def _assert_snapshot(actual: BehaviorSnapshot, expected: BehaviorSnapshot) -> None:
    assert asdict(actual) == asdict(expected)


def _normal_response(request: LLMRequest) -> LLMResponse:
    content = "<response>handled</response>" if request.lane == "brain" else "handled"
    return LLMResponse(
        content=content,
        lane=request.lane,
        provider=request.provider,
        model=request.model,
        confidence=0.9,
        cost_estimate=0.01,
    )


def _blocking_verifier_response(request: LLMRequest) -> LLMResponse:
    if request.lane == "verifier":
        content = (
            "<response>{"
            "\"recommendation\":\"needs_approval\","
            "\"risk_level\":\"high\","
            "\"summary\":\"human review required\","
            "\"reasons\":[\"touches critical path\"],"
            "\"blockers\":[],"
            "\"missing_checks\":[],"
            "\"confidence\":0.86"
            "}</response>"
        )
    elif request.lane == "brain":
        content = "<response>handled</response>"
    else:
        content = "handled"
    return LLMResponse(
        content=content,
        lane=request.lane,
        provider=request.provider,
        model=request.model,
        confidence=0.9,
        cost_estimate=0.01,
    )


def _runtime(tmp_path: Path, *, executor=_normal_response):
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
            anthropic_executor=executor,
            openai_transport=executor,
            google_transport=executor,
            codex_transport=executor,
        )


def _assert_contains_all(actual: list[str], expected: list[str]) -> None:
    for item in expected:
        assert item in actual


def test_command_snapshot_fixtures_cover_current_command_inventory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    fixtures = _load_fixtures()["commands"]
    fixture_keys = {(item["phase"], item["name"]) for item in fixtures}
    inventory_keys = {
        (phase, command.name)
        for phase, commands in (
            ("pre", runtime.bot._pre_state_commands),
            ("post", runtime.bot._post_shortcut_commands),
        )
        for command in commands
    }

    assert fixture_keys == inventory_keys
    assert len(fixture_keys) == 36


def test_command_snapshot_fixtures_dispatch_without_internal_mocks(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    fixtures = _load_fixtures()["commands"]

    with patch("claw_v2.bot.Path.home", return_value=tmp_path), patch("threading.Timer") as timer:
        timer.return_value.start.return_value = None
        for item in fixtures:
            command_list = (
                runtime.bot._pre_state_commands
                if item["phase"] == "pre"
                else runtime.bot._post_shortcut_commands
            )
            context = CommandContext(
                user_id="123",
                session_id=f"snapshot-{item['phase']}-{item['name']}",
                text=item["text"],
                stripped=item["text"],
            )
            assert any(command.name == item["name"] and command.matches(item["text"]) for command in command_list)

            reply = runtime.bot.handle_text(user_id="123", session_id=context.session_id, text=item["text"])
            assert isinstance(reply, str)
            assert reply.strip(), item


def test_simple_message_semantic_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    cursor = _event_cursor(runtime.observe)

    reply = runtime.bot.handle_text(user_id="123", session_id="simple-message", text="hola")

    assert reply == "handled"
    events = _events_since(runtime.observe, cursor)
    actual = BehaviorSnapshot(
        intent="telegram_message",
        required_approval=False,
        selected_agent=None,
        risk_lane="brain",
        tool_plan=[],
        emitted_events=[event for event in _expected_case("simple-message").emitted_events if event in _event_types(events)],
        artifact_types=[],
    )
    _assert_snapshot(actual, _expected_case("simple-message"))


def test_approval_flow_semantic_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, executor=_blocking_verifier_response)
    cursor = _event_cursor(runtime.observe)
    executed: list[str] = []

    pending = runtime.brain.execute_critical_action(
        action="deploy_prod",
        plan="Deploy production",
        diff="diff --git a/app.py b/app.py",
        test_output="not run",
        executor=lambda: executed.append("ran"),
    )
    assert pending.status == "awaiting_approval"
    assert pending.verification.approval_id is not None
    assert pending.verification.approval_token is not None
    assert not executed

    assert runtime.approvals.approve(
        pending.verification.approval_id,
        pending.verification.approval_token,
    )
    completed = runtime.brain.execute_critical_action(
        action="deploy_prod",
        plan="Deploy production",
        diff="diff --git a/app.py b/app.py",
        test_output="not run",
        approval_id=pending.verification.approval_id,
        executor=lambda: executed.append("ran"),
    )

    assert completed.status == "executed_with_approval"
    assert executed == ["ran"]
    events = _events_since(runtime.observe, cursor)
    actual = BehaviorSnapshot(
        intent="critical_action",
        required_approval=True,
        selected_agent=None,
        risk_lane="verifier",
        tool_plan=["verify", "approval"],
        emitted_events=[
            event
            for event in _expected_case("approval-required-critical-action").emitted_events
            if event in _event_types(events)
        ],
        artifact_types=["approval_id", "verifier_votes"],
    )
    _assert_snapshot(actual, _expected_case("approval-required-critical-action"))


def test_pipeline_semantic_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    linear = MagicMock()
    linear.get_issue.return_value = LinearIssue(
        id="HEC-1",
        title="Fix login",
        description="Make login reliable",
        state="Todo",
        labels=["claw-auto"],
        branch_name="hec-1-login",
        url="https://linear.example/HEC-1",
    )
    pull_requests = MagicMock()
    service = PipelineService(
        linear=linear,
        router=runtime.router,
        approvals=runtime.approvals,
        pull_requests=pull_requests,
        observe=runtime.observe,
        default_repo_root=tmp_path,
        state_root=tmp_path / "pipeline",
        memory=runtime.memory,
        learning=runtime.brain.learning,
    )
    cursor = _event_cursor(runtime.observe)

    with patch("claw_v2.pipeline._create_branch"), patch(
        "claw_v2.pipeline._create_worktree", return_value=tmp_path / "wt"
    ), patch("claw_v2.pipeline._collect_diff", return_value="diff content"), patch(
        "claw_v2.pipeline._run_tests", return_value=(True, "5 passed")
    ), patch("claw_v2.pipeline._remove_worktree"):
        run = service.process_issue("HEC-1")

    assert run.status == "awaiting_approval"
    assert run.approval_id is not None
    events = _events_since(runtime.observe, cursor)
    actual = BehaviorSnapshot(
        intent="pipeline",
        required_approval=True,
        selected_agent=None,
        risk_lane="worker",
        tool_plan=["linear", "worker", "tests", "approval"],
        emitted_events=[
            event for event in _expected_case("pipeline-awaiting-approval").emitted_events if event in _event_types(events)
        ],
        artifact_types=["approval_id", "diff", "test_output"],
    )
    _assert_snapshot(actual, _expected_case("pipeline-awaiting-approval"))


def test_coordinator_agent_delegation_semantic_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    cursor = _event_cursor(runtime.observe)

    result = runtime.coordinator.run(
        task_id="coord-1",
        objective="Investigate login",
        research_tasks=[
            WorkerTask(
                name="research-login",
                instruction="Find login failure causes",
                lane="research",
                assigned_agent="hex",
            )
        ],
    )

    assert result.error == ""
    assert "research" in result.phase_results
    events = _events_since(runtime.observe, cursor)
    actual = BehaviorSnapshot(
        intent="coordinator",
        required_approval=False,
        selected_agent="hex",
        risk_lane="research",
        tool_plan=["research", "synthesis"],
        emitted_events=[
            event
            for event in _expected_case("coordinator-agent-delegation").emitted_events
            if event in _event_types(events)
        ],
        artifact_types=["assigned_agent"],
    )
    _assert_snapshot(actual, _expected_case("coordinator-agent-delegation"))


def test_error_path_graceful_degradation_semantic_snapshot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.bot.set_capability_status("chrome_cdp", available=False, reason="Chrome disabled in test.")
    cursor = _event_cursor(runtime.observe)

    reply = runtime.bot.handle_text(user_id="123", session_id="error-path", text="/chrome_pages")

    assert "Chrome disabled in test" in reply
    events = _events_since(runtime.observe, cursor)
    actual = BehaviorSnapshot(
        intent="bot_command:chrome",
        required_approval=False,
        selected_agent=None,
        risk_lane="none",
        tool_plan=[],
        emitted_events=_event_types(events),
        artifact_types=["degraded_capability"],
    )
    _assert_snapshot(actual, _expected_case("error-path-graceful-degradation"))
