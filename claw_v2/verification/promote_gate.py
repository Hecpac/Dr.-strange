"""F2.5 — Promote gate: the ONLY path that can produce a terminal status of
`succeeded` for a task that declared a SuccessCondition.

Wires into `claw_v2/task_handler.py` at the exact point where today
`terminal_status = "succeeded" if verification_status == "passed" else ...`
is computed (line 754 area). The integration is opt-in via the checkpoint
field `success_condition_artifact` — when absent, the gate is a no-op and
existing tasks keep their behavior. When present, tool.ok=True alone is
NEVER enough to land `succeeded`.

This module is pure-function. No I/O, no external calls. Callers in F3+F5
pre-fetch any external observations and pass them in via the artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from claw_v2.verification.success_contract import (
    SUCCESS_CONDITION_SCHEMA_VERSION,
    ExternalCheckSpec,
    FileIntegrityCheck as _FileIntegrityCheck,
    PreflightSpec,
    StateDeltaSpec,
    SuccessCondition,
    VerificationResult,
    build_verification_envelope,
    validate_success_condition,
)


@dataclass(slots=True)
class GateOutcome:
    """Result of running the promote gate on a candidate terminal status."""

    terminal_status: str  # "succeeded" | "failed" | "" (blocked-by-input handled upstream)
    verification_status: str  # "passed" | "pending_verification" | "failed" | "blocked"
    envelope: VerificationResult | None
    degraded: bool  # True if the gate downgraded from succeeded
    reason: str  # short code describing the degrade, "" if not degraded
    envelopes: list[dict[str, Any] | None] | None = None


_ARTIFACT_KEY = "success_condition_artifact"
_ARTIFACTS_KEY = "success_condition_artifacts"


def _deserialize_success_condition(payload: Mapping[str, Any]) -> SuccessCondition | None:
    """Rebuild SuccessCondition from a JSON-safe dict the checkpoint carries."""
    if not isinstance(payload, Mapping):
        return None
    try:
        ext_raw = payload.get("external_check")
        ext = None
        if isinstance(ext_raw, Mapping):
            ext = ExternalCheckSpec(
                kind=ext_raw["kind"],
                target=str(ext_raw.get("target") or ""),
                expected_phrases=tuple(ext_raw.get("expected_phrases") or ()),
                forbidden_phrases=tuple(ext_raw.get("forbidden_phrases") or ()),
                json_path_equals=ext_raw.get("json_path_equals"),
                settle_seconds=float(ext_raw.get("settle_seconds", 6.0)),
                timeout_seconds=float(ext_raw.get("timeout_seconds", 30.0)),
                retries=int(ext_raw.get("retries", 3)),
            )
        sd_raw = payload.get("state_delta_check")
        sd = None
        if isinstance(sd_raw, Mapping):
            sd = StateDeltaSpec(
                db_table=sd_raw.get("db_table"),
                expected_rows_delta=int(sd_raw.get("expected_rows_delta", 1)),
                fs_path=sd_raw.get("fs_path"),
                expected_size_delta_bytes=int(sd_raw.get("expected_size_delta_bytes", 1)),
                expected_content_changed=bool(sd_raw.get("expected_content_changed", False)),
            )
        return SuccessCondition(
            must_contain_keys=tuple(payload.get("must_contain_keys") or ()),
            must_match_regex=dict(payload.get("must_match_regex") or {}),
            external_check=ext,
            state_delta_check=sd,
            forbidden_reasons=tuple(payload.get("forbidden_reasons") or ()),
            schema_version=str(payload.get("schema_version") or "1.0.0"),
            must_equal=dict(payload.get("must_equal") or {}),
            must_be_empty_keys=tuple(payload.get("must_be_empty_keys") or ()),
            must_be_existing_path=tuple(payload.get("must_be_existing_path") or ()),
            must_be_nonempty_str=tuple(payload.get("must_be_nonempty_str") or ()),
            cross_field_equality=tuple(
                (a, b) for a, b in (payload.get("cross_field_equality") or [])
            ),
            cross_field_inequality=tuple(
                (a, b) for a, b in (payload.get("cross_field_inequality") or [])
            ),
            forbidden_field_values={
                k: tuple(v) for k, v in (payload.get("forbidden_field_values") or {}).items()
            },
            verify_file_integrity=tuple(
                _FileIntegrityCheck(
                    path_field=item.get("path_field", ""),
                    hash_field=item.get("hash_field", ""),
                    size_field=item.get("size_field", ""),
                    max_bytes=int(item.get("max_bytes", 64 * 1024 * 1024)),
                )
                for item in (payload.get("verify_file_integrity") or [])
                if isinstance(item, Mapping) and item.get("path_field")
            ),
            allowed_path_roots=tuple(payload.get("allowed_path_roots") or ()),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _deserialize_preflight(payload: Any) -> PreflightSpec | None:
    if not isinstance(payload, Mapping):
        return None
    try:
        return PreflightSpec(
            probe_kind=payload["probe_kind"],
            target=str(payload.get("target") or ""),
            selectors=tuple(payload.get("selectors") or ()),
            must_match=dict(payload.get("must_match") or {}),
            fail_message=str(payload.get("fail_message") or "preflight failed"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def gate_terminal_status(
    *,
    raw_terminal_status: str,
    raw_verification_status: str,
    completed_checkpoint: Mapping[str, Any] | None,
) -> GateOutcome:
    """Run the success-condition gate.

    Returns a `GateOutcome` describing the (possibly downgraded) terminal
    status and a verification envelope ready to persist alongside the
    task ledger row.

    Contract:
      * If the candidate terminal_status is NOT "succeeded" → passthrough.
      * If checkpoint has no `success_condition_artifact` → passthrough
        (backward-compatible; existing tasks unchanged).
      * Else build SuccessCondition + run validate + build envelope.
        envelope.status determines the new terminal status:
          - "passed"               → "succeeded"
          - "pending_verification" → "" (terminal_status cleared; task stays open)
          - "failed"               → "failed"
          - "blocked"              → "" (verification_status="blocked")
    """
    checkpoint = dict(completed_checkpoint or {})
    artifacts = checkpoint.get(_ARTIFACTS_KEY)
    artifact = checkpoint.get(_ARTIFACT_KEY)
    if isinstance(artifacts, list) and len(artifacts) == 1 and artifact is None:
        artifact = artifacts[0]

    if raw_terminal_status != "succeeded":
        return GateOutcome(
            terminal_status=raw_terminal_status,
            verification_status=raw_verification_status,
            envelope=None,
            degraded=False,
            reason="",
        )

    if isinstance(artifacts, list) and len(artifacts) > 1:
        outcomes: list[GateOutcome] = []
        for item in artifacts:
            if not isinstance(item, Mapping):
                outcomes.append(
                    GateOutcome(
                        terminal_status="failed",
                        verification_status="failed",
                        envelope=None,
                        degraded=True,
                        reason="malformed_success_condition_artifact",
                    )
                )
                continue
            outcomes.append(
                gate_terminal_status(
                    raw_terminal_status=raw_terminal_status,
                    raw_verification_status=raw_verification_status,
                    completed_checkpoint={_ARTIFACT_KEY: item},
                )
            )

        envelopes = [out.envelope.to_dict() if out.envelope else None for out in outcomes]
        degraded_envelope = next(
            (out.envelope for out in outcomes if out.degraded and out.envelope is not None),
            None,
        )
        first_envelope = degraded_envelope or next(
            (out.envelope for out in outcomes if out.envelope is not None),
            None,
        )
        if any(
            out.terminal_status == "failed" or out.verification_status == "failed"
            for out in outcomes
        ):
            return GateOutcome(
                terminal_status="failed",
                verification_status="failed",
                envelope=first_envelope,
                degraded=True,
                reason="multi_artifact_failed",
                envelopes=envelopes,
            )
        if any(out.verification_status == "blocked" for out in outcomes):
            return GateOutcome(
                terminal_status="",
                verification_status="blocked",
                envelope=first_envelope,
                degraded=True,
                reason="multi_artifact_blocked",
                envelopes=envelopes,
            )
        if any(out.verification_status == "pending_verification" for out in outcomes):
            return GateOutcome(
                terminal_status="",
                verification_status="pending_verification",
                envelope=first_envelope,
                degraded=True,
                reason="multi_artifact_pending_verification",
                envelopes=envelopes,
            )
        return GateOutcome(
            terminal_status="succeeded",
            verification_status="passed",
            envelope=first_envelope,
            degraded=False,
            reason="",
            envelopes=envelopes,
        )

    if artifact is None:
        return GateOutcome(
            terminal_status=raw_terminal_status,
            verification_status=raw_verification_status,
            envelope=None,
            degraded=False,
            reason="",
        )

    if not isinstance(artifact, Mapping):
        # Malformed artifact → cannot trust succeeded
        envelope = None
        return GateOutcome(
            terminal_status="failed",
            verification_status="failed",
            envelope=envelope,
            degraded=True,
            reason="malformed_success_condition_artifact",
        )

    condition = _deserialize_success_condition(artifact.get("success_condition") or {})
    if condition is None:
        return GateOutcome(
            terminal_status="failed",
            verification_status="failed",
            envelope=None,
            degraded=True,
            reason="success_condition_unparseable",
        )

    # D8/DV.2 (2026-06-12): an artifact serialized under another schema
    # version must not silently pass (or fail) the gate — its check
    # semantics may have changed. Park it as pending, never "failed".
    if condition.schema_version != SUCCESS_CONDITION_SCHEMA_VERSION:
        return GateOutcome(
            terminal_status="",
            verification_status="pending_verification",
            envelope=None,
            degraded=True,
            reason="schema_version_mismatch",
        )

    tool_result = dict(artifact.get("tool_result") or {})
    external_observation = artifact.get("external_observation")
    state_delta_observation = artifact.get("state_delta_observation")
    evidence_uri = artifact.get("evidence_uri")
    preflight_payload = artifact.get("preflight")
    preflight = _deserialize_preflight(preflight_payload) if preflight_payload else None
    preflight_passed = bool(artifact.get("preflight_passed", False))
    tier = int(artifact.get("tier") or 0)

    errors = validate_success_condition(
        tool_result=tool_result,
        condition=condition,
        external_observation=external_observation
        if isinstance(external_observation, Mapping)
        else None,
        state_delta_observation=state_delta_observation
        if isinstance(state_delta_observation, Mapping)
        else None,
    )

    # Tier-3 specific gating: preflight + evidence + SOME form of post-hoc
    # verification are mandatory. external_check, verify_file_integrity, or
    # state_delta_check all satisfy the verification requirement.
    if tier == 3:
        has_verification = (
            condition.external_check is not None
            or bool(condition.verify_file_integrity)
            or condition.state_delta_check is not None
        )
        if not has_verification:
            errors.append("tier3_requires_external_verification")
        if preflight is None:
            errors.append("tier3_requires_preflight")
        if preflight is not None and not preflight_passed:
            errors.append("tier3_preflight_not_passed")
        if not evidence_uri:
            errors.append("tier3_requires_evidence_uri")

    envelope = build_verification_envelope(
        condition=condition,
        errors=errors,
        evidence_uri=evidence_uri if isinstance(evidence_uri, str) else None,
        external_observation=external_observation
        if isinstance(external_observation, Mapping)
        else None,
    )

    # Tier-3 blocked reasons collapse to verification_status="blocked"
    blocked_codes = {
        "tier3_requires_external_verification",
        "tier3_requires_preflight",
        "tier3_preflight_not_passed",
        "tier3_requires_evidence_uri",
    }
    if tier == 3 and any(e in blocked_codes for e in errors):
        return GateOutcome(
            terminal_status="",
            verification_status="blocked",
            envelope=envelope,
            degraded=True,
            reason="tier3_requirements_not_met",
        )

    if envelope.status == "passed":
        return GateOutcome(
            terminal_status="succeeded",
            verification_status="passed",
            envelope=envelope,
            degraded=False,
            reason="",
        )
    if envelope.status == "pending_verification":
        return GateOutcome(
            terminal_status="",
            verification_status="pending_verification",
            envelope=envelope,
            degraded=True,
            reason="missing_observation",
        )
    # "failed" or any other envelope status downgrades
    return GateOutcome(
        terminal_status="failed",
        verification_status="failed",
        envelope=envelope,
        degraded=True,
        reason="success_condition_violated",
    )


# ---------------------------------------------------------------------------
# F2.5.1 — fail-closed wrapper used by task_handler.py.
#
# Centralises the call so the integration is testable AS A FUNCTION (not via
# inspect.getsource). Returns (terminal_status, verification_status,
# new_checkpoint, events_to_emit). On exception with a declared
# success_condition_artifact, the wrapper FAILS CLOSED — terminal_status is
# wiped, verification_status becomes "blocked", and an event is queued.
# ---------------------------------------------------------------------------


def apply_promote_gate_to_checkpoint(
    *,
    raw_terminal_status: str,
    raw_verification_status: str,
    completed_checkpoint: Mapping[str, Any] | None,
) -> tuple[str, str, dict, list[tuple[str, dict]]]:
    """Apply the gate AND fail-closed on exception.

    Returns:
        (terminal_status, verification_status, new_checkpoint, events)

    Semantics:
      * If gate_terminal_status() succeeds → return its outcome (possibly
        degraded) plus a `promote_gate_envelope` audit field on the checkpoint
        when applicable.
      * If gate_terminal_status() RAISES:
          - When `success_condition_artifact` is present AND
            raw_terminal_status == "succeeded":
              * terminal_status = ""
              * verification_status = "blocked"
              * checkpoint gets promote_gate_reason="promote_gate_exception"
              * event "promote_gate_failed_closed" is queued
          - Otherwise (no artifact): legacy passthrough — the caller's
            existing path is preserved exactly as before F2.5.

    The function is pure: it never raises, never touches I/O, and the
    list of events lets the caller (task_handler) wire its own emit().
    """
    checkpoint = dict(completed_checkpoint or {})
    raw_artifacts = checkpoint.get(_ARTIFACTS_KEY)
    artifacts_present = (
        isinstance(raw_artifacts, list)
        and any(isinstance(item, Mapping) for item in raw_artifacts)
    )
    artifact_present = checkpoint.get(_ARTIFACT_KEY) is not None or artifacts_present
    is_promoting = raw_terminal_status == "succeeded"
    events: list[tuple[str, dict]] = []

    # F3a.1 — bypass detection. If the runtime ran a tool with a declared
    # contract (`contract_required=True`) but the artifact never landed on the
    # checkpoint, the runtime path is broken. We CANNOT silently promote to
    # succeeded. Treat as blocked + emit dedicated event so the bypass shows
    # up in observability.
    if is_promoting and bool(checkpoint.get("contract_required")) and not artifact_present:
        new_checkpoint = {
            **checkpoint,
            "verification_status": "blocked",
            "promote_gate_envelope": None,
            "promote_gate_reason": "contract_required_artifact_missing",
        }
        events.append(
            (
                "promote_gate_contract_bypass_detected",
                {
                    "reason": "contract_required_artifact_missing",
                    "raw_terminal_status": raw_terminal_status,
                },
            )
        )
        return ("", "blocked", new_checkpoint, events)

    try:
        gate = gate_terminal_status(
            raw_terminal_status=raw_terminal_status,
            raw_verification_status=raw_verification_status,
            completed_checkpoint=checkpoint,
        )
    except Exception as exc:  # noqa: BLE001 — must NEVER bubble out of the gate wrapper
        if artifact_present and is_promoting:
            new_checkpoint = {
                **checkpoint,
                "verification_status": "blocked",
                "promote_gate_envelope": None,
                "promote_gate_reason": "promote_gate_exception",
                "promote_gate_exception": type(exc).__name__,
            }
            events.append(
                (
                    "promote_gate_failed_closed",
                    {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:200],
                        "raw_terminal_status": raw_terminal_status,
                        "raw_verification_status": raw_verification_status,
                    },
                )
            )
            return ("", "blocked", new_checkpoint, events)
        # Legacy passthrough — no artifact, exception cannot affect promotion.
        events.append(
            (
                "promote_gate_legacy_passthrough",
                {
                    "exception_type": type(exc).__name__,
                    "reason": "no_artifact_present",
                },
            )
        )
        return (raw_terminal_status, raw_verification_status, checkpoint, events)

    if gate.degraded:
        new_checkpoint = {
            **checkpoint,
            "verification_status": gate.verification_status,
            "promote_gate_envelope": gate.envelope.to_dict() if gate.envelope else None,
            "promote_gate_reason": gate.reason,
        }
        if gate.envelopes is not None:
            new_checkpoint["promote_gate_envelopes"] = gate.envelopes
        events.append(
            (
                "promote_gate_degraded",
                {
                    "reason": gate.reason,
                    "new_terminal_status": gate.terminal_status,
                    "new_verification_status": gate.verification_status,
                },
            )
        )
        return (gate.terminal_status, gate.verification_status, new_checkpoint, events)

    if gate.envelope is not None:
        new_checkpoint = {
            **checkpoint,
            "promote_gate_envelope": gate.envelope.to_dict(),
        }
        if gate.envelopes is not None:
            new_checkpoint["promote_gate_envelopes"] = gate.envelopes
        return (gate.terminal_status, gate.verification_status, new_checkpoint, events)

    if gate.envelopes is not None:
        new_checkpoint = {
            **checkpoint,
            "promote_gate_envelopes": gate.envelopes,
        }
        return (gate.terminal_status, gate.verification_status, new_checkpoint, events)

    return (gate.terminal_status, gate.verification_status, checkpoint, events)
