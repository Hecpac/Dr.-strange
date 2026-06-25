from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence


F2_COORDINATOR_PHASES = ("research", "synthesis", "implementation", "verification")

_UNSAFE_EXTERNAL_EFFECT_STATUSES = frozenset(
    {
        "intent_recorded",
        "apply_in_progress",
        "applied",
        "failed",
        "verification_required",
        "blocked_manual_review",
    }
)
_VERIFIED_APPLIED_STATUS = "verified_applied"
_VERIFIED_ABSENT_STATUS = "verified_absent"


class F2RecoveryStatus(str, Enum):
    DISABLED = "disabled"
    COMPLETE = "complete"
    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"


@dataclass(frozen=True, slots=True)
class F2ExternalEffectBlocker:
    external_effect_id: str
    phase: str
    status: str
    reason: str
    linked_write_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class F2PhaseRecoveryDecision:
    phase: str
    status: F2RecoveryStatus
    reason: str
    latest_checkpoint_id: str | None
    latest_checkpoint_status: str | None
    last_write_order: int
    external_effect_blockers: tuple[F2ExternalEffectBlocker, ...] = ()
    verified_applied_effect_ids: tuple[str, ...] = ()
    verified_absent_effect_ids: tuple[str, ...] = ()
    external_effects_requiring_future_execution: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class F2RecoveryPlan:
    task_id: str
    run_id: str
    enabled: bool
    status: F2RecoveryStatus
    phase_decisions: tuple[F2PhaseRecoveryDecision, ...]
    next_phase: str | None
    cursor_before: Any | None
    cursor_after: Any | None
    cursor_action: str
    reasons: tuple[str, ...] = ()
    external_effect_blockers: tuple[F2ExternalEffectBlocker, ...] = ()
    external_effects_requiring_future_execution: tuple[str, ...] = ()
    will_replay_external_effects: bool = False


@dataclass(frozen=True, slots=True)
class _CursorProposal:
    phase: str
    cursor_status: str
    last_checkpoint_id: str | None
    last_write_order: int
    external_effect_id: str | None
    resume_payload: dict[str, Any]


def plan_f2_recovery(
    f2_store: Any | None,
    *,
    task_id: str,
    run_id: str | None = None,
    phases: Sequence[str] = F2_COORDINATOR_PHASES,
    persist_cursor: bool = False,
) -> F2RecoveryPlan:
    """Classify F2 durable recovery evidence without executing recovery."""
    _require_nonblank("task_id", task_id)
    resolved_run_id = run_id or task_id
    _require_nonblank("run_id", resolved_run_id)
    phase_tuple = tuple(phases)
    if not phase_tuple:
        raise ValueError("phases must not be empty")
    if f2_store is None:
        return F2RecoveryPlan(
            task_id=task_id,
            run_id=resolved_run_id,
            enabled=False,
            status=F2RecoveryStatus.DISABLED,
            phase_decisions=(),
            next_phase=None,
            cursor_before=None,
            cursor_after=None,
            cursor_action="disabled",
            reasons=("f2_durability_store_unavailable",),
            will_replay_external_effects=False,
        )

    cursor_before = f2_store.get_recovery_cursor(task_id=task_id, run_id=resolved_run_id)
    phase_decisions = tuple(
        _classify_phase(
            f2_store,
            task_id=task_id,
            run_id=resolved_run_id,
            phase=phase,
        )
        for phase in phase_tuple
    )
    external_effect_blockers = tuple(
        blocker
        for decision in phase_decisions
        for blocker in decision.external_effect_blockers
    )
    future_effects = tuple(
        effect_id
        for decision in phase_decisions
        for effect_id in decision.external_effects_requiring_future_execution
    )
    status = _overall_status(phase_decisions)
    reasons = tuple(dict.fromkeys(decision.reason for decision in phase_decisions))
    proposal = _cursor_proposal(
        task_id=task_id,
        run_id=resolved_run_id,
        status=status,
        phase_decisions=phase_decisions,
        external_effect_blockers=external_effect_blockers,
        future_effects=future_effects,
    )
    next_phase = _next_phase(phase_decisions)
    cursor_conflict = _cursor_conflict(cursor_before, proposal, phase_tuple, phase_decisions)
    cursor_after = None
    cursor_action = "not_requested"
    if cursor_conflict is not None:
        status = F2RecoveryStatus.BLOCKED
        reasons = (*reasons, cursor_conflict)
        cursor_after = cursor_before
        cursor_action = "blocked_conflict"
    elif persist_cursor:
        cursor_after = f2_store.upsert_recovery_cursor(
            task_id=task_id,
            run_id=resolved_run_id,
            phase=proposal.phase,
            cursor_status=proposal.cursor_status,
            last_checkpoint_id=proposal.last_checkpoint_id,
            last_write_order=proposal.last_write_order,
            external_effect_id=proposal.external_effect_id,
            resume_payload=proposal.resume_payload,
        )
        cursor_action = "updated"

    return F2RecoveryPlan(
        task_id=task_id,
        run_id=resolved_run_id,
        enabled=True,
        status=status,
        phase_decisions=phase_decisions,
        next_phase=next_phase,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        cursor_action=cursor_action,
        reasons=reasons,
        external_effect_blockers=external_effect_blockers,
        external_effects_requiring_future_execution=future_effects,
        will_replay_external_effects=False,
    )


