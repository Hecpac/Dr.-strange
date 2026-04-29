from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Protocol
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)

JobHandler = Callable[[], object]
CronErrorSink = Callable[["ScheduledJob", BaseException], None]


class CronPersistence(Protocol):
    def load_cron_state(self) -> dict[str, tuple[float, int]]: ...
    def save_cron_job(self, job_name: str, last_run_at: float, runs: int) -> None: ...


@dataclass(slots=True)
class ScheduledJob:
    name: str
    interval_seconds: int | None
    handler: JobHandler
    daily_at: str | None = None
    timezone: str | None = None
    last_run_at: float = 0.0
    runs: int = 0
    metadata: dict = field(default_factory=dict)


def _next_due_for_daily_at(
    daily_at: str, timezone: str, last_run_at: float, *, now: float
) -> float:
    """Compute next epoch seconds when this daily_at job should fire.

    `daily_at` is "HH:MM" 24-hour. `timezone` is a ZoneInfo key.
    Returns absolute epoch seconds for the next firing.
    """
    tz = ZoneInfo(timezone)
    hour_str, minute_str = daily_at.split(":")
    hour = int(hour_str)
    minute = int(minute_str)
    now_dt = datetime.fromtimestamp(now, tz=tz)
    candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    last_run_dt = datetime.fromtimestamp(last_run_at, tz=tz) if last_run_at > 0 else None
    if last_run_dt is not None and last_run_dt.date() == candidate.date():
        # Already ran today: schedule next run for tomorrow at HH:MM.
        candidate = candidate + timedelta(days=1)
    elif candidate <= now_dt:
        # HH:MM already passed today and we haven't run; fire now (next firing
        # window is the past; due immediately).
        return now
    return candidate.timestamp()


class CronScheduler:
    def __init__(
        self,
        persistence: CronPersistence | None = None,
        error_sink: CronErrorSink | None = None,
    ) -> None:
        self._jobs: dict[str, ScheduledJob] = {}
        self._persistence = persistence
        self._error_sink = error_sink
        self._restored_state: dict[str, tuple[float, int]] | None = None

    def register(self, job: ScheduledJob) -> None:
        if self._restored_state is not None and job.name in self._restored_state:
            job.last_run_at, job.runs = self._restored_state[job.name]
        self._jobs[job.name] = job

    def restore(self) -> None:
        """Load persisted run state into registered jobs."""
        if self._persistence is None:
            return
        saved = self._persistence.load_cron_state()
        self._restored_state = saved
        for name, job in self._jobs.items():
            if name in saved:
                job.last_run_at, job.runs = saved[name]

    def list_jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    def run_due(self, *, now: float | None = None) -> list[str]:
        current = time.time() if now is None else now
        executed: list[str] = []
        for job in self._jobs.values():
            if not self._is_due(job, current):
                continue
            try:
                job.handler()
            except Exception as exc:
                logger.exception("cron job %s failed", job.name)
                if self._error_sink is not None:
                    try:
                        self._error_sink(job, exc)
                    except Exception:
                        logger.exception("cron error sink failed for %s", job.name)
            job.last_run_at = current
            job.runs += 1
            executed.append(job.name)
            if self._persistence is not None:
                try:
                    self._persistence.save_cron_job(job.name, job.last_run_at, job.runs)
                except Exception:
                    logger.exception("cron persistence failed for %s", job.name)
        return executed

    def _is_due(self, job: ScheduledJob, current: float) -> bool:
        # daily_at takes precedence when set
        if job.daily_at and job.timezone:
            try:
                next_due = _next_due_for_daily_at(
                    job.daily_at, job.timezone, job.last_run_at, now=current
                )
            except Exception:
                logger.exception("daily_at parse failed for %s", job.name)
                return False
            return current >= next_due
        if job.interval_seconds is None:
            return False
        if job.runs == 0:
            return True
        return current - job.last_run_at >= job.interval_seconds
