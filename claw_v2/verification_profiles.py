from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.no_fudge import validate_no_fudge_factors


RISK_ORDER: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


@dataclass(slots=True)
class VerificationProfile:
    task_kind: str
    required_evidence: tuple[str, ...]
    allowed_risk: str = "medium"
    human_approval_required: bool = False
    verifier_required: bool = False
    verification_coordinates: tuple[str, ...] = ("required_evidence",)


@dataclass(slots=True)
class ProfileVerificationDecision:
    status: str
    reason: str
    missing_evidence: list[str] = field(default_factory=list)
    risk_level: str = "low"
    requires_human_approval: bool = False
    coordinates: list[dict[str, Any]] = field(default_factory=list)


PROFILES: dict[str, VerificationProfile] = {
    "notebooklm_create": VerificationProfile(
        task_kind="notebooklm_create",
        required_evidence=("handler_result", "notebook_id_or_title"),
        allowed_risk="low",
        human_approval_required=False,
        verifier_required=False,
    ),
    "notebooklm_review": VerificationProfile(
        task_kind="notebooklm_review",
        required_evidence=("handler_result", "notebook_id_or_title", "review_summary"),
        allowed_risk="low",
        human_approval_required=False,
        verifier_required=False,
    ),
    "coding_inspection": VerificationProfile(
        task_kind="coding_inspection",
        required_evidence=("files_read", "findings"),
        allowed_risk="low",
        human_approval_required=False,
        verifier_required=False,
    ),
    "coding_patch": VerificationProfile(
        task_kind="coding_patch",
        required_evidence=("changed_files", "diff", "verification_check"),
        allowed_risk="medium",
        human_approval_required=False,
        verifier_required=True,
        verification_coordinates=("required_evidence", "fast_tests", "no_fudge_factors"),
    ),
    "coding_bugfix": VerificationProfile(
        task_kind="coding_bugfix",
        required_evidence=("changed_files", "diff", "test_output_or_repro_check"),
        allowed_risk="medium",
        human_approval_required=False,
        verifier_required=True,
        verification_coordinates=("required_evidence", "fast_tests", "no_fudge_factors"),
    ),
    "research": VerificationProfile(
        task_kind="research",
        required_evidence=("sources", "synthesis"),
        allowed_risk="low",
        human_approval_required=False,
        verifier_required=False,
    ),
    "pipeline_merge": VerificationProfile(
        task_kind="pipeline_merge",
        required_evidence=("pr_url", "approval_id"),
        allowed_risk="high",
        human_approval_required=True,
        verifier_required=True,
    ),
    "social_publish": VerificationProfile(
        task_kind="social_publish",
        required_evidence=("drafts_preview", "approval_id"),
        allowed_risk="critical",
        human_approval_required=True,
        verifier_required=True,
    ),
    "ai_news_brief": VerificationProfile(
        task_kind="ai_news_brief",
        required_evidence=("sources", "claim_map", "fetched_at"),
        allowed_risk="low",
        human_approval_required=False,
        verifier_required=False,
    ),
}


_EVIDENCE_ALIASES: dict[str, tuple[str, ...]] = {
    "notebook_id_or_title": ("notebook_id", "notebook_title"),
    "test_output_or_repro_check": ("test_output", "repro_check"),
    "verification_check": ("verification_check", "test_output", "static_check"),
}


def _evidence_has_key(evidence: dict[str, Any], key: str) -> bool:
    aliases = _EVIDENCE_ALIASES.get(key)
    if aliases is None:
        return bool(evidence.get(key))
    return any(evidence.get(alias) for alias in aliases)


def get_profile(task_kind: str) -> VerificationProfile | None:
    return PROFILES.get(task_kind)


def verify_profile_evidence(
    *,
    task_kind: str,
    evidence: dict[str, Any],
) -> ProfileVerificationDecision:
    profile = PROFILES.get(task_kind)
    if profile is None:
        return ProfileVerificationDecision(
            status="pending",
            reason="unknown_task_kind",
            missing_evidence=["task_kind_profile"],
            risk_level="medium",
            requires_human_approval=False,
            coordinates=[
                {
                    "dimension": "required_evidence",
                    "status": "unknown_profile",
                    "required": True,
                    "evidence": {"task_kind": task_kind},
                }
            ],
        )

    coordinates = verification_coordinates_for(task_kind=task_kind, evidence=evidence)
    missing = [
        key
        for key in profile.required_evidence
        if not _evidence_has_key(evidence, key)
    ]

    if missing:
        return ProfileVerificationDecision(
            status="pending",
            reason="missing_required_evidence",
            missing_evidence=missing,
            risk_level=profile.allowed_risk,
            requires_human_approval=profile.human_approval_required,
            coordinates=coordinates,
        )

    no_fudge = _coordinate_by_dimension(coordinates, "no_fudge_factors")
    if no_fudge is not None and no_fudge.get("status") == "blocked":
        return ProfileVerificationDecision(
            status="blocked",
            reason="no_fudge_factors",
            missing_evidence=["no_fudge_justification"],
            risk_level="high",
            requires_human_approval=True,
            coordinates=coordinates,
        )

    missing_coordinates = [
        str(coordinate.get("dimension"))
        for coordinate in coordinates
        if coordinate.get("required") and coordinate.get("status") == "missing"
    ]
    if missing_coordinates:
        return ProfileVerificationDecision(
            status="pending",
            reason="missing_verification_coordinates",
            missing_evidence=missing_coordinates,
            risk_level=profile.allowed_risk,
            requires_human_approval=False,
            coordinates=coordinates,
        )

    if profile.human_approval_required:
        return ProfileVerificationDecision(
            status="blocked",
            reason="human_approval_required",
            missing_evidence=[],
            risk_level=profile.allowed_risk,
            requires_human_approval=True,
            coordinates=coordinates,
        )

    return ProfileVerificationDecision(
        status="passed",
        reason="profile_evidence_satisfied",
        missing_evidence=[],
        risk_level=profile.allowed_risk,
        requires_human_approval=False,
        coordinates=coordinates,
    )