def _classify_phase(
    f2_store: Any,
    *,
    task_id: str,
    run_id: str,
    phase: str,
) -> F2PhaseRecoveryDecision:
    checkpoints = f2_store.list_phase_checkpoints(
        task_id=task_id,
        run_id=run_id,
        phase=phase,
        order="phase_version_asc",
    )
    writes = f2_store.list_checkpoint_writes(
        task_id=task_id,
        run_id=run_id,
        phase=phase,
        order="write_order_asc",
    )
    effects = f2_store.list_external_effects(
        task_id=task_id,
        run_id=run_id,
        phase=phase,
        order="updated_at_asc",
    )
    linked_writes = _linked_external_effect_writes(writes)
    blockers: list[F2ExternalEffectBlocker] = []
    verified_applied: list[str] = []
    verified_absent: list[str] = []
    future_effects: list[str] = []
    for effect in effects:
        linked_write_ids = linked_writes.get(effect.external_effect_id, ())
        if not linked_write_ids:
            blockers.append(
                F2ExternalEffectBlocker(
                    external_effect_id=effect.external_effect_id,
                    phase=phase,
                    status=effect.status,
                    reason="orphaned_external_effect",
                )
            )
            continue
        if effect.status in _UNSAFE_EXTERNAL_EFFECT_STATUSES:
            blockers.append(
                F2ExternalEffectBlocker(
                    external_effect_id=effect.external_effect_id,
                    phase=phase,
                    status=effect.status,
                    reason="unsafe_external_effect_status",
                    linked_write_ids=linked_write_ids,
                )
            )
        elif effect.status == _VERIFIED_APPLIED_STATUS:
            verified_applied.append(effect.external_effect_id)
        elif effect.status == _VERIFIED_ABSENT_STATUS:
            verified_absent.append(effect.external_effect_id)
            future_effects.append(effect.external_effect_id)
        else:
            blockers.append(
                F2ExternalEffectBlocker(
                    external_effect_id=effect.external_effect_id,
                    phase=phase,
                    status=effect.status,
                    reason="unknown_external_effect_status",
                    linked_write_ids=linked_write_ids,
                )
            )

    latest = checkpoints[-1] if checkpoints else None
    max_write_order = max((write.write_order for write in writes), default=0)
    status = F2RecoveryStatus.RETRYABLE
    reason = "phase_incomplete"
    if blockers:
        status = F2RecoveryStatus.MANUAL_REVIEW_REQUIRED
        reason = "external_effect_manual_review_required"
    elif writes and latest is None:
        status = F2RecoveryStatus.BLOCKED
        reason = "checkpoint_missing_for_writes"
    elif latest is not None and latest.last_write_order > max_write_order:
        status = F2RecoveryStatus.BLOCKED
        reason = "checkpoint_references_missing_write"
    elif (
        latest is not None
        and latest.status != "started"
        and latest.last_write_order < max_write_order
    ):
        status = F2RecoveryStatus.BLOCKED
        reason = "checkpoint_missing_latest_write"
    elif latest is not None and latest.status == "succeeded":
        status = F2RecoveryStatus.COMPLETE
        reason = "phase_succeeded"
    elif latest is not None and latest.status == "started":
        status = F2RecoveryStatus.RETRYABLE
        reason = "phase_started_without_terminal_checkpoint"
    elif latest is not None:
        status = F2RecoveryStatus.BLOCKED
        reason = f"phase_checkpoint_status_{latest.status}"

    return F2PhaseRecoveryDecision(
        phase=phase,
        status=status,
        reason=reason,
        latest_checkpoint_id=latest.checkpoint_id if latest is not None else None,
        latest_checkpoint_status=latest.status if latest is not None else None,
        last_write_order=latest.last_write_order if latest is not None else max_write_order,
        external_effect_blockers=tuple(blockers),
        verified_applied_effect_ids=tuple(verified_applied),
        verified_absent_effect_ids=tuple(verified_absent),
        external_effects_requiring_future_execution=tuple(future_effects),
    )


