"""LLM Feature Extractor — converts unstructured data into numeric feature vectors.

Pattern from PeerJ LLM+DRL hybrid: LLMs extract semantic features,
traditional models (PPO/rules) consume them for execution decisions.
This eliminates hallucination risk in the execution path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from claw_v2.llm import LLMRouter

logger = logging.getLogger(__name__)

FEATURE_PROMPT = """\
Extract numeric features from the provided market context.
Return JSON only with this exact shape:
{
  "sentiment": -1.0 to 1.0,
  "regime": "trending" | "mean_reverting" | "choppy",
  "regime_confidence": 0.0 to 1.0,
  "event_impact": -1.0 to 1.0,
  "volatility_regime": "low" | "normal" | "high" | "extreme",
  "key_factors": ["factor1", "factor2"]
}
Rules:
- Base sentiment on actual evidence, not speculation.
- regime_confidence below 0.5 means the regime is unclear — downstream should reduce position size.
- event_impact captures scheduled or breaking events (earnings, elections, policy changes)."""


@dataclass(slots=True)
class FeatureVector:
    sentiment: float = 0.0
    regime: str = "choppy"
    regime_confidence: float = 0.0
    event_impact: float = 0.0
    volatility_regime: str = "normal"
    key_factors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sentiment": self.sentiment,
            "regime": self.regime,
            "regime_confidence": self.regime_confidence,
            "event_impact": self.event_impact,
            "volatility_regime": self.volatility_regime,
            "key_factors": self.key_factors,
        }

    @property
    def regime_gate_open(self) -> bool:
        """Regime-Adaptive Sentiment gate: only trade when regime is clear."""
        return self.regime_confidence >= 0.5 and self.regime != "choppy"


@dataclass(slots=True)
class LLMFeatureExtractor:
    router: LLMRouter

    def extract(self, context: str) -> FeatureVector:
        resp = self.router.ask(
            context,
            system_prompt=FEATURE_PROMPT,
            lane="research",
            evidence_pack={"evidence": context},
        )
        return self._parse(resp.content)

    def _parse(self, content: str) -> FeatureVector:
        from claw_v2.brain_json import _try_parse_json_object
        parsed = _try_parse_json_object(content)
        if parsed is None:
            logger.warning("Feature extraction failed to parse LLM response")
            return FeatureVector()
        return FeatureVector(
            sentiment=max(-1.0, min(1.0, float(parsed.get("sentiment", 0.0)))),
            regime=parsed.get("regime", "choppy") if parsed.get("regime") in {"trending", "mean_reverting", "choppy"} else "choppy",
            regime_confidence=max(0.0, min(1.0, float(parsed.get("regime_confidence", 0.0)))),
            event_impact=max(-1.0, min(1.0, float(parsed.get("event_impact", 0.0)))),
            volatility_regime=parsed.get("volatility_regime", "normal") if parsed.get("volatility_regime") in {"low", "normal", "high", "extreme"} else "normal",
            key_factors=parsed.get("key_factors", [])[:5],
        )
