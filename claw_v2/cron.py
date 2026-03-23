from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


JobHandler = Callable[[], object]


@dataclass(slots=True)
class ScheduledJob:
    name: str
    interval_seconds: int
    handler: JobHandler
    last_run_at: float = 0.0
    runs: int = 0
    metadata: dict = field(default_factory=dict)


class CronScheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, ScheduledJob] = {}

    def register(self, job: ScheduledJob) -> None:
        self._jobs[job.name] = job

    def list_jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    def run_due(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else now
        executed: list[str] = []
        for job in self._jobs.values():
            if job.runs > 0 and current - job.last_run_at < job.interval_seconds:
                continue
            job.handler()
            job.last_run_at = current
            job.runs += 1
            executed.append(job.name)
        return executed