def _linked_external_effect_writes(writes: Sequence[Any]) -> dict[str, tuple[str, ...]]:
    linked: dict[str, list[str]] = {}
    for write in writes:
        if write.external_effect_id:
            linked.setdefault(write.external_effect_id, []).append(write.write_id)
    return {effect_id: tuple(write_ids) for effect_id, write_ids in linked.items()}


def _overall_status(
    phase_decisions: Sequence[F2PhaseRecoveryDecision],
) -> F2RecoveryStatus:
    if any(decision.status is F2RecoveryStatus.MANUAL_REVIEW_REQUIRED for decision in phase_decisions):
        return F2RecoveryStatus.MANUAL_REVIEW_REQUIRED
    if any(decision.status is F2RecoveryStatus.BLOCKED for decision in phase_decisions):
        return F2RecoveryStatus.BLOCKED
    if all(decision.status is F2RecoveryStatus.COMPLETE for decision in phase_decisions):
        return F2RecoveryStatus.COMPLETE
    return F2RecoveryStatus.RETRYABLE


def _next_phase(phase_decisions: Sequence[F2PhaseRecoveryDecision]) -> str | None:
    for decision in phase_decisions:
        if decision.status is not F2RecoveryStatus.COMPLETE:
            return decision.phase
    return None


def _cursor_proposal(
    *,
    task_id: str,
    run_id: str,
    status: F2RecoveryStatus,
    phase_decisions: Sequence[F2PhaseRecoveryDecision],
    external_effect_blockers: Sequence[F2ExternalEffectBlocker],
    future_effects: Sequence[str],
) -> _CursorProposal:
    if status is F2RecoveryStatus.COMPLETE:
        decision = phase_decisions[-1]
        return _CursorProposal(
            phase=decision.phase,
            cursor_status="terminal_recovery_complete",
            last_checkpoint_id=decision.latest_checkpoint_id,
            last_write_order=decision.last_write_order,
            external_effect_id=None,
            resume_payload=_resume_payload(
                task_id=task_id,
                run_id=run_id,
                status=status,
                next_phase=None,
                future_effects=future_effects,
            ),
        )
    if external_effect_blockers:
        blocker = external_effect_blockers[0]
        decision = _decision_for_phase(phase_decisions, blocker.phase)
        return _CursorProposal(
            phase=blocker.phase,
            cursor_status="blocked_manual_review",
            last_checkpoint_id=decision.latest_checkpoint_id,
            last_write_order=decision.last_write_order,
            external_effect_id=blocker.external_effect_id,
            resume_payload=_resume_payload(
                task_id=task_id,
                run_id=run_id,
                status=status,
                next_phase=blocker.phase,
                future_effects=future_effects,
            ),
        )
    decision = next(
        (item for item in phase_decisions if item.status is not F2RecoveryStatus.COMPLETE),
        phase_decisions[-1],
    )
    previous_checkpoint_id = _previous_complete_checkpoint_id(phase_decisions, decision.phase)
    cursor_status = (
        "ready_to_resume_phase"
        if decision.latest_checkpoint_id or decision.last_write_order > 0
        else "ready_to_start_phase"
    )
    if status is F2RecoveryStatus.BLOCKED:
        cursor_status = "blocked_manual_review"
    return _CursorProposal(
        phase=decision.phase,
        cursor_status=cursor_status,
        last_checkpoint_id=decision.latest_checkpoint_id or previous_checkpoint_id,
        last_write_order=decision.last_write_order,
        external_effect_id=None,
        resume_payload=_resume_payload(
            task_id=task_id,
            run_id=run_id,
            status=status,
            next_phase=decision.phase,
            future_effects=future_effects,
        ),
    )


