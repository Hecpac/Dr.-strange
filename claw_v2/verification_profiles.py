from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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


@dataclass(slots=True)
class ProfileVerificationDecision:
    status: str
    reason: str
    missing_evidence: list[str] = field(default_factory=list)
    risk_level: str = "low"
    requires_human_approval: bool = False


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
    ),
    "coding_bugfix": VerificationProfile(
        task_kind="coding_bugfix",
        required_evidence=("changed_files", "diff", "test_output_or_repro_check"),
        allowed_risk="medium",
        human_approval_required=False,
        verifier_required=True,
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
        )

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
        )

    if profile.human_approval_required:
        return ProfileVerificationDecision(
            status="blocked",
            reason="human_approval_required",
            missing_evidence=[],
            risk_level=profile.allowed_risk,
            requires_human_approval=True,
        )

    return ProfileVerificationDecision(
        status="passed",
        reason="profile_evidence_satisfied",
        missing_evidence=[],
        risk_level=profile.allowed_risk,
        requires_human_approval=False,
    )
