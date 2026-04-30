from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claw_v2.evidence_ledger import Claim, VERIFICATION_EVIDENCE_KINDS
from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl

FAR_SCHEMA_VERSION = "far_assessment.v1"
Severity = Literal["low", "medium", "high", "critical"]
RecommendedDecision = Literal["continue", "revise", "ask_human", "block"]


@dataclass(slots=True)
class DoubtFlag:
    flag: str
    severity: Severity
    reason: str
    required_resolution: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "flag": self.flag,
            "severity": self.severity,
            "reason": self.reason[:240],
            "required_resolution": self.required_resolution[:240],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DoubtFlag":
        return cls(
            flag=str(data["flag"]),
            severity=str(data.get("severity") or "low"),  # type: ignore[arg-type]
            reason=str(data.get("reason") or ""),
            required_resolution=str(data.get("required_resolution") or ""),
        )


@dataclass(slots=True)
class ConfidenceBasis:
    verified_claims: int = 0
    unverified_claims: int = 0
    tool_evidence_refs: int = 0
    freshness: str = "fresh"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified_claims": self.verified_claims,
            "unverified_claims": self.unverified_claims,
            "tool_evidence_refs": self.tool_evidence_refs,
            "freshness": self.freshness,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConfidenceBasis":
        return cls(
            verified_claims=int(data.get("verified_claims") or 0),
            unverified_claims=int(data.get("unverified_claims") or 0),
            tool_evidence_refs=int(data.get("tool_evidence_refs") or 0),
            freshness=str(data.get("freshness") or "fresh"),
        )


@dataclass(slots=True)
class FaRAssessment:
    assessment_id: str
    goal_id: str
    claim_ids: list[str]
    confidence: float
    confidence_basis: ConfidenceBasis
    doubt_flags: list[DoubtFlag] = field(default_factory=list)
    recommended_decision: RecommendedDecision = "continue"
    assessed_at: str = field(default_factory=now_iso)
    schema_version: str = FAR_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "assessment_id": self.assessment_id,
            "goal_id": self.goal_id,
            "claim_ids": list(self.claim_ids),
            "confidence": round(float(self.confidence), 4),
            "confidence_basis": self.confidence_basis.to_dict(),
            "doubt_flags": [flag.to_dict() for flag in self.doubt_flags],
            "recommended_decision": self.recommended_decision,
            "assessed_at": self.assessed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FaRAssessment":
        return cls(
            assessment_id=str(data["assessment_id"]),
            goal_id=str(data["goal_id"]),
            claim_ids=[str(item) for item in data.get("claim_ids", [])],
            confidence=float(data.get("confidence") or 0.0),
            confidence_basis=ConfidenceBasis.from_dict(data.get("confidence_basis") or {}),
            doubt_flags=[DoubtFlag.from_dict(item) for item in data.get("doubt_flags", [])],
            recommended_decision=str(data.get("recommended_decision") or "continue"),  # type: ignore[arg-type]
            assessed_at=str(data.get("assessed_at") or now_iso()),
            schema_version=str(data.get("schema_version") or FAR_SCHEMA_VERSION),
        )


