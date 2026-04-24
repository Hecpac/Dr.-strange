"""QTS — Quantitative Trading System with multi-agent DAG architecture.

Pattern: LLMs extract features (sentiment, regime), traditional models execute.
Based on AgenticTrading (NeurIPS) + LLM-DRL Hybrid (PeerJ) research.
"""

from claw_v2.qts.dag import DAGPlanner, DAGNode
from claw_v2.qts.agents import ResearchAgent, AnalystAgent, RiskAgent, ExecutorAgent
from claw_v2.qts.features import LLMFeatureExtractor
from claw_v2.qts.paper import PaperTrader

__all__ = [
    "DAGPlanner",
    "DAGNode",
    "ResearchAgent",
    "AnalystAgent",
    "RiskAgent",
    "ExecutorAgent",
    "LLMFeatureExtractor",
    "PaperTrader",
]
