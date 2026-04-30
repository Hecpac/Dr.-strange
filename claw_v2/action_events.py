from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claw_v2.redaction import redact_sensitive
from claw_v2.telemetry import append_jsonl, generate_id, latest_by_id, now_iso, read_jsonl

ACTION_EVENT_SCHEMA_VERSION = "action_event.v1"

ActionEventType = Literal[
    "goal_initialized",
    "goal_updated",
    "goal_completed",
    "claim_recorded",
    "evidence_linked",
    "action_proposed",
    "action_executed",
    "action_failed",
    "risk_escalated",
    "critic_review_requested",
    "critic_decision_received",
    "stop_condition_triggered",
    "recall_requested",
    "recall_result_recorded",
    "gdi_snapshot",
]
Actor = Literal["claw", "critic", "user", "scheduler", "external"]
ActionTier = Literal["tier_1", "tier_2", "tier_2_5", "tier_3"]
RiskLevel = Literal["low", "medium", "high", "critical"]
ActionStatus = Literal["success", "failure", "pending", "skipped", "blocked"]

ACTION_EVENT_TYPES = frozenset(ActionEventType.__args__)  # type: ignore[attr-defined]
ACTORS = frozenset(Actor.__args__)  # type: ignore[attr-defined]
ACTION_TIERS = frozenset(ActionTier.__args__)  # type: ignore[attr-defined]
RISK_LEVELS = frozenset(RiskLevel.__args__)  # type: ignore[attr-defined]
ACTION_STATUSES = frozenset(ActionStatus.__args__)  # type: ignore[attr-defined]


@dataclass(slots=True)
class ProposedAction:
    tool: str
    args_redacted: dict[str, Any] = field(default_factory=dict)
    tier: ActionTier = "tier_1"
    rationale_brief: str = ""

    def __post_init__(self) -> None:
        if not self.tool.strip():
            raise ValueError("proposed action tool is required")
        if self.tier not in ACTION_TIERS:
            raise ValueError(f"invalid action tier: {self.tier}")
        if len(self.rationale_brief) > 240:
            raise ValueError("rationale_brief must be <= 240 chars")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args_redacted": redact_sensitive(dict(self.args_redacted), limit=1000),
            "tier": self.tier,
            "rationale_brief": self.rationale_brief,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProposedAction":
        return cls(
            tool=str(data["tool"]),
            args_redacted=dict(data.get("args_redacted") or {}),
            tier=str(data.get("tier") or "tier_1"),  # type: ignore[arg-type]
            rationale_brief=str(data.get("rationale_brief") or ""),
        )


