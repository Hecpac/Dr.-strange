from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from claw_v2.action_events import ActionEvent, ProposedAction
from claw_v2.evidence_ledger import Claim
from claw_v2.goal_contract import GoalContract
from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl

GDI_SCHEMA_VERSION = "gdi_snapshot.v1"
GDIBand = Literal["continue", "caution", "critic_required", "stop"]
GDIGateAction = Literal["allow", "log", "recall_recommended", "critic_required", "block"]


@dataclass(slots=True)
class GDISignal:
    name: str
    value: Any
    weight: float

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "weight": round(float(self.weight), 4)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GDISignal":
        return cls(
            name=str(data["name"]),
            value=data.get("value"),
            weight=float(data.get("weight") or 0.0),
        )


@dataclass(slots=True)
class GDISnapshot:
    snapshot_id: str
    goal_id: str
    session_id: str
    gdi_score: float
    band: GDIBand
    signals: list[GDISignal] = field(default_factory=list)
    reason_summary: str = ""
    computed_at: str = field(default_factory=now_iso)
    schema_version: str = GDI_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "gdi_score": round(float(self.gdi_score), 4),
            "band": self.band,
            "signals": [signal.to_dict() for signal in self.signals],
            "reason_summary": self.reason_summary[:240],
            "computed_at": self.computed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GDISnapshot":
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            goal_id=str(data["goal_id"]),
            session_id=str(data["session_id"]),
            gdi_score=float(data.get("gdi_score") or 0.0),
            band=str(data.get("band") or "continue"),  # type: ignore[arg-type]
            signals=[GDISignal.from_dict(item) for item in data.get("signals", [])],
            reason_summary=str(data.get("reason_summary") or ""),
            computed_at=str(data.get("computed_at") or now_iso()),
            schema_version=str(data.get("schema_version") or GDI_SCHEMA_VERSION),
        )


@dataclass(slots=True)
class GDIGateDecision:
    action: GDIGateAction
    allowed: bool
    reason: str
    snapshot_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "allowed": self.allowed,
            "reason": self.reason,
            "snapshot_id": self.snapshot_id,
        }


def calculate_gdi_snapshot(
    goal: GoalContract,
    *,
    session_id: str,
    proposed_next_action: ProposedAction | dict[str, Any] | None = None,
    recent_events: list[ActionEvent] | None = None,
    claims: list[Claim] | None = None,
    workspace_root: Path | str | None = None,
) -> GDISnapshot:
    action = _coerce_action(proposed_next_action)
    events = list(recent_events or [])
    claim_rows = list(claims or [])
    signals: list[GDISignal] = []

    if action is not None:
        if action.tool in set(goal.disallowed_actions):
            signals.append(GDISignal("tool_in_disallowed", True, 0.4))
        if goal.allowed_actions and action.tool not in set(goal.allowed_actions):
            signals.append(GDISignal("tool_not_in_allowed_actions", action.tool, 0.15))
        constraint_hit = _constraint_contradiction(goal.constraints, action)
        if constraint_hit:
            signals.append(GDISignal("constraint_contradiction", constraint_hit, 0.3))
        if workspace_root is not None and _has_workspace_escape(action.args_redacted, Path(workspace_root)):
            signals.append(GDISignal("workspace_escape", True, 0.35))

    failures = _consecutive_failures(events)
    if failures:
        signals.append(GDISignal("consecutive_failures", failures, min(0.2, failures * 0.05)))

    escalations = sum(1 for event in events if event.event_type == "risk_escalated")
    if escalations:
        signals.append(GDISignal("risk_escalations", escalations, min(0.2, escalations * 0.1)))

    unverified = [
        claim for claim in claim_rows
        if claim.claim_type in {"fact", "inference", "risk_signal"} and claim.verification_status != "verified"
    ]
    if claim_rows and unverified:
        ratio = len(unverified) / max(len(claim_rows), 1)
        signals.append(GDISignal("unverified_claim_ratio", round(ratio, 3), min(0.2, ratio * 0.2)))

    contradictions = [claim for claim in claim_rows if claim.verification_status == "contradicted"]
    if contradictions:
        signals.append(GDISignal("contradicted_claims", len(contradictions), 0.25))

    score = min(1.0, sum(max(0.0, signal.weight) for signal in signals))
    band = band_for_score(score)
    return GDISnapshot(
        snapshot_id=generate_id("gdi"),
        goal_id=goal.goal_id,
        session_id=session_id,
        gdi_score=score,
        band=band,
        signals=signals,
        reason_summary=_reason_summary(signals, band),
        computed_at=now_iso(),
    )