def _resume_payload(
    *,
    task_id: str,
    run_id: str,
    status: F2RecoveryStatus,
    next_phase: str | None,
    future_effects: Sequence[str],
) -> dict[str, Any]:
    return {
        "planner": "f2_4a_recovery_planner",
        "task_id": task_id,
        "run_id": run_id,
        "status": status.value,
        "next_phase": next_phase,
        "will_replay_external_effects": False,
        "external_effects_requiring_future_execution": list(future_effects),
    }


def _cursor_conflict(
    cursor: Any | None,
    proposal: _CursorProposal,
    phases: Sequence[str],
    phase_decisions: Sequence[F2PhaseRecoveryDecision],
) -> str | None:
    if cursor is None:
        return None
    phase_rank = {phase: rank for rank, phase in enumerate(phases)}
    if cursor.phase not in phase_rank:
        return "cursor_phase_unknown"
    if cursor.last_checkpoint_id is not None and cursor.last_checkpoint_id not in {
        decision.latest_checkpoint_id
        for decision in phase_decisions
        if decision.latest_checkpoint_id is not None
    }:
        return "cursor_checkpoint_missing"
    if cursor.cursor_status == "terminal_recovery_complete" and proposal.cursor_status != (
        "terminal_recovery_complete"
    ):
        return "terminal_cursor_without_complete_evidence"
    if phase_rank[cursor.phase] > phase_rank[proposal.phase]:
        return "cursor_ahead_of_durable_evidence"
    if cursor.phase == proposal.phase and cursor.last_write_order > proposal.last_write_order:
        return "cursor_write_order_ahead_of_durable_evidence"
    return None


def _decision_for_phase(
    phase_decisions: Sequence[F2PhaseRecoveryDecision],
    phase: str,
) -> F2PhaseRecoveryDecision:
    for decision in phase_decisions:
        if decision.phase == phase:
            return decision
    raise ValueError(f"phase not found in recovery decisions: {phase}")


def _previous_complete_checkpoint_id(
    phase_decisions: Sequence[F2PhaseRecoveryDecision],
    phase: str,
) -> str | None:
    previous: str | None = None
    for decision in phase_decisions:
        if decision.phase == phase:
            return previous
        if decision.status is F2RecoveryStatus.COMPLETE:
            previous = decision.latest_checkpoint_id
    return previous


def _require_nonblank(name: str, value: str) -> None:
    if not str(value or "").strip():
        raise ValueError(f"{name} is required")