@dataclass(slots=True)
class ActionResult:
    status: ActionStatus
    output_hash: str = ""
    error: str | None = None
    external_blob_ref: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ACTION_STATUSES:
            raise ValueError(f"invalid action status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output_hash": self.output_hash,
            "error": self.error,
            "external_blob_ref": self.external_blob_ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionResult":
        return cls(
            status=str(data["status"]),  # type: ignore[arg-type]
            output_hash=str(data.get("output_hash") or ""),
            error=_optional_str(data.get("error")),
            external_blob_ref=_optional_str(data.get("external_blob_ref")),
        )


@dataclass(slots=True)
class ActionEvent:
    event_id: str
    event_type: ActionEventType
    actor: Actor
    goal_id: str
    session_id: str
    goal_revision: int = 1
    originating_event_id: str | None = None
    proposed_next_action: ProposedAction | None = None
    risk_level: RiskLevel = "low"
    claims: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    result: ActionResult | None = None
    timestamp: str = field(default_factory=now_iso)
    schema_version: str = ACTION_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.event_type not in ACTION_EVENT_TYPES:
            raise ValueError(f"invalid event_type: {self.event_type}")
        if self.actor not in ACTORS:
            raise ValueError(f"invalid actor: {self.actor}")
        if not self.goal_id.strip():
            raise ValueError("goal_id is required")
        if self.goal_revision < 1:
            raise ValueError("goal_revision must be >= 1")
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"invalid risk_level: {self.risk_level}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "originating_event_id": self.originating_event_id,
            "session_id": self.session_id,
            "proposed_next_action": (
                self.proposed_next_action.to_dict() if self.proposed_next_action is not None else None
            ),
            "risk_level": self.risk_level,
            "claims": list(self.claims),
            "evidence_refs": list(self.evidence_refs),
            "result": self.result.to_dict() if self.result is not None else None,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionEvent":
        proposed = data.get("proposed_next_action")
        result = data.get("result")
        return cls(
            event_id=str(data["event_id"]),
            event_type=str(data["event_type"]),  # type: ignore[arg-type]
            actor=str(data["actor"]),  # type: ignore[arg-type]
            goal_id=str(data["goal_id"]),
            goal_revision=int(data.get("goal_revision") or 1),
            originating_event_id=_optional_str(data.get("originating_event_id")),
            session_id=str(data["session_id"]),
            proposed_next_action=ProposedAction.from_dict(proposed) if isinstance(proposed, dict) else None,
            risk_level=str(data.get("risk_level") or "low"),  # type: ignore[arg-type]
            claims=[str(item) for item in data.get("claims", [])],
            evidence_refs=[str(item) for item in data.get("evidence_refs", [])],
            result=ActionResult.from_dict(result) if isinstance(result, dict) else None,
            timestamp=str(data.get("timestamp") or now_iso()),
            schema_version=str(data.get("schema_version") or ACTION_EVENT_SCHEMA_VERSION),
        )


def emit_event(
    telemetry_root: Path | str,
    *,
    event_type: ActionEventType,
    actor: Actor,
    goal_id: str,
    session_id: str,
    goal_revision: int | None = None,
    originating_event_id: str | None = None,
    proposed_next_action: ProposedAction | dict[str, Any] | None = None,
    risk_level: RiskLevel = "low",
    claims: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    result: ActionResult | dict[str, Any] | None = None,
    observe: Any | None = None,
) -> ActionEvent:
    event = ActionEvent(
        event_id=generate_id("e"),
        event_type=event_type,
        actor=actor,
        goal_id=goal_id,
        goal_revision=goal_revision or _latest_goal_revision(telemetry_root, goal_id),
        originating_event_id=originating_event_id,
        session_id=session_id,
        proposed_next_action=_coerce_proposed_action(proposed_next_action),
        risk_level=risk_level,
        claims=list(claims or []),
        evidence_refs=list(evidence_refs or []),
        result=_coerce_action_result(result),
        timestamp=now_iso(),
    )
    payload = event.to_dict()
    append_jsonl(_events_path(telemetry_root), payload)
    if observe is not None:
        observe.emit(event.event_type, payload=payload)
    return event


def load_events(telemetry_root: Path | str) -> list[ActionEvent]:
    return [ActionEvent.from_dict(row) for row in read_jsonl(_events_path(telemetry_root))]


def recover_orphan_actions(telemetry_root: Path | str, *, observe: Any | None = None) -> int:
    events_path = _events_path(telemetry_root)
    if not events_path.exists():
        return 0

    proposed: dict[str, ActionEvent] = {}
    finalized: set[str] = set()
    for event in load_events(telemetry_root):
        if event.event_type == "action_proposed":
            proposed[event.event_id] = event
        elif event.event_type in {"action_executed", "action_failed"} and event.originating_event_id:
            finalized.add(event.originating_event_id)

    count = 0
    for event_id, event in proposed.items():
        if event_id in finalized:
            continue
        claim_id = _record_restart_risk_signal(telemetry_root, event, observe=observe)
        emit_event(
            telemetry_root,
            event_type="action_failed",
            actor=event.actor,
            goal_id=event.goal_id,
            goal_revision=event.goal_revision,
            originating_event_id=event.event_id,
            session_id=event.session_id,
            proposed_next_action=event.proposed_next_action,
            risk_level=event.risk_level,
            claims=[claim_id] if claim_id else [],
            result=ActionResult(
                status="failure",
                output_hash="",
                error="interrupted_by_restart",
                external_blob_ref=None,
            ),
            observe=observe,
        )
        count += 1
    return count


def _events_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "events.jsonl"


def _goals_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "goals.jsonl"


def _latest_goal_revision(telemetry_root: Path | str, goal_id: str) -> int:
    latest = latest_by_id(_goals_path(telemetry_root), "goal_id").get(goal_id)
    if latest is None:
        return 1
    return int(latest.get("goal_revision") or 1)


def _record_restart_risk_signal(telemetry_root: Path | str, event: ActionEvent, *, observe: Any | None) -> str | None:
    if event.proposed_next_action is None:
        return None
    from claw_v2.evidence_ledger import record_claim

    claim = record_claim(
        telemetry_root,
        goal_id=event.goal_id,
        claim_text=f"Action {event.proposed_next_action.tool} interrupted by restart, state unknown",
        claim_type="risk_signal",
        verification_status="unverified",
        confidence=0.0,
        observe=observe,
    )
    return claim.claim_id


def _coerce_proposed_action(value: ProposedAction | dict[str, Any] | None) -> ProposedAction | None:
    if value is None or isinstance(value, ProposedAction):
        return value
    return ProposedAction.from_dict(value)


def _coerce_action_result(value: ActionResult | dict[str, Any] | None) -> ActionResult | None:
    if value is None or isinstance(value, ActionResult):
        return value
    return ActionResult.from_dict(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
