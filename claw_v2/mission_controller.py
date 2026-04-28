"""Mission Controller — mantiene viva una misión hasta succeeded /
blocked / awaiting_approval / failed / interrupted.

Persistencia MVP (sin migración DB): anida bajo
`session_state.active_object["_mission"]`. NO pisa el contrato
`{kind:"notebook", id, title}` que NlmHandler usa.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal


MissionStatus = Literal[
    "planning",
    "routing",
    "executing",
    "collecting_evidence",
    "verifying",
    "awaiting_approval",
    "blocked",
    "interrupted",
    "succeeded",
    "failed",
]


_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed"})


@dataclass(slots=True)
class MissionRecord:
    mission_id: str
    session_id: str
    objective: str
    task_kind: str
    route: str
    status: MissionStatus
    phase: str = "planning"
    required_capabilities: list[str] = field(default_factory=list)
    evidence_required: list[str] = field(default_factory=list)
    evidence_collected: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    approval_id: str | None = None
    blocked_reason: str | None = None
    retry_count: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


def _new_mission_id() -> str:
    return f"m-{uuid.uuid4().hex[:12]}"


class MissionController:
    """Lifecycle controller for missions, persisted in session_state.

    Storage strategy: nested under ``active_object["_mission"]`` to avoid
    touching the schema. Reader and writer both use the existing
    ``MemoryStore.update_session_state`` / ``get_session_state`` callables.
    """

    def __init__(
        self,
        *,
        get_session_state: Callable[[str], dict[str, Any]],
        update_session_state: Callable[..., None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._get_state = get_session_state
        self._update_state = update_session_state
        self._clock = clock

    def _read_active_object(self, session_id: str) -> dict[str, Any]:
        try:
            state = self._get_state(session_id) or {}
        except Exception:
            state = {}
        active = state.get("active_object") or {}
        if not isinstance(active, dict):
            return {}
        return dict(active)

    def _persist_mission(
        self, session_id: str, mission: MissionRecord | None
    ) -> None:
        active = self._read_active_object(session_id)
        if mission is None:
            active.pop("_mission", None)
        else:
            active["_mission"] = asdict(mission)
        self._update_state(session_id, active_object=active)

    def latest_relevant(self, session_id: str) -> MissionRecord | None:
        active = self._read_active_object(session_id)
        raw = active.get("_mission")
        if not isinstance(raw, dict):
            return None
        try:
            mission = MissionRecord(**raw)
        except TypeError:
            return None
        if mission.is_terminal():
            return None
        return mission

    def start_or_resume(
        self,
        *,
        session_id: str,
        objective: str,
        task_kind: str,
        route: str,
        required_capabilities: list[str] | None = None,
        evidence_required: list[str] | None = None,
    ) -> MissionRecord:
        existing = self.latest_relevant(session_id)
        if existing is not None and existing.task_kind == task_kind:
            existing.objective = objective or existing.objective
            existing.route = route or existing.route
            existing.status = (
                "executing" if existing.status == "interrupted" else existing.status
            )
            existing.updated_at = self._clock()
            self._persist_mission(session_id, existing)
            return existing
        now = self._clock()
        mission = MissionRecord(
            mission_id=_new_mission_id(),
            session_id=session_id,
            objective=objective,
            task_kind=task_kind,
            route=route,
            status="planning",
            phase="planning",
            required_capabilities=list(required_capabilities or []),
            evidence_required=list(evidence_required or []),
            created_at=now,
            updated_at=now,
        )
        self._persist_mission(session_id, mission)
        return mission

    def record_evidence(
        self,
        mission_id: str,
        *,
        session_id: str,
        evidence: dict[str, Any],
    ) -> MissionRecord | None:
        mission = self.latest_relevant(session_id)
        if mission is None or mission.mission_id != mission_id:
            return None
        mission.evidence_collected.update(evidence)
        mission.updated_at = self._clock()
        if mission.status == "planning":
            mission.status = "collecting_evidence"
            mission.phase = "collecting_evidence"
        self._persist_mission(session_id, mission)
        return mission

    def mark_blocked(
        self,
        mission_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> MissionRecord | None:
        mission = self.latest_relevant(session_id)
        if mission is None or mission.mission_id != mission_id:
            return None
        mission.status = "blocked"
        mission.phase = "blocked"
        mission.blocked_reason = reason
        mission.updated_at = self._clock()
        self._persist_mission(session_id, mission)
        return mission

    def mark_interrupted(
        self,
        mission_id: str,
        *,
        session_id: str,
        reason: str = "interrupted",
    ) -> MissionRecord | None:
        mission = self.latest_relevant(session_id)
        if mission is None or mission.mission_id != mission_id:
            return None
        mission.status = "interrupted"
        mission.phase = "interrupted"
        mission.blocked_reason = reason
        mission.updated_at = self._clock()
        self._persist_mission(session_id, mission)
        return mission

    def complete_if_verified(
        self,
        mission_id: str,
        *,
        session_id: str,
    ) -> MissionRecord | None:
        mission = self.latest_relevant(session_id)
        if mission is None or mission.mission_id != mission_id:
            return None
        missing = [
            key
            for key in mission.evidence_required
            if key not in mission.evidence_collected
        ]
        if missing:
            return mission  # not complete yet
        mission.status = "succeeded"
        mission.phase = "succeeded"
        mission.updated_at = self._clock()
        self._persist_mission(session_id, mission)
        return mission

    def fail(
        self,
        mission_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> MissionRecord | None:
        mission = self.latest_relevant(session_id)
        if mission is None or mission.mission_id != mission_id:
            return None
        mission.status = "failed"
        mission.phase = "failed"
        mission.blocked_reason = reason
        mission.updated_at = self._clock()
        self._persist_mission(session_id, mission)
        return mission
