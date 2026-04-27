from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CompletionDecision:
    final_status: str
    verification_status: str
    reason: str
    missing_evidence: list[str]


SUCCESS_STATUSES = {"succeeded", "completed", "done", "closed"}
PASSED_VERIFICATION = {"ok", "passed", "verified"}
FAILED_VERIFICATION = {"failed", "blocked", "denied"}
COMPLETION_CANDIDATES = SUCCESS_STATUSES


_METADATA_KEYS = frozenset(
    {
        "trace_id",
        "root_trace_id",
        "span_id",
        "parent_span_id",
        "job_id",
        "task_id",
        "session_id",
        "metadata",
        "evidence",
    }
)


def _has_evidence(record: dict[str, Any]) -> bool:
    artifacts = record.get("artifacts") or {}
    evidence = record.get("evidence") or {}
    if any(evidence.get(key) for key in evidence):
        return True
    for key, value in artifacts.items():
        if key in _METADATA_KEYS:
            continue
        if value:
            return True
    return False


def _looks_like_plan_only(summary: str) -> bool:
    if not summary:
        return False
    markers = ("Step 1", "**Step", "Paso 1", "Step 2", "Paso 2")
    return any(marker in summary for marker in markers)


def validate_completion(record: dict[str, Any]) -> CompletionDecision:
    status = str(record.get("status") or "").lower()
    verification = str(record.get("verification_status") or "").lower()
    summary = str(record.get("summary") or "")
    has_evidence = _has_evidence(record)

    if status in SUCCESS_STATUSES and _looks_like_plan_only(summary) and not has_evidence:
        return CompletionDecision(
            final_status="pending",
            verification_status="missing_evidence",
            reason="plan_only_no_execution_evidence",
            missing_evidence=["actions_taken", "tool_result_or_artifact"],
        )

    if status in SUCCESS_STATUSES and verification not in PASSED_VERIFICATION:
        return CompletionDecision(
            final_status="pending",
            verification_status=verification or "pending",
            reason="success_without_passed_verification",
            missing_evidence=["passed_verification"],
        )

    if status in SUCCESS_STATUSES and not has_evidence:
        return CompletionDecision(
            final_status="pending",
            verification_status="missing_evidence",
            reason="success_without_evidence",
            missing_evidence=["tool_result_or_artifact"],
        )

    if status in SUCCESS_STATUSES and verification in PASSED_VERIFICATION and has_evidence:
        return CompletionDecision(
            final_status="succeeded",
            verification_status="passed",
            reason="verified_with_evidence",
            missing_evidence=[],
        )

    if verification in FAILED_VERIFICATION:
        return CompletionDecision(
            final_status="failed" if verification == "failed" else "blocked",
            verification_status=verification,
            reason="verification_failed_or_blocked",
            missing_evidence=[],
        )

    return CompletionDecision(
        final_status=status or "pending",
        verification_status=verification or "pending",
        reason="not_terminal_or_not_ready",
        missing_evidence=[],
    )
