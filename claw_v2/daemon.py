from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from claw_v2.cron import CronScheduler
from claw_v2.heartbeat import HeartbeatService, HeartbeatSnapshot
from claw_v2.observe import ObserveStream
from claw_v2.tracing import new_trace_context


@dataclass(slots=True)
class TickResult:
    executed_jobs: list[str]
    heartbeat: HeartbeatSnapshot


class ClawDaemon:
    def __init__(
        self,
        *,
        scheduler: CronScheduler,
        heartbeat: HeartbeatService,
        observe: ObserveStream | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.heartbeat = heartbeat
        self.observe = observe

    def tick(self, *, now: float | None = None) -> TickResult:
        trace = new_trace_context(artifact_id="daemon_tick")
        executed_jobs = self.scheduler.run_due(now=now)
        snapshot = self.heartbeat.collect()
        if self.observe is not None:
            self.observe.emit(
                "daemon_tick",
                trace_id=trace["trace_id"],
                root_trace_id=trace["root_trace_id"],
                span_id=trace["span_id"],
                parent_span_id=trace["parent_span_id"],
                artifact_id=trace["artifact_id"],
                payload={"executed_jobs": executed_jobs, "heartbeat": asdict(snapshot)},
            )
        return TickResult(executed_jobs=executed_jobs, heartbeat=snapshot)

    async def run_loop(self, shutdown: asyncio.Event, interval: float = 60.0) -> None:
        while not shutdown.is_set():
            try:
                await asyncio.to_thread(self.tick)
            except Exception as exc:
                if self.observe is not None:
                    trace = new_trace_context(artifact_id="daemon_tick")
                    self.observe.emit(
                        "daemon_tick_error",
                        trace_id=trace["trace_id"],
                        root_trace_id=trace["root_trace_id"],
                        span_id=trace["span_id"],
                        parent_span_id=trace["parent_span_id"],
                        artifact_id=trace["artifact_id"],
                        payload={"error": str(exc)},
                    )
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
