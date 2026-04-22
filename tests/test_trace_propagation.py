from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from claw_v2.adapters.base import LLMRequest
from claw_v2.coordinator import WorkerTask
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def _response(request: LLMRequest) -> LLMResponse:
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
            anthropic_executor=_response,
            openai_transport=_response,
            google_transport=_response,
            codex_transport=_response,
        )


def _cursor(observe) -> int:
    return int(observe._conn.execute("SELECT COALESCE(MAX(id), 0) FROM observe_stream").fetchone()[0])


def _events(observe, cursor: int) -> list[dict]:
    rows = observe._conn.execute(
        """
        SELECT event_type, trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id, payload
        FROM observe_stream
        WHERE id > ?
        ORDER BY id ASC
        """,
        (cursor,),
    ).fetchall()
    return [
        {
            "event_type": row[0],
            "trace_id": row[1],
            "root_trace_id": row[2],
            "span_id": row[3],
            "parent_span_id": row[4],
            "job_id": row[5],
            "artifact_id": row[6],
            "payload": json.loads(row[7]),
        }
        for row in rows
    ]


def test_brain_turn_preserves_single_trace_across_llm_and_completion(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    cursor = _cursor(runtime.observe)

    runtime.bot.handle_text(user_id="123", session_id="trace-brain", text="hola")

    events = [
        event
        for event in _events(runtime.observe, cursor)
        if event["event_type"] in {"brain_turn_start", "llm_decision", "llm_response", "brain_turn_complete"}
    ]
    event_types = [event["event_type"] for event in events]
    trace_ids = {event["trace_id"] for event in events}

    assert event_types == ["brain_turn_start", "llm_decision", "llm_response", "brain_turn_complete"]
    assert len(trace_ids) == 1
    assert None not in trace_ids
    assert all(event["root_trace_id"] == next(iter(trace_ids)) for event in events)
    assert all(event["artifact_id"] == "trace-brain" for event in events)


def test_coordinator_preserves_parent_trace_across_worker_and_synthesis(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    cursor = _cursor(runtime.observe)

    runtime.coordinator.run(
        task_id="coord-trace",
        objective="Investigate traces",
        research_tasks=[WorkerTask(name="trace-worker", instruction="inspect", lane="research")],
    )

    events = [
        event
        for event in _events(runtime.observe, cursor)
        if event["event_type"] in {"coordinator_start", "llm_decision", "llm_response", "coordinator_complete"}
    ]
    trace_ids = {event["trace_id"] for event in events}
    span_ids = {event["span_id"] for event in events if event["span_id"]}

    assert {"coordinator_start", "llm_decision", "llm_response", "coordinator_complete"} <= {
        event["event_type"] for event in events
    }
    assert len(trace_ids) == 1
    assert None not in trace_ids
    assert len(span_ids) >= 3
    assert all(event["job_id"] == "coord-trace" for event in events)
