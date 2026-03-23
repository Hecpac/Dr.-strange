from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

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


class HeartbeatService:
    def __init__(
        self,
        *,
        metrics: MetricsTracker,
        approvals: ApprovalManager,
        agent_store: FileAgentStore,
        observe: ObserveStream | None = None,
    ) -> None:
        self.metrics = metrics
        self.approvals = approvals
        self.agent_store = agent_store
        self.observe = observe

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
        return snapshot
