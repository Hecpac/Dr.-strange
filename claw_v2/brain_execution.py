from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from claw_v2.approval import ApprovalManager
from claw_v2.brain_lifecycle import VerificationOutcomeRecorder
from claw_v2.observe import ObserveStream
from claw_v2.types import CriticalActionExecution, CriticalActionVerification

if TYPE_CHECKING:
    from claw_v2.checkpoint import CheckpointService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SnapshotOrchestrator:
    checkpoint: "CheckpointService | None" = None

    def pre_snapshot(self, *, action: str, session_id: str | None = None) -> str | None:
        if self.checkpoint is None:
            return None
        try:
            return self.checkpoint.create(
                trigger_reason=f"pre-critical-action:{action[:80]}",
                session_id=session_id,
            )
        except Exception:
            logger.warning("Pre-action checkpoint failed", exc_info=True)
            return None


@dataclass(slots=True)
class ExecutionEventEmitter:
    observe: ObserveStream | None
    outcomes: VerificationOutcomeRecorder
    predicted_confidence: float | None = None

    def emit(
        self,
        *,
        action: str,
        verification: CriticalActionVerification,
        status: str,
        approval_status: str | None,
        checkpoint_id: str | None = None,
    ) -> None:
        if self.observe is None or verification.response is None:
            return
        self.observe.emit(
            "critical_action_execution",
            lane=verification.response.lane,
            provider=verification.response.provider,
            model=verification.response.model,
            payload={
                "action": action,
                "status": status,
                "approval_status": approval_status,
                "recommendation": verification.recommendation,
                "risk_level": verification.risk_level,
                "requires_human_approval": verification.requires_human_approval,
                "should_proceed": verification.should_proceed,
                "approval_id": verification.approval_id,
                "checkpoint_id": checkpoint_id,
            },
        )
        status_map = {
            "executed": "ok",
            "executed_autonomously": "ok",
            "executed_with_approval": "ok",
            "blocked": "failed",
            "aborted_by_pre_check": "failed",
            "awaiting_approval": "pending",
        }
        mapped_status = status_map.get(status, status)
        error_snippet = verification.summary if mapped_status == "failed" else None
        self.outcomes.record(
            session_id="brain.critical_action",
            task_type="critical_action",
            goal=action,
            action_summary=(verification.summary or verification.recommendation or action),
            verification_status=mapped_status,
            error_snippet=error_snippet,
            predicted_confidence=self.predicted_confidence,
        )


@dataclass(slots=True)
class ExecutionGatingService:
    approvals: ApprovalManager | None = None
    snapshots: SnapshotOrchestrator = field(default_factory=SnapshotOrchestrator)
    events: ExecutionEventEmitter | None = None

    def execute(
        self,
        *,
        action: str,
        plan: str,
        diff: str,
        test_output: str,
        executor: Callable[[], Any],
        verify_action: Callable[..., CriticalActionVerification],
        autonomy_mode: str = "assisted",
        approval_id: str | None = None,
        pre_check: Callable[[CriticalActionVerification], bool] | None = None,
    ) -> CriticalActionExecution:
        approval_status, approval_override = self._approval_override(approval_id)
        verification = verify_action(
            plan=plan,
            diff=diff,
            test_output=test_output,
            action=action,
            create_approval=not approval_override,
        )
        if approval_override:
            approval_status, approval_override = self._approval_override(approval_id)

        if pre_check is not None and not pre_check(verification):
            self._emit(action, verification, "aborted_by_pre_check", approval_status)
            return CriticalActionExecution(
                action=action,
                status="aborted_by_pre_check",
                executed=False,
                verification=verification,
                reason="Pre-execution check rejected the action.",
                approval_status=approval_status,
            )

        if autonomy_mode == "autonomous" and verification.should_proceed and verification.risk_level in {"low", "medium"}:
            return self._execute_now(action, executor, verification, "executed_autonomously", approval_status)
        if verification.should_proceed:
            return self._execute_now(action, executor, verification, "executed", approval_status)
        if approval_override:
            return self._execute_now(
                action,
                executor,
                verification,
                "executed_with_approval",
                approval_status,
                reason="human approval override",
            )
        if verification.requires_human_approval:
            status = "awaiting_approval" if self.approvals is not None else "blocked"
            self._emit(action, verification, status, approval_status)
            return CriticalActionExecution(
                action=action,
                status=status,
                executed=False,
                verification=verification,
                reason=verification.summary,
                approval_status=approval_status,
            )

        self._emit(action, verification, "blocked", approval_status)
        return CriticalActionExecution(
            action=action,
            status="blocked",
            executed=False,
            verification=verification,
            reason=verification.summary,
            approval_status=approval_status,
        )

    def _execute_now(
        self,
        action: str,
        executor: Callable[[], Any],
        verification: CriticalActionVerification,
        status: str,
        approval_status: str | None,
        *,
        reason: str | None = None,
    ) -> CriticalActionExecution:
        ckpt_id = self.snapshots.pre_snapshot(action=action)
        result = executor()
        self._emit(action, verification, status, approval_status, checkpoint_id=ckpt_id)
        return CriticalActionExecution(
            action=action,
            status=status,
            executed=True,
            verification=verification,
            result=result,
            reason=reason,
            approval_status=approval_status,
            checkpoint_id=ckpt_id,
        )

    def _approval_override(self, approval_id: str | None) -> tuple[str | None, bool]:
        approval_status = None
        if approval_id is not None and self.approvals is not None:
            try:
                approval_status = self.approvals.status(approval_id)
            except FileNotFoundError:
                approval_status = "missing"
        return approval_status, approval_status == "approved"

    def _emit(
        self,
        action: str,
        verification: CriticalActionVerification,
        status: str,
        approval_status: str | None,
        checkpoint_id: str | None = None,
    ) -> None:
        if self.events is not None:
            self.events.emit(
                action=action,
                verification=verification,
                status=status,
                approval_status=approval_status,
                checkpoint_id=checkpoint_id,
            )
