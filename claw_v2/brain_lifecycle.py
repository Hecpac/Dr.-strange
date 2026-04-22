from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from claw_v2.learning import LearningLoop
from claw_v2.memory import MemoryStore
from claw_v2.observe import ObserveStream

if TYPE_CHECKING:
    from claw_v2.checkpoint import CheckpointService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VerificationOutcomeRecorder:
    memory: MemoryStore
    observe: ObserveStream | None = None
    learning: LearningLoop | None = None
    checkpoint: "CheckpointService | None" = None

    def record(
        self,
        *,
        session_id: str,
        task_type: str,
        goal: str,
        action_summary: str,
        verification_status: str,
        error_snippet: str | None,
        predicted_confidence: float | None = None,
    ) -> None:
        if self.observe is not None:
            self.observe.emit(
                "cycle_verification_complete",
                payload={
                    "session_id": session_id,
                    "task_type": task_type,
                    "verification_status": verification_status,
                    "had_error": bool(error_snippet),
                    "predicted_confidence": predicted_confidence,
                },
            )
        if self.learning is not None:
            try:
                self.learning.record_cycle_outcome(
                    session_id=session_id,
                    task_type=task_type,
                    goal=goal,
                    action_summary=action_summary,
                    verification_status=verification_status,
                    error_snippet=error_snippet,
                    predicted_confidence=predicted_confidence,
                )
            except Exception:
                logger.warning("Auto post-mortem recording failed", exc_info=True)

        if self.checkpoint is None:
            return
        try:
            consecutive = _count_recent_consecutive_failures(
                self.memory,
                task_type=task_type,
                session_id=session_id,
                within_minutes=30,
            )
        except Exception:
            logger.debug("Failure count probe failed", exc_info=True)
            return
        if consecutive < 3:
            return
        latest = self.checkpoint.latest()
        autonomy_mode = (
            self.memory.get_session_state(session_id).get("autonomy_mode", "assisted")
            if session_id else "assisted"
        )
        if latest is None:
            if self.observe is not None:
                self.observe.emit(
                    "auto_rollback_unavailable",
                    payload={
                        "session_id": session_id,
                        "consecutive_failures": consecutive,
                        "autonomy_mode": autonomy_mode,
                    },
                )
            return
        if self.observe is not None:
            self.observe.emit(
                "auto_rollback_proposed",
                payload={
                    "ckpt_id": latest["ckpt_id"],
                    "consecutive_failures": consecutive,
                    "session_id": session_id,
                    "autonomy_mode": autonomy_mode,
                },
            )
        if autonomy_mode == "autonomous":
            try:
                self.checkpoint.schedule_restore(latest["ckpt_id"])
            except Exception:
                logger.warning("schedule_restore failed", exc_info=True)


def _count_recent_consecutive_failures(
    memory: MemoryStore,
    *,
    task_type: str | None,
    session_id: str | None,
    within_minutes: int = 30,
) -> int:
    rows = memory.recent_outcomes_within(
        within_minutes=within_minutes,
        task_type=task_type,
        session_id=session_id,
        limit=20,
    )
    count = 0
    for row in rows:
        if row["outcome"] == "failure":
            count += 1
        else:
            break
    return count
