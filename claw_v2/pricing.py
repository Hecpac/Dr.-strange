"""Deterministic cost metering for API-billed LLM providers (2026-05-31 audit H5).

OpenAI/Google adapters previously hardcoded cost_estimate=0.0, leaving the
cost-per-hour breaker and the daily-cost gate structurally blind to the only two
API-billed providers. This module turns token usage into a USD estimate from a
versioned, offline price table. A model that is NOT in the table yields
unknown=True (never a silent 0): callers must surface/gate it so invisible
billable spend can never accumulate.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PRICES_PATH = Path(__file__).parent / "config" / "model_prices.json"


@dataclass(frozen=True, slots=True)
class CostEstimate:
    amount_usd: float
    unknown: bool
    provider: str
    model: str
    price_source: str | None = None
    price_as_of: str | None = None


def _load_prices() -> dict[str, Any]:
    try:
        return json.loads(_PRICES_PATH.read_text(encoding="utf-8"))
    except (
        OSError,
        ValueError,
    ) as exc:  # missing / malformed -> everything is unknown (fail-closed)
        logger.warning(
            "model_prices.json unreadable (%s); billable models will be cost_unknown", exc
        )
        return {}


_PRICES = _load_prices()


def _token_count(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):  # guard: bools are ints in Python
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


# OpenAI Responses API: input_tokens/output_tokens. Google usage_metadata:
# prompt_token_count/candidates_token_count. Legacy/chat fallbacks last.
_INPUT_KEYS = ("input_tokens", "prompt_token_count", "prompt_tokens")
_OUTPUT_KEYS = ("output_tokens", "candidates_token_count", "completion_tokens")
# Google bills thinking tokens at the output rate but reports them separately
# from candidates_token_count (the visible output). Add them so reasoning
# responses are not under-billed. OpenAI's output_tokens already includes
# reasoning tokens and OpenAI usage dicts don't carry this key, so this is
# additive only for Google.
_THINKING_KEYS = ("thoughts_token_count",)


def estimate_cost_usd(provider: str, model: str, usage: dict[str, Any] | None) -> CostEstimate:
    """Estimate USD cost for one billable LLM call from its token usage.

    Known model -> amount_usd (>0 when tokens are present), unknown=False, with
    the price source/as_of stamped. Unknown billable model -> amount_usd=0.0,
    unknown=True (the caller MUST treat this as unsafe, not as zero spend).
    """
    table = _PRICES.get("prices_per_1m_tokens", {})
    entry = table.get(f"{provider}:{model}")
    if entry is None:
        return CostEstimate(amount_usd=0.0, unknown=True, provider=provider, model=model)
    usage = usage or {}
    in_tokens = _token_count(usage, _INPUT_KEYS)
    out_tokens = _token_count(usage, _OUTPUT_KEYS) + _token_count(usage, _THINKING_KEYS)
    in_rate = float(entry["input"])
    out_rate = float(entry["output"])
    tier = entry.get("context_tier")
    if tier and in_tokens > int(tier.get("input_token_threshold", 0)):
        above = tier.get("above", {})
        in_rate = float(above.get("input", in_rate))
        out_rate = float(above.get("output", out_rate))
    amount = (in_tokens * in_rate + out_tokens * out_rate) / 1_000_000.0
    return CostEstimate(
        amount_usd=amount,
        unknown=False,
        provider=provider,
        model=model,
        price_source=(_PRICES.get("sources") or {}).get(provider),
        price_as_of=_PRICES.get("as_of"),
    )
