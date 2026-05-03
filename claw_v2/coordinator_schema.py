from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


COORDINATOR_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "status",
        "task_kind",
        "actions_taken",
        "evidence",
        "changed_files",
        "verification",
        "blockers",
        "next_user_action",
        "summary_for_user",
    ],
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["executed", "blocked", "pending", "failed"],
        },
        "task_kind": {"type": "string"},
        "actions_taken": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["agent", "action", "tool", "result"],
                "additionalProperties": False,
                "properties": {
                    "agent": {"type": "string"},
                    "action": {"type": "string"},
                    "tool": {"type": "string"},
                    "result": {"type": "string"},
                },
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "name", "value"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
        "changed_files": {
            "type": "array",
            "items": {"type": "string"},
        },
        "verification": {
            "type": "object",
            "required": ["status", "checks"],
            "additionalProperties": False,
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["passed", "pending", "failed", "blocked"],
                },
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "status", "evidence"],
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["passed", "pending", "failed", "blocked"],
                            },
                            "evidence": {"type": "string"},
                        },
                    },
                },
            },
        },
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
        },
        "next_user_action": {"type": ["string", "null"]},
        "summary_for_user": {"type": "string"},
    },
}


VALID_RESULT_STATUSES = frozenset({"executed", "blocked", "pending", "failed"})
VALID_VERIFICATION_STATUSES = frozenset({"passed", "pending", "failed", "blocked"})


@dataclass(slots=True)
class CoordinatorValidation:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _shape_errors(payload: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    required = COORDINATOR_RESULT_SCHEMA["required"]
    for key in required:
        if key not in payload:
            errors.append(f"missing:{key}")
    if errors:
        return errors

    extras = set(payload.keys()) - set(COORDINATOR_RESULT_SCHEMA["properties"].keys())
    if extras:
        errors.append("additional_properties:" + ",".join(sorted(extras)))

    if payload.get("status") not in VALID_RESULT_STATUSES:
        errors.append("invalid_status")
    if not isinstance(payload.get("task_kind"), str) or not payload["task_kind"].strip():
        errors.append("task_kind_must_be_non_empty_string")

    actions = payload.get("actions_taken")
    if not isinstance(actions, list):
        errors.append("actions_taken_must_be_array")
    else:
        for index, item in enumerate(actions):
            if not isinstance(item, dict):
                errors.append(f"actions_taken[{index}]_not_object")
                continue
            for required_key in ("agent", "action", "tool", "result"):
                if required_key not in item:
                    errors.append(f"actions_taken[{index}].missing:{required_key}")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence_must_be_array")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                errors.append(f"evidence[{index}]_not_object")
                continue
            for required_key in ("type", "name", "value"):
                if required_key not in item:
                    errors.append(f"evidence[{index}].missing:{required_key}")

    changed = payload.get("changed_files")
    if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
        errors.append("changed_files_must_be_array_of_strings")

    verification = payload.get("verification")
    if not isinstance(verification, dict):
        errors.append("verification_must_be_object")
    else:
        if verification.get("status") not in VALID_VERIFICATION_STATUSES:
            errors.append("verification.invalid_status")
        if not isinstance(verification.get("checks"), list):
            errors.append("verification.checks_must_be_array")

    if not isinstance(payload.get("blockers"), list):
        errors.append("blockers_must_be_array")
    nua = payload.get("next_user_action")
    if nua is not None and not isinstance(nua, str):
        errors.append("next_user_action_must_be_string_or_null")
    if not isinstance(payload.get("summary_for_user"), str):
        errors.append("summary_for_user_must_be_string")

    return errors


def validate_coordinator_result(payload: Any) -> CoordinatorValidation:
    errors = _shape_errors(payload)
    return CoordinatorValidation(valid=not errors, errors=errors)


def validate_coordinator_semantics(result: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(result, dict):
        return ["payload_not_object"]

    actions = result.get("actions_taken") or []
    evidence = result.get("evidence") or []
    changed_files = result.get("changed_files") or []
    blockers = result.get("blockers") or []
    verification = result.get("verification") or {}
    verification_status = verification.get("status")
    checks = verification.get("checks") if isinstance(verification, dict) else []
    status = result.get("status")

    if status == "executed" and not actions:
        errors.append("executed_requires_actions_taken")

    passed_check_evidence = (
        isinstance(checks, list)
        and any(
            isinstance(check, dict)
            and check.get("status") == "passed"
            and bool(str(check.get("evidence") or "").strip())
            for check in checks
        )
    )
    if verification_status == "passed" and not (evidence or changed_files or passed_check_evidence):
        errors.append("passed_verification_requires_evidence")

    if blockers and status not in {"blocked", "pending"}:
        errors.append("blockers_require_blocked_or_pending_status")

    if status == "executed" and verification_status != "passed":
        errors.append("executed_requires_passed_verification")

    return errors


def coerce_unstructured_coordinator_output(raw_text: str | None) -> dict[str, Any]:
    text = (raw_text or "").strip()
    return {
        "status": "pending",
        "task_kind": "unknown",
        "actions_taken": [],
        "evidence": [],
        "changed_files": [],
        "verification": {"status": "pending", "checks": []},
        "blockers": ["Coordinator returned unstructured output; cannot verify execution."],
        "next_user_action": None,
        "summary_for_user": text[:1000],
    }
