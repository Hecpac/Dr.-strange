"""DAG Planner — orchestrates QTS agents in a dependency graph.

Based on AgenticTrading (NeurIPS): DAG Planner + agent pools.
Layer 1 runs in parallel via threads, layers 2-4 run sequentially.

DAG structure:
  [researcher] ──┐
                  ├──> [analyst] ──> [risk] ──> [executor]
  [features]  ───┘

The executor agent recommends timing/type but does NOT place orders.
Order placement is handled by a traditional execution engine.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from claw_v2.qts.agents import (
    AgentSignal,
    AnalystAgent,
    ExecutorAgent,
    ResearchAgent,
    RiskAgent,
    _BaseAgent,
)
from claw_v2.qts.features import FeatureVector, LLMFeatureExtractor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DAGNode:
    name: str
    agent: _BaseAgent | LLMFeatureExtractor
    depends_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DAGResult:
    signals: dict[str, AgentSignal]
    features: FeatureVector | None
    aggregated_signal: float
    should_trade: bool
    position_size: float
    elapsed_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)


class DAGPlanner:
    def __init__(self, *, researcher: ResearchAgent, analyst: AnalystAgent,
                 risk: RiskAgent, executor: ExecutorAgent,
                 feature_extractor: LLMFeatureExtractor) -> None:
        self.researcher = researcher
        self.analyst = analyst
        self.risk = risk
        self.executor = executor
        self.feature_extractor = feature_extractor

    def run(self, context: dict[str, Any]) -> DAGResult:
        t0 = time.monotonic()
        signals: dict[str, AgentSignal] = {}

        # Layer 1: parallel — researcher + feature extractor
        with ThreadPoolExecutor(max_workers=2) as pool:
            research_future = pool.submit(self.researcher.extract, context)
            features_future = pool.submit(
                self.feature_extractor.extract, context.get("market_data", "")
            )
            research_signal = research_future.result()
            features = features_future.result()
        signals["researcher"] = research_signal

        # Regime gate: if regime unclear, skip downstream agents
        if not features.regime_gate_open:
            elapsed = (time.monotonic() - t0) * 1000
            logger.info("Regime gate closed (confidence=%.2f, regime=%s)",
                        features.regime_confidence, features.regime)
            return DAGResult(
                signals=signals, features=features,
                aggregated_signal=0.0, should_trade=False, position_size=0.0,
                elapsed_ms=elapsed,
                metadata={"gate": "regime_closed"},
            )

        # Layer 2: analyst (consumes researcher output + features)
        analyst_context = {
            **context,
            "researcher_signal": f"{research_signal.value:.2f} (conf={research_signal.confidence:.2f})",
            "regime": features.regime,
            "sentiment": f"{features.sentiment:.2f}",
        }
        signals["analyst"] = self.analyst.extract(analyst_context)

        # Layer 3: risk (consumes all prior signals)
        risk_context = {
            **context,
            "researcher_signal": f"{signals['researcher'].value:.2f}",
            "analyst_signal": f"{signals['analyst'].value:.2f}",
            "regime": features.regime,
            "volatility": features.volatility_regime,
        }
        signals["risk"] = self.risk.extract(risk_context)

        # Position size gate: risk agent can veto
        position_size = max(0.0, min(1.0, signals["risk"].value))
        if position_size < 0.05:
            elapsed = (time.monotonic() - t0) * 1000
            return DAGResult(
                signals=signals, features=features,
                aggregated_signal=0.0, should_trade=False, position_size=0.0,
                elapsed_ms=elapsed,
                metadata={"gate": "risk_veto"},
            )

        # Layer 4: executor timing (consumes everything)
        exec_context = {
            **context,
            "aggregated_direction": f"{signals['analyst'].value:.2f}",
            "position_size": f"{position_size:.2f}",
            "regime": features.regime,
        }
        signals["executor"] = self.executor.extract(exec_context)

        # Aggregate: analyst direction * risk sizing * executor urgency
        direction = signals["analyst"].value
        urgency = max(0.0, signals["executor"].value)
        aggregated = direction * position_size * urgency

        elapsed = (time.monotonic() - t0) * 1000
        return DAGResult(
            signals=signals,
            features=features,
            aggregated_signal=aggregated,
            should_trade=abs(aggregated) > 0.1,
            position_size=position_size * urgency,
            elapsed_ms=elapsed,
            metadata={
                "direction": "long" if direction > 0 else "short",
                "order_type": signals["executor"].metadata.get("order_type", "market"),
            },
        )