def verification_coordinates_for(*, task_kind: str, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    profile = PROFILES.get(task_kind)
    if profile is None:
        return [
            {
                "dimension": "required_evidence",
                "status": "unknown_profile",
                "required": True,
                "evidence": {"task_kind": task_kind},
            }
        ]

    coordinates: list[dict[str, Any]] = [
        _required_evidence_coordinate(profile=profile, evidence=evidence),
    ]
    if "fast_tests" in profile.verification_coordinates:
        coordinates.append(_fast_tests_coordinate(task_kind=task_kind, evidence=evidence))
    if "no_fudge_factors" in profile.verification_coordinates:
        coordinates.append(_no_fudge_coordinate(evidence=evidence))
    if bool(evidence.get("critical_logic")):
        coordinates.append(_critical_logic_coordinate(evidence=evidence))
    if evidence.get("success_contract"):
        coordinates.append(_success_contract_coordinate(evidence=evidence))
    return coordinates


def record_verification_coordinates(
    telemetry_root: Path | str,
    *,
    goal_id: str,
    decision: ProfileVerificationDecision,
    source_ref: str = "verification_profiles",
    session_id: str = "runtime",
    observe: Any | None = None,
) -> list[Any]:
    """Record each verification coordinate as its own evidence-ledger claim."""

    from claw_v2.evidence_ledger import EvidenceRef, record_claim

    claims: list[Any] = []
    for coordinate in decision.coordinates:
        dimension = str(coordinate.get("dimension") or "unknown")
        status = str(coordinate.get("status") or "unknown")
        passed = status in {"passed", "present"}
        claims.append(
            record_claim(
                telemetry_root,
                goal_id=goal_id,
                claim_text=f"Verification coordinate {dimension} status={status}",
                claim_type="fact" if passed else "risk_signal",
                evidence_refs=[EvidenceRef(kind="tool_call", ref=f"{source_ref}:{dimension}")],
                verification_status="verified" if passed else "unverified",
                confidence=0.99 if passed else 0.0,
                session_id=session_id,
                observe=observe,
            )
        )
    return claims


def _required_evidence_coordinate(
    *,
    profile: VerificationProfile,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    missing = [
        key
        for key in profile.required_evidence
        if not _evidence_has_key(evidence, key)
    ]
    return {
        "dimension": "required_evidence",
        "status": "missing" if missing else "passed",
        "required": True,
        "missing_evidence": missing,
        "evidence": {"keys": sorted(str(key) for key in evidence.keys())},
    }


def _fast_tests_coordinate(*, task_kind: str, evidence: dict[str, Any]) -> dict[str, Any]:
    if task_kind == "coding_bugfix":
        present = _evidence_has_key(evidence, "test_output_or_repro_check")
    else:
        present = _evidence_has_key(evidence, "verification_check")
    return {
        "dimension": "fast_tests",
        "status": "passed" if present else "missing",
        "required": True,
        "evidence": {
            "test_output": bool(evidence.get("test_output")),
            "repro_check": bool(evidence.get("repro_check")),
            "verification_check": bool(evidence.get("verification_check")),
            "static_check": bool(evidence.get("static_check")),
        },
    }


def _no_fudge_coordinate(*, evidence: dict[str, Any]) -> dict[str, Any]:
    report = validate_no_fudge_factors(str(evidence.get("diff") or ""), evidence=evidence)
    return {
        "dimension": "no_fudge_factors",
        "status": report.status,
        "required": True,
        "evidence": report.to_dict(),
    }


def _critical_logic_coordinate(*, evidence: dict[str, Any]) -> dict[str, Any]:
    present = bool(evidence.get("randomized_check") or evidence.get("parametrized_check"))
    return {
        "dimension": "randomized_or_parametrized_check",
        "status": "passed" if present else "missing",
        "required": True,
        "evidence": {
            "critical_logic": True,
            "randomized_check": bool(evidence.get("randomized_check")),
            "parametrized_check": bool(evidence.get("parametrized_check")),
        },
    }


def _success_contract_coordinate(*, evidence: dict[str, Any]) -> dict[str, Any]:
    present = bool(evidence.get("invariant_check") or evidence.get("contract_check"))
    return {
        "dimension": "success_contract_invariants",
        "status": "passed" if present else "missing",
        "required": True,
        "evidence": {
            "success_contract": bool(evidence.get("success_contract")),
            "invariant_check": bool(evidence.get("invariant_check")),
            "contract_check": bool(evidence.get("contract_check")),
        },
    }


def _coordinate_by_dimension(
    coordinates: list[dict[str, Any]],
    dimension: str,
) -> dict[str, Any] | None:
    for coordinate in coordinates:
        if coordinate.get("dimension") == dimension:
            return coordinate
    return None
