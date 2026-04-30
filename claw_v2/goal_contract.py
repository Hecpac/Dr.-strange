from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claw_v2.telemetry import append_jsonl, generate_id, latest_by_id, now_iso, read_jsonl

GoalRiskProfile = Literal["tier_1", "tier_2", "tier_2_5", "tier_3"]
GOAL_SCHEMA_VERSION = "goal_contract.v1"
GOAL_RISK_PROFILES = frozenset({"tier_1", "tier_2", "tier_2_5", "tier_3"})


@dataclass(slots=True)
class GoalContract:
    goal_id: str
    objective: str
    goal_revision: int = 1
    constraints: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    disallowed_actions: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    risk_profile: GoalRiskProfile = "tier_1"
    anchor_source: str = "manual"
    parent_goal_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    schema_version: str = GOAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.objective.strip():
            raise ValueError("objective is required")
        if self.goal_revision < 1:
            raise ValueError("goal_revision must be >= 1")
        if self.risk_profile not in GOAL_RISK_PROFILES:
            raise ValueError(f"invalid risk_profile: {self.risk_profile}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "assumptions": list(self.assumptions),
            "allowed_actions": list(self.allowed_actions),
            "disallowed_actions": list(self.disallowed_actions),
            "success_criteria": list(self.success_criteria),
            "stop_conditions": list(self.stop_conditions),
            "risk_profile": self.risk_profile,
            "anchor_source": self.anchor_source,
            "parent_goal_id": self.parent_goal_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalContract":
        return cls(
            goal_id=str(data["goal_id"]),
            objective=str(data["objective"]),
            goal_revision=int(data.get("goal_revision") or 1),
            constraints=_str_list(data.get("constraints")),
            assumptions=_str_list(data.get("assumptions")),
            allowed_actions=_str_list(data.get("allowed_actions")),
            disallowed_actions=_str_list(data.get("disallowed_actions")),
            success_criteria=_str_list(data.get("success_criteria")),
            stop_conditions=_str_list(data.get("stop_conditions")),
            risk_profile=str(data.get("risk_profile") or "tier_1"),  # type: ignore[arg-type]
            anchor_source=str(data.get("anchor_source") or "manual"),
            parent_goal_id=_optional_str(data.get("parent_goal_id")),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            schema_version=str(data.get("schema_version") or GOAL_SCHEMA_VERSION),
        )


def create_goal(
    telemetry_root: Path | str,
    *,
    objective: str,
    constraints: list[str] | None = None,
    assumptions: list[str] | None = None,
    allowed_actions: list[str] | None = None,
    disallowed_actions: list[str] | None = None,
    success_criteria: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    risk_profile: GoalRiskProfile = "tier_1",
    anchor_source: str = "manual",
    parent_goal_id: str | None = None,
    observe: Any | None = None,
) -> GoalContract:
    now = now_iso()
    contract = GoalContract(
        goal_id=generate_id("g"),
        objective=objective,
        constraints=list(constraints or []),
        assumptions=list(assumptions or []),
        allowed_actions=list(allowed_actions or []),
        disallowed_actions=list(disallowed_actions or []),
        success_criteria=list(success_criteria or []),
        stop_conditions=list(stop_conditions or []),
        risk_profile=risk_profile,
        anchor_source=anchor_source,
        parent_goal_id=parent_goal_id,
        created_at=now,
        updated_at=now,
    )
    append_jsonl(_goals_path(telemetry_root), contract.to_dict())
    if observe is not None:
        observe.emit("goal_initialized", payload=contract.to_dict())
    return contract


def update_goal(
    telemetry_root: Path | str,
    goal: GoalContract,
    *,
    observe: Any | None = None,
    session_id: str = "runtime",
    **updates: Any,
) -> GoalContract:
    if _is_goal_completed(telemetry_root, goal.goal_id):
        raise ValueError(f"goal {goal.goal_id} is completed and cannot be updated")
    latest = latest_by_id(_goals_path(telemetry_root), "goal_id").get(goal.goal_id)
    data = dict(latest or goal.to_dict())
    for key, value in updates.items():
        if value is not None:
            data[key] = value
    data["goal_revision"] = int(data.get("goal_revision") or 1) + 1
    data["updated_at"] = now_iso()
    updated = GoalContract.from_dict(data)
    append_jsonl(_goals_path(telemetry_root), updated.to_dict())
    from claw_v2.action_events import emit_event

    emit_event(
        telemetry_root,
        event_type="goal_updated",
        actor="claw",
        goal_id=updated.goal_id,
        goal_revision=updated.goal_revision,
        session_id=session_id,
        risk_level="low",
        observe=None,
    )
    if observe is not None:
        observe.emit("goal_updated", payload=updated.to_dict())
    return updated


def load_goals(telemetry_root: Path | str) -> list[GoalContract]:
    return [GoalContract.from_dict(row) for row in read_jsonl(_goals_path(telemetry_root))]


def _goals_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "goals.jsonl"


def _events_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "events.jsonl"


def _is_goal_completed(telemetry_root: Path | str, goal_id: str) -> bool:
    for event in read_jsonl(_events_path(telemetry_root)):
        if event.get("event_type") == "goal_completed" and event.get("goal_id") == goal_id:
            return True
    return False


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
