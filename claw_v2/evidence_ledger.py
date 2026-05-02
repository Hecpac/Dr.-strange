from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl

logger = logging.getLogger(__name__)

CLAIM_SCHEMA_VERSION = "evidence_ledger.v1"
ClaimType = Literal["fact", "inference", "assumption", "decision", "risk_signal"]
VerificationStatus = Literal["verified", "unverified", "contradicted", "stale"]

CLAIM_TYPES = frozenset({"fact", "inference", "assumption", "decision", "risk_signal"})
VERIFICATION_STATUSES = frozenset({"verified", "unverified", "contradicted", "stale"})
VERIFICATION_EVIDENCE_KINDS = frozenset({"tool_call", "file_read", "external_api"})


@dataclass(slots=True)
class EvidenceRef:
    kind: str
    ref: str
    captured_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "ref": self.ref, "captured_at": self.captured_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRef":
        return cls(
            kind=str(data["kind"]),
            ref=str(data["ref"]),
            captured_at=str(data.get("captured_at") or now_iso()),
        )


@dataclass(slots=True)
class Claim:
    claim_id: str
    goal_id: str
    claim_text: str
    claim_type: ClaimType
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    verification_status: VerificationStatus = "unverified"
    confidence: float = 0.0
    depends_on: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    schema_version: str = CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.goal_id.strip():
            raise ValueError("goal_id is required")
        if not self.claim_text.strip():
            raise ValueError("claim_text is required")
        if self.claim_type not in CLAIM_TYPES:
            raise ValueError(f"invalid claim_type: {self.claim_type}")
        if self.verification_status not in VERIFICATION_STATUSES:
            raise ValueError(f"invalid verification_status: {self.verification_status}")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.verification_status == "verified" and not any(
            ref.kind in VERIFICATION_EVIDENCE_KINDS for ref in self.evidence_refs
        ):
            raise ValueError("verified claims require tool_call, file_read, or external_api evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "goal_id": self.goal_id,
            "claim_text": self.claim_text,
            "claim_type": self.claim_type,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "verification_status": self.verification_status,
            "confidence": float(self.confidence),
            "depends_on": list(self.depends_on),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        return cls(
            claim_id=str(data["claim_id"]),
            goal_id=str(data["goal_id"]),
            claim_text=str(data["claim_text"]),
            claim_type=str(data["claim_type"]),  # type: ignore[arg-type]
            evidence_refs=[EvidenceRef.from_dict(item) for item in data.get("evidence_refs", [])],
            verification_status=str(data.get("verification_status") or "unverified"),  # type: ignore[arg-type]
            confidence=float(data.get("confidence") or 0.0),
            depends_on=[str(item) for item in data.get("depends_on", [])],
            created_at=str(data.get("created_at") or now_iso()),
            schema_version=str(data.get("schema_version") or CLAIM_SCHEMA_VERSION),
        )


def record_claim(
    telemetry_root: Path | str,
    *,
    goal_id: str,
    claim_text: str,
    claim_type: ClaimType,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | None = None,
    verification_status: VerificationStatus = "unverified",
    confidence: float = 0.0,
    depends_on: list[str] | None = None,
    session_id: str = "runtime",
    observe: Any | None = None,
) -> Claim:
    claim = Claim(
        claim_id=generate_id("c"),
        goal_id=goal_id,
        claim_text=claim_text,
        claim_type=claim_type,
        evidence_refs=[_coerce_evidence_ref(item) for item in (evidence_refs or [])],
        verification_status=verification_status,
        confidence=confidence,
        depends_on=list(depends_on or []),
        created_at=now_iso(),
    )
    append_jsonl(_claims_path(telemetry_root), claim.to_dict())
    try:
        from claw_v2.action_events import emit_event

        emit_event(
            telemetry_root,
            event_type="claim_recorded",
            actor="claw",
            goal_id=claim.goal_id,
            session_id=session_id,
            claims=[claim.claim_id],
            evidence_refs=[_event_evidence_ref(ref) for ref in claim.evidence_refs],
            risk_level="low" if claim.claim_type != "risk_signal" else "medium",
            observe=None,
        )
    except Exception:
        logger.debug("Could not mirror claim_recorded into action events", exc_info=True)
    if observe is not None:
        observe.emit("claim_recorded", payload=claim.to_dict())
    return claim


def load_claims(telemetry_root: Path | str) -> list[Claim]:
    return [Claim.from_dict(row) for row in read_jsonl(_claims_path(telemetry_root))]


def _claims_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "claims.jsonl"


def _coerce_evidence_ref(value: EvidenceRef | dict[str, Any]) -> EvidenceRef:
    if isinstance(value, EvidenceRef):
        return value
    return EvidenceRef.from_dict(value)


def _event_evidence_ref(ref: EvidenceRef) -> str:
    return f"{ref.kind}:{ref.ref}"
