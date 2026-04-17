from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from claw_v2.adapters.base import LLMRequest, ProviderAdapter
from claw_v2.agents import ExperimentRecord
from claw_v2.config import AppConfig
from claw_v2.llm import LLMRouter
from claw_v2.types import LLMResponse


class StaticAdapter(ProviderAdapter):
    def __init__(
        self,
        provider_name: str,
        *,
        tool_capable: bool,
        responder: Callable[[LLMRequest], LLMResponse],
    ) -> None:
        self.provider_name = provider_name
        self.tool_capable = tool_capable
        self._responder = responder

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self._responder(request)


def echo_response(provider_name: str) -> Callable[[LLMRequest], LLMResponse]:
    def responder(request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=f"{provider_name}:{request.lane}:{request.model}",
            lane=request.lane,
            provider=provider_name,
            model=request.model,
            confidence=0.75,
            cost_estimate=0.01,
        )

    return responder


def build_test_router(
    config: AppConfig,
    *,
    audit_sink: Callable[[dict], None] | None = None,
    pre_hooks: list | None = None,
    post_hooks: list | None = None,
) -> LLMRouter:
    return LLMRouter(
        config=config,
        adapters={
            "anthropic": StaticAdapter("anthropic", tool_capable=True, responder=echo_response("anthropic")),
            "openai": StaticAdapter("openai", tool_capable=False, responder=echo_response("openai")),
            "google": StaticAdapter("google", tool_capable=False, responder=echo_response("google")),
        },
        audit_sink=audit_sink,
        pre_hooks=pre_hooks,
        post_hooks=post_hooks,
    )


def scripted_experiment_runner(records: list[ExperimentRecord]) -> Callable[[str, int, dict], ExperimentRecord]:
    queue = deque(records)

    def runner(agent_name: str, experiment_number: int, state: dict) -> ExperimentRecord:
        if not queue:
            raise RuntimeError(f"no scripted experiments left for {agent_name}")
        return queue.popleft()

    return runner
