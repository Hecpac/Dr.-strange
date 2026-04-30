from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claw_v2.action_events import ProposedAction
from claw_v2.evidence_ledger import Claim
from claw_v2.gdi import GDISnapshot
from claw_v2.goal_contract import GoalContract
from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl

CRITIC_SCHEMA_VERSION = "critic_decision.v1"
CriticDecisionValue = Literal["approve", "revise", "block", "ask_human"]


@dataclass(slots=True)
class RiskAssessment:
    level: str
    factors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "factors": list(self.factors)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RiskAssessment":
        return cls(
            level=str(data.get("level") or "low"),
            factors=[str(item) for item in data.get("factors", [])],
        )


@dataclass(slots=True)
class CriticDecision:
    decision_id: str
    goal_id: str
    decision: CriticDecisionValue
    reason_summary: str
    goal_alignment: float
    required_fix: list[str] = field(default_factory=list)
    risk_assessment: RiskAssessment = field(default_factory=lambda: RiskAssessment(level="low"))
    evidence_gaps: list[str] = field(default_factory=list)
    decided_at: str = field(default_factory=now_iso)
    schema_version: str = CRITIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.decision not in {"approve", "revise", "block", "ask_human"}:
            raise ValueError(f"invalid critic decision: {self.decision}")
        if not 0.0 <= self.goal_alignment <= 1.0:
            raise ValueError("goal_alignment must be between 0.0 and 1.0")
        if self.decision == "revise" and not self.required_fix:
            raise ValueError("revise decisions require required_fix")
        if self.decision == "block" and not self.reason_summary.strip():
            raise ValueError("block decisions require a reason")
        if self.decision == "ask_human" and not self.required_fix:
            raise ValueError("ask_human decisions require a concrete question/fix")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "goal_id": self.goal_id,
            "decision": self.decision,
            "reason_summary": self.reason_summary[:480],
            "goal_alignment": round(float(self.goal_alignment), 4),
            "required_fix": list(self.required_fix),
            "risk_assessment": self.risk_assessment.to_dict(),
            "evidence_gaps": list(self.evidence_gaps),
            "decided_at": self.decided_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CriticDecision":
        return cls(
            decision_id=str(data["decision_id"]),
            goal_id=str(data["goal_id"]),
            decision=str(data["decision"]),  # type: ignore[arg-type]
            reason_summary=str(data.get("reason_summary") or ""),
            goal_alignment=float(data.get("goal_alignment") or 0.0),
            required_fix=[str(item) for item in data.get("required_fix", [])],
            risk_assessment=RiskAssessment.from_dict(data.get("risk_assessment") or {}),
            evidence_gaps=[str(item) for item in data.get("evidence_gaps", [])],
            decided_at=str(data.get("decided_at") or now_iso()),
            schema_version=str(data.get("schema_version") or CRITIC_SCHEMA_VERSION),
        )


def evaluate_critic_decision(
    goal: GoalContract,
    *,
    proposed_next_action: ProposedAction | dict[str, Any],
    evidence_ledger_subset: list[Claim] | None = None,
    risk_level: str = "low",
    gdi_snapshot: GDISnapshot | None = None,
    recall_results: list[dict[str, Any]] | None = None,
) -> CriticDecision:
    action = _coerce_action(proposed_next_action)
    claims = list(evidence_ledger_subset or [])
    factors: list[str] = []
    required_fix: list[str] = []
    evidence_gaps = _evidence_gaps(claims)

    alignment = _goal_alignment(goal, action, gdi_snapshot)
    if action.tool in set(goal.disallowed_actions):
        return _decision(
            goal,
            "block",
            "Proposed action is explicitly disallowed by the Goal Contract.",
            alignment,
            risk_level,
            ["Choose an allowed action or update the Goal Contract with human approval."],
            ["tool_in_disallowed_actions"],
            evidence_gaps,
        )

    if gdi_snapshot is not None and gdi_snapshot.band == "stop":
        return _decision(
            goal,
            "block",
            "GDI stop band indicates unacceptable drift.",
            min(alignment, 0.2),
            risk_level,
            ["Stop execution and ask the human to confirm the goal/action."],
            ["gdi_stop_band"],
            evidence_gaps,
        )

    if goal.allowed_actions and action.tool not in set(goal.allowed_actions):
        factors.append("tool_not_in_allowed_actions")
        required_fix.append("Use an action listed in the Goal Contract or update the contract first.")

    if action.tier == "tier_2_5":
        tier_fix = _tier_25_required_fix(action)
        if tier_fix:
            factors.append("tier_2_5_missing_guards")
            required_fix.extend(tier_fix)

    if gdi_snapshot is not None and gdi_snapshot.band == "critic_required":
        factors.append("gdi_critic_required")
        required_fix.append("Resolve or explicitly accept the GDI drift signals before proceeding.")

    if _recall_quality_failed(recall_results):
        factors.append("recall_quality_gate_failed")
        required_fix.append("Refresh Active Recall or proceed with explicit human confirmation.")

    if evidence_gaps and action.tier in {"tier_2", "tier_2_5"}:
        required_fix.append("Verify or explicitly mark evidence gaps before Tier-2 execution.")
        return _decision(
            goal,
            "revise",
            "Evidence gaps prevent safe Tier-2 approval.",
            min(alignment, 0.65),
            risk_level,
            required_fix,
            factors or ["evidence_gaps"],
            evidence_gaps,
        )

    if evidence_gaps and action.tier == "tier_3":
        return _decision(
            goal,
            "ask_human",
            "Tier-3 action has unresolved evidence gaps.",
            min(alignment, 0.6),
            risk_level,
            ["Ask the human to confirm the unresolved evidence gaps before proceeding."],
            factors or ["tier_3_evidence_gaps"],
            evidence_gaps,
        )

    if required_fix:
        return _decision(
            goal,
            "revise",
            "Proposed action needs revision before approval.",
            min(alignment, 0.7),
            risk_level,
            required_fix,
            factors,
            evidence_gaps,
        )

    return _decision(
        goal,
        "approve",
        "Proposed action aligns with the Goal Contract and available evidence.",
        alignment,
        risk_level,
        [],
        factors,
        evidence_gaps,
    )


