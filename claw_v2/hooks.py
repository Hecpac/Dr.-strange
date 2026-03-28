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
