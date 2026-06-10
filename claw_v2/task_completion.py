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
# Brain-fallback tool-use can attach a full evidence_manifest, but the manifest
# itself is not a verifier pass. A row may close terminally only when the
# manifest explicitly reports a passed result and has no blockers.
NEEDS_VERIFICATION_STATUSES = {"needs_verification", "needs_verify"}
SUCCESS_OUTCOMES = {
    "ok",
    "passed",
    "verified",
    "succeeded",
    "success",
    "completed",
    "complete",
    "done",
    "delivered",
}
FAILED_OUTCOMES = {"failed", "failure", "error"}
BLOCKED_OUTCOMES = {"blocked", "denied", "cancelled", "canceled"}


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
        # PR 0F: evidence_manifest is checked explicitly via
        # `_has_brain_tooluse_evidence_manifest`; the truthy-dict
        # fallthrough must NOT count it on its own, otherwise an empty
        # placeholder manifest would short-circuit the false-success
        # guard.
        "evidence_manifest",
        # outcome_manifest is validated explicitly. A placeholder manifest must
        # not make a task look executed by falling through the generic artifact
        # truthiness check.
        "outcome_manifest",
    }
)


def _has_evidence(record: dict[str, Any]) -> bool:
    artifacts = record.get("artifacts") or {}
    evidence = record.get("evidence") or {}
    if any(evidence.get(key) for key in evidence):
        return True
    if _has_brain_tooluse_evidence_manifest(record):
        return True
    if _has_outcome_manifest_evidence(record):
        return True
    for key, value in artifacts.items():
        if key in _METADATA_KEYS:
            continue
        if value:
            return True
    return False


def _has_brain_tooluse_evidence_manifest(record: dict[str, Any]) -> bool:
    """True iff record carries a substantive brain_fallback evidence pack.

    "Substantive" means the manifest reports actual tool use (non-empty
    `tools_run`) AND has a correlation hook (`trace_id` or
    `observe_event_ids`). Empty placeholder manifests don't count as
    evidence so the false-success guard can still catch them.
    """
    artifacts = record.get("artifacts") or {}
    manifest = artifacts.get("evidence_manifest") if isinstance(artifacts, dict) else None
    if not isinstance(manifest, dict):
        return False
    if str(manifest.get("origin") or "") != "brain_fallback":
        return False
    tools_run = manifest.get("tools_run") or []
    if not isinstance(tools_run, list) or not tools_run:
        return False
    has_trace = bool(manifest.get("trace_id"))
    has_event_ids = bool(manifest.get("observe_event_ids"))
    if not (has_trace or has_event_ids):
        return False
    return True


def _manifest_verification_result(record: dict[str, Any]) -> str:
    artifacts = record.get("artifacts") or {}
    manifest = artifacts.get("evidence_manifest") if isinstance(artifacts, dict) else None
    if not isinstance(manifest, dict):
        return ""
    return str(manifest.get("verification_result") or "").lower()


def _manifest_blockers(record: dict[str, Any]) -> list[Any]:
    artifacts = record.get("artifacts") or {}
    manifest = artifacts.get("evidence_manifest") if isinstance(artifacts, dict) else None
    if not isinstance(manifest, dict):
        return []
    blockers = manifest.get("blockers") or []
    return list(blockers) if isinstance(blockers, list) else []


def _outcome_manifest(record: dict[str, Any]) -> dict[str, Any] | None:
    manifest = record.get("outcome_manifest")
    if isinstance(manifest, dict):
        return manifest
    artifacts = record.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        return None
    manifest = artifacts.get("outcome_manifest")
    return manifest if isinstance(manifest, dict) else None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _outcome_blockers(manifest: dict[str, Any]) -> list[Any]:
    return [item for item in _as_list(manifest.get("blockers")) if item]


def _outcome_result(value: Any) -> str:
    return str(value or "").strip().lower()


def _outcome_final_result(manifest: dict[str, Any]) -> str:
    for key in ("final_outcome", "outcome", "result", "status"):
        if key in manifest:
            return _outcome_result(manifest.get(key))
    return ""


def _async_job_result(job: Any) -> str:
    if isinstance(job, dict):
        for key in ("final_outcome", "outcome", "result", "verification_status", "status", "state"):
            if key in job:
                return _outcome_result(job.get(key))
        return ""
    return _outcome_result(job)