def assess_far(
    *,
    goal_id: str,
    claims: list[Claim],
    action_tier: str = "tier_1",
    external_state_verified: bool = False,
    user_confirmation_present: bool = False,
) -> FaRAssessment:
    flags: list[DoubtFlag] = []
    verified_claims = sum(1 for claim in claims if claim.verification_status == "verified")
    unverified_claims = sum(1 for claim in claims if claim.verification_status != "verified")
    tool_evidence_refs = sum(
        1
        for claim in claims
        for ref in claim.evidence_refs
        if ref.kind in VERIFICATION_EVIDENCE_KINDS
    )

    for claim in claims:
        if claim.claim_type == "fact" and not any(ref.kind in VERIFICATION_EVIDENCE_KINDS for ref in claim.evidence_refs):
            flags.append(_flag(
                "missing_tool_evidence",
                "medium",
                f"Claim {claim.claim_id} is factual without tool evidence.",
                "Attach tool_call, file_read, or external_api evidence.",
            ))
        if claim.verification_status == "stale":
            flags.append(_flag(
                "stale_evidence",
                "medium",
                f"Claim {claim.claim_id} is stale.",
                "Refresh the evidence before relying on this claim.",
            ))
        if claim.verification_status == "contradicted":
            flags.append(_flag(
                "conflicting_claims",
                "high",
                f"Claim {claim.claim_id} is contradicted.",
                "Resolve the contradiction or downgrade the decision.",
            ))
        if claim.claim_type == "assumption":
            flags.append(_flag(
                "assumption_required",
                "low",
                f"Claim {claim.claim_id} is an assumption.",
                "Confirm the assumption or keep it out of factual confidence.",
            ))

    if action_tier in {"tier_2_5", "tier_3"} and not external_state_verified:
        flags.append(_flag(
            "external_state_unknown",
            "high" if action_tier == "tier_3" else "medium",
            "Sensitive action lacks fresh external-state verification.",
            "Verify external state with a tool before proceeding.",
        ))
    if not tool_evidence_refs and claims:
        flags.append(_flag(
            "low_observability",
            "medium",
            "Claims do not leave enough tool-grounded evidence.",
            "Capture verifiable evidence or reduce confidence.",
        ))
    if action_tier == "tier_3" and not user_confirmation_present:
        flags.append(_flag(
            "user_confirmation_needed",
            "critical",
            "Tier-3 action requires explicit human confirmation.",
            "Ask the human before proceeding.",
        ))

    basis = ConfidenceBasis(
        verified_claims=verified_claims,
        unverified_claims=unverified_claims,
        tool_evidence_refs=tool_evidence_refs,
        freshness=_freshness(claims),
    )
    confidence = _confidence(basis, flags, total_claims=len(claims))
    return FaRAssessment(
        assessment_id=generate_id("far"),
        goal_id=goal_id,
        claim_ids=[claim.claim_id for claim in claims],
        confidence=confidence,
        confidence_basis=basis,
        doubt_flags=flags,
        recommended_decision=_recommended_decision(flags),
        assessed_at=now_iso(),
    )


def record_far_assessment(
    telemetry_root: Path | str,
    assessment: FaRAssessment,
    *,
    observe: Any | None = None,
) -> FaRAssessment:
    payload = assessment.to_dict()
    append_jsonl(_far_path(telemetry_root), payload)
    if observe is not None:
        observe.emit("far_assessment", payload=payload)
        if any(flag.severity == "critical" for flag in assessment.doubt_flags):
            observe.emit("risk_escalated", payload={
                "goal_id": assessment.goal_id,
                "assessment_id": assessment.assessment_id,
                "reason": "critical_far_doubt_flag",
            })
    return assessment


def load_far_assessments(telemetry_root: Path | str) -> list[FaRAssessment]:
    return [FaRAssessment.from_dict(row) for row in read_jsonl(_far_path(telemetry_root))]


def _far_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "far.jsonl"


def _flag(flag: str, severity: Severity, reason: str, resolution: str) -> DoubtFlag:
    return DoubtFlag(flag=flag, severity=severity, reason=reason, required_resolution=resolution)


def _freshness(claims: list[Claim]) -> str:
    if any(claim.verification_status == "stale" for claim in claims):
        if any(claim.verification_status == "verified" for claim in claims):
            return "mixed"
        return "stale"
    return "fresh"


def _confidence(basis: ConfidenceBasis, flags: list[DoubtFlag], *, total_claims: int) -> float:
    if total_claims <= 0:
        return 0.0
    score = (basis.verified_claims / total_claims) * 0.7
    score += min(0.3, basis.tool_evidence_refs * 0.1)
    penalties = {"low": 0.03, "medium": 0.1, "high": 0.2, "critical": 0.35}
    for flag in flags:
        score -= penalties.get(flag.severity, 0.1)
    return max(0.0, min(1.0, score))


def _recommended_decision(flags: list[DoubtFlag]) -> RecommendedDecision:
    if any(flag.flag == "user_confirmation_needed" for flag in flags):
        return "ask_human"
    if any(flag.flag == "conflicting_claims" and flag.severity in {"high", "critical"} for flag in flags):
        return "block"
    if any(flag.severity in {"medium", "high", "critical"} for flag in flags):
        return "revise"
    return "continue"

