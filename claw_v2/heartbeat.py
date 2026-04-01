from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from claw_v2.agents import FileAgentStore
from claw_v2.approval import ApprovalManager
from claw_v2.metrics import MetricsTracker
from claw_v2.observe import ObserveStream


@dataclass(slots=True)
class HeartbeatSnapshot:
    timestamp: str
    pending_approvals: int
    pending_approval_ids: list[str]
    agents: dict[str, dict]
    lane_metrics: dict[str, dict]


def _compute_health(info: dict) -> str:
    if info.get("paused"):
        return "CRITICAL"
    cost = info.get("cost_today", 0)
    budget = info.get("daily_budget", 10.0)
    if budget > 0 and cost / budget > 0.8:
        return "WARN:budget"
    if info.get("has_errors"):
        return "WARN:errors"
    return "OK"


def update_agent_registry(snapshot: HeartbeatSnapshot, registry_path: Path) -> None:
    header = "| Agent | Model | Status | Last Action | Last Metric | Cost Today | Health |\n"
    separator = "|-------|-------|--------|-------------|-------------|------------|--------|\n"
    rows = []
    for name, info in sorted(snapshot.agents.items()):
        status = "paused" if info.get("paused") else "active"
        last_action = info.get("last_action", "-")
        last_metric = info.get("last_metric", "-")
        cost = f"${info.get('cost_today', 0):.2f}"
        health = _compute_health(info)
        model = info.get("model", "-")
        rows.append(f"| {name} | {model} | {status} | {last_action} | {last_metric} | {cost} | {health} |")
    content = f"# Agent Registry\n\nAuto-updated every heartbeat.\n\n{header}{separator}" + "\n".join(rows) + "\n"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(content, encoding="utf-8")


class HeartbeatService:
    def __init__(
        self,
        *,
        metrics: MetricsTracker,
        approvals: ApprovalManager,
        agent_store: FileAgentStore,
        observe: ObserveStream | None = None,
        registry_path: Path | None = None,
    ) -> None:
        self.metrics = metrics
        self.approvals = approvals
        self.agent_store = agent_store
        self.observe = observe
        self.registry_path = registry_path

    def collect(self) -> HeartbeatSnapshot:
        pending = self.approvals.list_pending()
        agents: dict[str, dict] = {}
        for agent_name in self.agent_store.list_agents():
            state = self.agent_store.load_state(agent_name)
            agents[agent_name] = {
                "agent_class": state.get("agent_class"),
                "trust_level": state.get("trust_level", 1),
                "experiments_today": state.get("experiments_today", 0),
                "paused": state.get("paused", False),
                "last_metric": state.get("last_verified_state", {}).get("metric"),
                "promote_on_improvement": state.get("promote_on_improvement", False),
                "commit_on_promotion": state.get("commit_on_promotion", False),
                "branch_on_promotion": state.get("branch_on_promotion", False),
            }
        return HeartbeatSnapshot(
            timestamp=datetime.now(UTC).isoformat(),
            pending_approvals=len(pending),
            pending_approval_ids=[item["approval_id"] for item in pending],
            agents=agents,
            lane_metrics=self.metrics.snapshot(),
        )

    def emit(self) -> HeartbeatSnapshot:
        snapshot = self.collect()
        if self.observe is not None:
            self.observe.emit("heartbeat", payload=asdict(snapshot))
        if self.registry_path is not None:
            update_agent_registry(snapshot, self.registry_path)
            if self.observe is not None:
                self.observe.emit("agent_registry_updated")
        return snapshot