def _pending_outcome_async_jobs(manifest: dict[str, Any]) -> list[Any]:
    pending = [item for item in _as_list(manifest.get("pending_async_jobs")) if item]
    for job in _as_list(manifest.get("async_jobs")):
        if not job:
            continue
        result = _async_job_result(job)
        if result not in SUCCESS_OUTCOMES:
            pending.append(job)
    return pending


def _outcome_manifest_has_passed_verification(manifest: dict[str, Any]) -> bool:
    for item in _as_list(manifest.get("verifications")):
        if isinstance(item, dict):
            result = _outcome_result(item.get("result") or item.get("status"))
        else:
            result = _outcome_result(item)
        if result in PASSED_VERIFICATION or result in SUCCESS_OUTCOMES:
            return True
    return False


def _has_outcome_manifest_evidence(record: dict[str, Any]) -> bool:
    manifest = _outcome_manifest(record)
    if not manifest:
        return False
    if any(item for item in _as_list(manifest.get("deliveries"))):
        return True
    if any(item for item in _as_list(manifest.get("evidence_refs"))):
        return True
    if any(item for item in _as_list(manifest.get("evidence"))):
        return True
    if _outcome_manifest_has_passed_verification(manifest):
        return True
    return False


def _validate_outcome_manifest(record: dict[str, Any]) -> CompletionDecision | None:
    manifest = _outcome_manifest(record)
    if not manifest:
        return None

    blockers = _outcome_blockers(manifest)
    if blockers:
        return CompletionDecision(
            final_status="pending",
            verification_status="blocked",
            reason="outcome_manifest_has_blockers",
            missing_evidence=["blocker_resolution"],
        )

    pending_async_jobs = _pending_outcome_async_jobs(manifest)
    if pending_async_jobs:
        return CompletionDecision(
            final_status="pending",
            verification_status="needs_verification",
            reason="outcome_manifest_has_pending_async_jobs",
            missing_evidence=["async_job_terminal_outcome"],
        )

    final_outcome = _outcome_final_result(manifest)
    if final_outcome in FAILED_OUTCOMES:
        return CompletionDecision(
            final_status="pending",
            verification_status="failed",
            reason="outcome_manifest_not_successful",
            missing_evidence=["successful_final_outcome"],
        )
    if final_outcome in BLOCKED_OUTCOMES:
        return CompletionDecision(
            final_status="pending",
            verification_status="blocked",
            reason="outcome_manifest_not_successful",
            missing_evidence=["successful_final_outcome"],
        )
    if final_outcome not in SUCCESS_OUTCOMES:
        return CompletionDecision(
            final_status="pending",
            verification_status="needs_verification",
            reason="outcome_manifest_not_terminal",
            missing_evidence=["terminal_outcome"],
        )
    return None


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
    has_brain_manifest = _has_brain_tooluse_evidence_manifest(record)

    if status in SUCCESS_STATUSES:
        outcome_decision = _validate_outcome_manifest(record)
        if outcome_decision is not None:
            return outcome_decision

    if status in SUCCESS_STATUSES and _looks_like_plan_only(summary) and not has_evidence:
        return CompletionDecision(
            final_status="pending",
            verification_status="missing_evidence",
            reason="plan_only_no_execution_evidence",
            missing_evidence=["actions_taken", "tool_result_or_artifact"],
        )

    # Runtime invariant: no row may persist as succeeded while its verification
    # status still says needs_verification. A brain tool-use manifest is
    # evidence of activity, not a passed verifier result.
    if (
        status in SUCCESS_STATUSES
        and verification in NEEDS_VERIFICATION_STATUSES
        and has_brain_manifest
    ):
        manifest_result = _manifest_verification_result(record)
        blockers = _manifest_blockers(record)
        if manifest_result in PASSED_VERIFICATION and not blockers:
            return CompletionDecision(
                final_status="succeeded",
                verification_status="passed",
                reason="brain_tooluse_verified_with_manifest",
                missing_evidence=[],
            )
        return CompletionDecision(
            final_status="pending",
            verification_status="needs_verification",
            reason="brain_tooluse_with_manifest_pending_verification",
            missing_evidence=["passed_verification"],
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
