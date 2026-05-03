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
        "lifecycle",
        "response_preview",
        "skill_result",
    }
)

_CONCRETE_EVIDENCE_KEYS = frozenset(
    {
        "changed_files",
        "diff",
        "test_output",
        "static_check",
        "repro_check",
        "commit",
        "pr_url",
        "artifact_path",
        "screenshot_path",
    }
)

_HANDLER_EXTERNAL_KEYS = frozenset(
    {
        "external_id",
        "external_title",
        "notebook_id",
        "notebook_title",
        "url",
        "source_url",
    }
)

_EMPTY_TEXT_VALUES = frozenset({"", "none", "n/a", "na", "null", "[]", "{}"})


def _is_concrete_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY_TEXT_VALUES
    if isinstance(value, dict):
        return any(_is_concrete_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_is_concrete_value(item) for item in value)
    return bool(value)


def _get_nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _has_key(payload: dict[str, Any], keys: set[str] | frozenset[str]) -> bool:
    return any(_is_concrete_value(payload.get(key)) for key in keys)


def _has_evidence(record: dict[str, Any]) -> bool:
    artifacts = record.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    evidence = record.get("evidence")
    if not isinstance(evidence, dict):
        evidence = artifacts.get("evidence") if isinstance(artifacts.get("evidence"), dict) else {}

    if _has_key(artifacts, _CONCRETE_EVIDENCE_KEYS) or _has_key(evidence, _CONCRETE_EVIDENCE_KEYS):
        return True

    coordinator_result = artifacts.get("coordinator_result")
    if isinstance(coordinator_result, dict):
        if _is_concrete_value(coordinator_result.get("changed_files")):
            return True
        if _is_concrete_value(coordinator_result.get("evidence")):
            return True
        checks = _get_nested(coordinator_result, "verification", "checks")
        if isinstance(checks, list):
            for check in checks:
                if not isinstance(check, dict):
                    continue
                if check.get("status") == "passed" and _is_concrete_value(check.get("evidence")):
                    return True

    if _is_concrete_value(artifacts.get("sources") or evidence.get("sources")) and _is_concrete_value(
        artifacts.get("synthesis") or evidence.get("synthesis")
    ):
        return True

    if _is_concrete_value(artifacts.get("handler_result") or evidence.get("handler_result")):
        if _has_key(artifacts, _HANDLER_EXTERNAL_KEYS) or _has_key(evidence, _HANDLER_EXTERNAL_KEYS):
            return True

    profile = artifacts.get("verification_profile")
    if isinstance(profile, dict) and profile.get("status") == "passed" and _is_concrete_value(evidence):
        return True

    for key, value in artifacts.items():
        if key in _METADATA_KEYS:
            continue
        if key in _CONCRETE_EVIDENCE_KEYS and _is_concrete_value(value):
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
