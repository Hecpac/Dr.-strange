from __future__ import annotations

import logging

from claw_v2.adapters.base import LLMRequest, PreLLMHook, PostLLMHook
from claw_v2.observe import ObserveStream
from claw_v2.types import LLMResponse

logger = logging.getLogger(__name__)


def make_daily_cost_gate(observe: ObserveStream, daily_limit: float = 10.0) -> PreLLMHook:
    def daily_cost_gate(request: LLMRequest) -> LLMRequest | None:
        total_today = observe.total_cost_today()
        if total_today >= daily_limit:
            logger.warning("Daily cost limit reached: $%.2f >= $%.2f", total_today, daily_limit)
            return None
        return request

    daily_cost_gate.__name__ = "daily_cost_gate"
    return daily_cost_gate


def make_decision_logger(observe: ObserveStream) -> PostLLMHook:
    def decision_logger(request: LLMRequest, response: LLMResponse) -> LLMResponse:
        observe.emit(
            "llm_decision",
            lane=response.lane,
            provider=response.provider,
            model=response.model,
            payload={
                "session_id": request.session_id,
                "confidence": response.confidence,
                "cost_estimate": response.cost_estimate,
                "degraded_mode": response.degraded_mode,
                "prompt_length": len(request.prompt) if isinstance(request.prompt, str) else len(request.prompt),
                "response_length": len(response.content),
                "effort": request.effort,
                "has_evidence_pack": request.evidence_pack is not None,
            },
        )
        return response

    decision_logger.__name__ = "decision_logger"
    return decision_logger