def record_critic_decision(
    telemetry_root: Path | str,
    decision: CriticDecision,
    *,
    observe: Any | None = None,
) -> CriticDecision:
    payload = decision.to_dict()
    append_jsonl(_critic_path(telemetry_root), payload)
    if observe is not None:
        observe.emit("critic_decision_received", payload=payload)
    return decision


def load_critic_decisions(telemetry_root: Path | str) -> list[CriticDecision]:
    return [CriticDecision.from_dict(row) for row in read_jsonl(_critic_path(telemetry_root))]


def _critic_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "critic_decisions.jsonl"


def _coerce_action(value: ProposedAction | dict[str, Any]) -> ProposedAction:
    if isinstance(value, ProposedAction):
        return value
    return ProposedAction.from_dict(value)


def _evidence_gaps(claims: list[Claim]) -> list[str]:
    gaps: list[str] = []
    for claim in claims:
        if claim.claim_type in {"fact", "decision", "risk_signal"} and claim.verification_status != "verified":
            gaps.append(f"{claim.claim_id}:{claim.verification_status}")
    return gaps


def _goal_alignment(goal: GoalContract, action: ProposedAction, gdi_snapshot: GDISnapshot | None) -> float:
    alignment = 1.0
    if action.tool in set(goal.disallowed_actions):
        alignment = 0.0
    elif goal.allowed_actions and action.tool not in set(goal.allowed_actions):
        alignment = 0.65
    if gdi_snapshot is not None:
        alignment = min(alignment, max(0.0, 1.0 - gdi_snapshot.gdi_score))
    return round(alignment, 4)


def _tier_25_required_fix(action: ProposedAction) -> list[str]:
    args = action.args_redacted
    fixes: list[str] = []
    branch = str(args.get("branch") or args.get("ref") or "")
    if not branch:
        fixes.append("Declare the target branch for the Tier-2.5 push.")
    elif branch in {"main", "master", "prod", "production"}:
        fixes.append("Tier-2.5 push cannot target a protected branch.")
    if bool(args.get("force") or args.get("force_push")):
        fixes.append("Tier-2.5 push must not use force push.")
    if not bool(args.get("current_task_requested") or args.get("explicit_request")):
        fixes.append("Tier-2.5 push requires an explicit current-task request.")
    if not bool(args.get("verification_planned") or args.get("verify_after")):
        fixes.append("Tier-2.5 push requires a verification plan after push.")
    return fixes


def _recall_quality_failed(recall_results: list[dict[str, Any]] | None) -> bool:
    if not recall_results:
        return False
    for result in recall_results:
        gate = result.get("quality_gate") if isinstance(result, dict) else None
        if isinstance(gate, dict) and gate.get("passed") is False:
            return True
    return False


def _decision(
    goal: GoalContract,
    decision: CriticDecisionValue,
    reason: str,
    alignment: float,
    risk_level: str,
    required_fix: list[str],
    factors: list[str],
    evidence_gaps: list[str],
) -> CriticDecision:
    return CriticDecision(
        decision_id=generate_id("d"),
        goal_id=goal.goal_id,
        decision=decision,
        reason_summary=reason,
        goal_alignment=alignment,
        required_fix=required_fix,
        risk_assessment=RiskAssessment(level=risk_level, factors=factors),
        evidence_gaps=evidence_gaps,
        decided_at=now_iso(),
    )

