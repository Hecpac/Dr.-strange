from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(slots=True)
class LaneMetrics:
    invocations: int = 0
    total_cost: float = 0.0
    degraded_invocations: int = 0


class MetricsTracker:
    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str, str], LaneMetrics] = defaultdict(LaneMetrics)

    def record(self, *, lane: str, provider: str, model: str, cost: float, degraded_mode: bool) -> None:
        metrics = self._by_key[(lane, provider, model)]
        metrics.invocations += 1
        metrics.total_cost += cost
        if degraded_mode:
            metrics.degraded_invocations += 1

    def snapshot(self) -> dict[str, dict]:
        return {
            f"{lane}:{provider}:{model}": {
                "invocations": metric.invocations,
                "total_cost": metric.total_cost,
                "degraded_invocations": metric.degraded_invocations,
            }
            for (lane, provider, model), metric in self._by_key.items()
        }