def record_gdi_snapshot(
    telemetry_root: Path | str,
    snapshot: GDISnapshot,
    *,
    observe: Any | None = None,
) -> GDISnapshot:
    payload = snapshot.to_dict()
    append_jsonl(_gdi_path(telemetry_root), payload)
    if observe is not None:
        observe.emit("gdi_snapshot", payload=payload)
    return snapshot


def load_gdi_snapshots(telemetry_root: Path | str) -> list[GDISnapshot]:
    return [GDISnapshot.from_dict(row) for row in read_jsonl(_gdi_path(telemetry_root))]


def gate_gdi_action(
    snapshot: GDISnapshot,
    *,
    action_tier: str,
    risk_level: str,
    calibrated: bool = False,
) -> GDIGateDecision:
    if not calibrated:
        return GDIGateDecision(
            action="log",
            allowed=True,
            reason="GDI is running in log-only calibration mode.",
            snapshot_id=snapshot.snapshot_id,
        )
    tier_sensitive = action_tier in {"tier_2", "tier_2_5", "tier_3"} or risk_level in {"high", "critical"}
    if snapshot.band == "stop":
        return GDIGateDecision("block", False, "GDI stop band requires human review.", snapshot.snapshot_id)
    if snapshot.band == "critic_required" and tier_sensitive:
        return GDIGateDecision("critic_required", False, "GDI requires Critic review before this action.", snapshot.snapshot_id)
    if snapshot.band == "caution" and (action_tier in {"tier_2_5", "tier_3"} or risk_level == "critical"):
        return GDIGateDecision("recall_recommended", True, "Run Active Recall before proceeding.", snapshot.snapshot_id)
    return GDIGateDecision("allow", True, "GDI band allows the action.", snapshot.snapshot_id)


def band_for_score(score: float) -> GDIBand:
    if score >= 0.75:
        return "stop"
    if score >= 0.5:
        return "critic_required"
    if score >= 0.25:
        return "caution"
    return "continue"


def _gdi_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "gdi.jsonl"


def _coerce_action(value: ProposedAction | dict[str, Any] | None) -> ProposedAction | None:
    if value is None or isinstance(value, ProposedAction):
        return value
    return ProposedAction.from_dict(value)


def _consecutive_failures(events: list[ActionEvent]) -> int:
    count = 0
    for event in reversed(events):
        if event.event_type == "action_failed":
            count += 1
            continue
        if event.event_type in {"action_executed", "action_proposed"}:
            break
    return count


def _constraint_contradiction(constraints: list[str], action: ProposedAction) -> str:
    text = f"{action.tool} {action.rationale_brief} {action.args_redacted}".lower()
    for constraint in constraints:
        lowered = constraint.lower()
        for marker in ("no ", "never ", "sin "):
            if marker not in lowered:
                continue
            forbidden = lowered.split(marker, maxsplit=1)[1].strip(" .,:;")
            if forbidden and forbidden in text:
                return constraint
        if "force-push" in lowered and "force" in text and "push" in text:
            return constraint
    return ""


def _has_workspace_escape(value: Any, workspace_root: Path) -> bool:
    try:
        root = workspace_root.expanduser().resolve()
    except OSError:
        return False
    for candidate in _iter_string_values(value):
        if not (candidate.startswith("/") or candidate.startswith("~")):
            continue
        try:
            path = Path(candidate).expanduser().resolve()
        except OSError:
            continue
        if path != root and root not in path.parents:
            return True
    return False


def _iter_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_iter_string_values(item))
        return out
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            out.extend(_iter_string_values(item))
        return out
    return []


def _reason_summary(signals: list[GDISignal], band: GDIBand) -> str:
    if not signals:
        return "No drift signals detected."
    names = ", ".join(signal.name for signal in sorted(signals, key=lambda item: item.weight, reverse=True)[:4])
    return f"{band}: {names}"[:240]

