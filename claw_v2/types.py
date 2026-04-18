from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Lane = Literal["brain", "worker", "verifier", "research", "judge"]
ProviderName = Literal["anthropic", "openai", "google", "ollama", "codex"]
AgentClass = Literal["researcher", "operator", "deployer"]
SanitizerVerdict = Literal["clean", "malicious", "unsure"]
VerificationRecommendation = Literal["approve", "needs_approval", "deny"]
RiskLevel = Literal["low", "medium", "high", "critical"]
CriticalActionStatus = Literal["executed", "executed_autonomously", "executed_with_approval", "awaiting_approval", "blocked"]


@dataclass(slots=True)
class EvidencePack:
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LLMResponse:
    content: str
    lane: Lane
    provider: str
    model: str
    confidence: float = 0.0
    cost_estimate: float = 0.0
    artifacts: dict[str, Any] = field(default_factory=dict)
    degraded_mode: bool = False


@dataclass(slots=True)
class SanitizedContent:
    verdict: SanitizerVerdict
    content: str
    source: str
    target_agent_class: AgentClass
    reason: str | None = None
    structured_data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SandboxDecision:
    allowed: bool
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CriticalActionVerification:
    recommendation: VerificationRecommendation
    risk_level: RiskLevel
    summary: str
    reasons: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    missing_checks: list[str] = field(default_factory=list)
    confidence: float = 0.0
    requires_human_approval: bool = False
    should_proceed: bool = False
    approval_id: str | None = None
    approval_token: str | None = None
    response: LLMResponse | None = None
    verifier_votes: list[dict[str, Any]] = field(default_factory=list)
    consensus_status: str = "single"


@dataclass(slots=True)
class CriticalActionExecution:
    action: str
    status: CriticalActionStatus
    executed: bool
    verification: CriticalActionVerification
    result: Any = None
    reason: str | None = None
    approval_status: str | None = None
