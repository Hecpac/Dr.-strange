"""QTS agent roles — each agent is a specialized LLM feature extractor.

Architecture: agents produce SIGNALS (floats + metadata), never trade directly.
A traditional model (or rule engine) consumes signals and executes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from claw_v2.llm import LLMRouter

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentSignal:
    agent: str
    signal_type: str
    value: float  # -1.0 (strong sell) to 1.0 (strong buy)
    confidence: float  # 0.0 to 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


class _BaseAgent:
    name: str
    role_prompt: str

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    def extract(self, context: dict[str, Any]) -> AgentSignal:
        formatted = self._format_context(context)
        resp = self.router.ask(
            formatted,
            system_prompt=self.role_prompt,
            lane="research",
            evidence_pack={"evidence": formatted},
        )
        return self._parse_signal(resp.content)

    def _format_context(self, context: dict[str, Any]) -> str:
        parts = []
        for key, val in context.items():
            parts.append(f"<{key}>{val}</{key}>")
        return "\n".join(parts)

    def _parse_signal(self, content: str) -> AgentSignal:
        from claw_v2.brain_json import _try_parse_json_object
        parsed = _try_parse_json_object(content)
        if parsed is None:
            return AgentSignal(agent=self.name, signal_type="error", value=0.0, confidence=0.0)
        return AgentSignal(
            agent=self.name,
            signal_type=parsed.get("signal_type", "unknown"),
            value=max(-1.0, min(1.0, float(parsed.get("value", 0.0)))),
            confidence=max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
            metadata=parsed.get("metadata", {}),
        )


SIGNAL_JSON_SCHEMA = """\
Return JSON only: {"signal_type": "...", "value": -1.0 to 1.0, "confidence": 0.0 to 1.0, "metadata": {...}}
value: -1.0 = strong sell, 0.0 = neutral, 1.0 = strong buy.
confidence: how certain you are in this signal."""


class ResearchAgent(_BaseAgent):
    """Gathers and synthesizes market data, news, social sentiment."""
    name = "researcher"
    role_prompt = f"""\
You are a market research agent. Analyze the provided data sources (news, social media, market data) and extract a directional signal.
Focus on: event catalysts, sentiment shifts, information asymmetry.
{SIGNAL_JSON_SCHEMA}"""


class AnalystAgent(_BaseAgent):
    """Analyzes technical and fundamental data for regime detection."""
    name = "analyst"
    role_prompt = f"""\
You are a quantitative analyst agent. Analyze price action, volatility regime, and technical indicators.
Classify the current regime (trending/mean-reverting/choppy) and provide a directional signal.
Only emit strong signals when regime is favorable (Hurst > 0.5 for trend, < 0.5 for mean-reversion).
{SIGNAL_JSON_SCHEMA}
Include "regime" in metadata: "trending", "mean_reverting", or "choppy"."""


class RiskAgent(_BaseAgent):
    """Evaluates risk factors and position sizing constraints."""
    name = "risk"
    role_prompt = f"""\
You are a risk management agent. Evaluate:
- Current drawdown vs max allowed
- Correlation with existing positions
- Tail risk indicators (VIX equivalent, funding rates)
- Position sizing recommendation (0.0 = no position, 1.0 = max size)
{SIGNAL_JSON_SCHEMA}
value = recommended position size multiplier (0.0 to 1.0). Include "max_loss_pct" in metadata."""


class ExecutorAgent(_BaseAgent):
    """Determines optimal execution timing and order type. Does NOT place orders."""
    name = "executor"
    role_prompt = f"""\
You are an execution timing agent. Given aggregated signals and market microstructure data:
- Recommend entry timing (immediate, wait for pullback, scale in)
- Recommend order type (market, limit at level, TWAP)
- Flag if spread or liquidity makes execution unfavorable
{SIGNAL_JSON_SCHEMA}
value = urgency (-1.0 = wait/cancel, 1.0 = execute now). Include "order_type" and "entry_price" in metadata."""
