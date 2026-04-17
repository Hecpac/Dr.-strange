from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

KNOWN_AGENTS = ("hex", "rook", "alma", "eval")


@dataclass(slots=True)
class EcosystemMetric:
    name: str
    value: float
    status: Literal["OK", "WARN", "CRITICAL"]
    detail: str


@dataclass(slots=True)
class EcosystemHealth:
    timestamp: float
    metrics: list[EcosystemMetric]
    overall: Literal["OK", "WARN", "CRITICAL"]


class EcosystemHealthService:
    def __init__(
        self,
        *,
        bus: Any,
        observe: Any,
        dream_states: dict[str, Any],
        heartbeat: Any,
    ) -> None:
        self.bus = bus
        self.observe = observe
        self.dream_states = dream_states
        self.heartbeat = heartbeat

    def collect(self) -> EcosystemHealth:
        metrics: list[EcosystemMetric] = []
        metrics.append(self._check_bus_lag())
        metrics.append(self._check_cost_distribution())
        worst: Literal["OK", "WARN", "CRITICAL"] = "OK"
        for m in metrics:
            if m.status == "CRITICAL":
                worst = "CRITICAL"
            elif m.status == "WARN" and worst != "CRITICAL":
                worst = "WARN"
        return EcosystemHealth(timestamp=time.time(), metrics=metrics, overall=worst)

    def _check_bus_lag(self) -> EcosystemMetric:
        total = sum(self.bus.pending_count(agent) for agent in KNOWN_AGENTS)
        if total > 10:
            return EcosystemMetric(name="bus_lag", value=total, status="CRITICAL", detail=f"{total} messages pending")
        if total > 3:
            return EcosystemMetric(name="bus_lag", value=total, status="WARN", detail=f"{total} messages pending")
        return EcosystemMetric(name="bus_lag", value=total, status="OK", detail=f"{total} messages pending")

    def _check_cost_distribution(self) -> EcosystemMetric:
        try:
            costs = self.observe.cost_per_agent_today()
            total = sum(costs.values())
            detail = ", ".join(f"{k}:${v:.2f}" for k, v in costs.items()) if costs else "no data"
            return EcosystemMetric(name="cost_distribution", value=total, status="OK", detail=detail)
        except Exception:
            return EcosystemMetric(name="cost_distribution", value=0, status="OK", detail="unavailable")

    def write_dashboard(self, path: Path = Path.home() / ".claw" / "ecosystem-health.md") -> None:
        health = self.collect()
        lines = [
            f"# Ecosystem Health — {time.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"**Overall: {health.overall}**",
            "",
            "| Metric | Value | Status | Detail |",
            "|--------|-------|--------|--------|",
        ]
        for m in health.metrics:
            lines.append(f"| {m.name} | {m.value} | {m.status} | {m.detail} |")
        lines.append("")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
