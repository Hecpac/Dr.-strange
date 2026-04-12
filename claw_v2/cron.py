from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


logger = logging.getLogger(__name__)

JobHandler = Callable[[], object]


class CronPersistence(Protocol):
    def load_cron_state(self) -> dict[str, tuple[float, int]]: ...
    def save_cron_job(self, job_name: str, last_run_at: float, runs: int) -> None: ...


@dataclass(slots=True)
class ScheduledJob:
    name: str
    interval_seconds: int
    handler: JobHandler
    last_run_at: float = 0.0
    runs: int = 0
    metadata: dict = field(default_factory=dict)


class CronScheduler:
    def __init__(self, persistence: CronPersistence | None = None) -> None:
        self._jobs: dict[str, ScheduledJob] = {}
        self._persistence = persistence

    def register(self, job: ScheduledJob) -> None:
        self._jobs[job.name] = job

    def restore(self) -> None:
        """Load persisted run state into registered jobs."""
        if self._persistence is None:
            return
        saved = self._persistence.load_cron_state()
        for name, job in self._jobs.items():
            if name in saved:
                job.last_run_at, job.runs = saved[name]

    def list_jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    def run_due(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else now
        executed: list[str] = []
        for job in self._jobs.values():
            if job.runs > 0 and current - job.last_run_at < job.interval_seconds:
                continue
            try:
                job.handler()
            except Exception:
                logger.exception("cron job %s failed", job.name)
            job.last_run_at = current
            job.runs += 1
            executed.append(job.name)
            if self._persistence is not None:
                try:
                    self._persistence.save_cron_job(job.name, job.last_run_at, job.runs)
                except Exception:
                    logger.exception("cron persistence failed for %s", job.name)
        return executed
