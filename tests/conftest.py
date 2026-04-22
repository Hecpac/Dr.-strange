from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from claw_v2.adapters.base import LLMRequest
from claw_v2.main import build_runtime
from claw_v2.types import LLMResponse


def deterministic_llm_response(request: LLMRequest) -> LLMResponse:
    content = "<response>handled</response>" if request.lane == "brain" else "handled"
    if request.lane == "verifier":
        content = (
            "<response>{"
            "\"recommendation\":\"approve\","
            "\"risk_level\":\"low\","
            "\"summary\":\"ok\","
            "\"reasons\":[\"tests passed\"],"
            "\"blockers\":[],"
            "\"missing_checks\":[],"
            "\"confidence\":0.9"
            "}</response>"
        )
    return LLMResponse(
        content=content,
        lane=request.lane,
        provider=request.provider,
        model=request.model,
        confidence=0.9,
        cost_estimate=0.01,
    )


@pytest.fixture
def runtime_factory(tmp_path: Path) -> Callable[..., object]:
    def _factory(anthropic_executor: Callable[[LLMRequest], LLMResponse] | None = None):
        root = tmp_path
        (root / "social_accounts").mkdir(parents=True, exist_ok=True)
        env = {
            "DB_PATH": str(root / "data" / "claw.db"),
            "WORKSPACE_ROOT": str(root / "workspace"),
            "AGENT_STATE_ROOT": str(root / "agents"),
            "EVAL_ARTIFACTS_ROOT": str(root / "evals"),
            "APPROVALS_ROOT": str(root / "approvals"),
            "TELEGRAM_ALLOWED_USER_ID": "123",
            "BRAIN_PROVIDER": "anthropic",
            "WORKER_PROVIDER": "anthropic",
            "VERIFIER_PROVIDER": "openai",
            "RESEARCH_PROVIDER": "google",
            "JUDGE_PROVIDER": "openai",
            "SOCIAL_ACCOUNTS_ROOT": str(root / "social_accounts"),
        }
        with patch.dict(os.environ, env, clear=False):
            return build_runtime(anthropic_executor=anthropic_executor or deterministic_llm_response)

    return _factory
