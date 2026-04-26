from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from claw_v2.cron import CronScheduler
from claw_v2.heartbeat import HeartbeatService, HeartbeatSnapshot
from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger
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
        task_ledger: TaskLedger | None = None,
        stale_task_seconds: float = 6 * 60 * 60,
        task_reconciliation_interval: float = 5 * 60,
    ) -> None:
        self.scheduler = scheduler
        self.heartbeat = heartbeat
        self.observe = observe
        self.task_ledger = task_ledger
        self.stale_task_seconds = stale_task_seconds
        self.task_reconciliation_interval = task_reconciliation_interval
        self._last_task_reconciliation_at = 0.0

    def tick(self, *, now: float | None = None) -> TickResult:
        trace = new_trace_context(artifact_id="daemon_tick")
        executed_jobs = self.scheduler.run_due(now=now)
        reconciled_lost = self._reconcile_stale_tasks(now=now)
        snapshot = self.heartbeat.collect()
        if self.observe is not None:
            self.observe.emit(
                "daemon_tick",
                trace_id=trace["trace_id"],
                root_trace_id=trace["root_trace_id"],
                span_id=trace["span_id"],
                parent_span_id=trace["parent_span_id"],
                artifact_id=trace["artifact_id"],
                payload={
                    "executed_jobs": executed_jobs,
                    "heartbeat": asdict(snapshot),
                    "reconciled_lost_tasks": reconciled_lost,
                },
            )
        return TickResult(executed_jobs=executed_jobs, heartbeat=snapshot)

    def _reconcile_stale_tasks(self, *, now: float | None = None) -> int:
        if self.task_ledger is None:
            return 0
        current = self._last_task_reconciliation_at if now is None else now
        if now is None:
            import time

            current = time.time()
        if current - self._last_task_reconciliation_at < self.task_reconciliation_interval:
            return 0
        changed = self.task_ledger.mark_stale_running_lost(older_than_seconds=self.stale_task_seconds)
        self._last_task_reconciliation_at = current
        if changed and self.observe is not None:
            self.observe.emit(
                "daemon_task_reconciliation",
                payload={"lost_tasks": changed, "older_than_seconds": self.stale_task_seconds},
            )
        return changed

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
