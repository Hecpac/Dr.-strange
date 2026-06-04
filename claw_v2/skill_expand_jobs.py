from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from claw_v2.jobs import JobService
from claw_v2.observe import ObserveStream
from claw_v2.skills import SkillRegistry

logger = logging.getLogger(__name__)

SKILL_EXPAND_JOB_KIND = "scheduler.skill_expand"
SKILL_EXPAND_RESUME_KEY = "scheduler:skill_expand"
SKILL_EXPAND_STALE_RUNNING_SECONDS = 60 * 60
_ERROR_PREVIEW_LIMIT = 200


def enqueue_skill_expand_job(
    *,
    job_service: JobService | None,
    observe: ObserveStream | None = None,
    max_new: int = 2,
) -> str | None:
    if job_service is None:
        if observe is not None:
            observe.emit(
                "scheduled_job_skipped",
                payload={"job": "skill_expand", "reason": "job_service_unavailable"},
            )
        return None

    try:
        job = job_service.enqueue(
            kind=SKILL_EXPAND_JOB_KIND,
            payload={"requested_at": time.time(), "max_new": max(0, int(max_new))},
            resume_key=SKILL_EXPAND_RESUME_KEY,
            metadata={"source": "scheduler.skill_expand"},
            max_attempts=3,
        )
    except Exception as exc:
        logger.exception("skill_expand enqueue failed")
        if observe is not None:
            observe.emit(
                "scheduled_job_error",
                payload={"job": "skill_expand", "error": _safe_error_preview(exc)},
            )
        return None

    if observe is not None:
        observe.emit(
            "scheduled_job_enqueued",
            payload={
                "job": "skill_expand",
                "job_id": job.job_id,
                "kind": job.kind,
                "status": job.status,
                "resume_key": job.resume_key,
            },
        )
    return str(job.job_id)


class SkillExpandJobRunner:
    def __init__(
        self,
        *,
        job_service: JobService,
        skill_registry: SkillRegistry,
        observe: ObserveStream | None = None,
        worker_id: str = "skill-expand-runner",
        retry_delay_seconds: float = 60.0,
        stale_running_seconds: float = SKILL_EXPAND_STALE_RUNNING_SECONDS,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.job_service = job_service
        self.skill_registry = skill_registry
        self.observe = observe
        self.worker_id = worker_id
        self.retry_delay_seconds = retry_delay_seconds
        self.stale_running_seconds = max(0.001, float(stale_running_seconds))
        self.should_stop = should_stop

    def run_available(self, *, limit: int = 1, now: float | None = None) -> int:
        if self._should_stop():
            return 0
        self.reclaim_stale_running(now=now)
        claimed = 0
        for _ in range(max(0, int(limit))):
            if self._should_stop():
                break
            if not self.run_once(now=now):
                break
            claimed += 1
        return claimed

    def reclaim_stale_running(self, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        reclaimed = 0
        running = self.job_service.list(
            statuses=("running",),
            kinds=(SKILL_EXPAND_JOB_KIND,),
            limit=100,
        )
        for job in running:
            reference = job.updated_at or job.started_at or job.created_at or current
            age_seconds = max(0.0, current - float(reference))
            if age_seconds < self.stale_running_seconds:
                continue
            checkpoint = {
                "reclaimed_at": current,
                "age_seconds": age_seconds,
                "previous_worker_id": job.worker_id or "",
                "reason": "stale_running_reclaimed",
            }
            record = self.job_service.fail(
                job.job_id,
                error="stale_running_reclaimed",
                retry=True,
                retry_delay_seconds=0,
                checkpoint=checkpoint,
            )
            reclaimed += 1
            self._emit_job_event(
                "skill_expand_job_stale_reclaimed",
                job,
                duration_seconds=age_seconds,
                extra={
                    "stale_running_seconds": self.stale_running_seconds,
                    "status": getattr(record, "status", None),
                },
            )
        return reclaimed

    def run_once(self, *, now: float | None = None) -> bool:
        if self._should_stop():
            return False
        job = self.job_service.claim_next(
            worker_id=self.worker_id,
            kinds=(SKILL_EXPAND_JOB_KIND,),
            now=now,
        )
        if job is None:
            return False
        started = time.monotonic()
        self._emit_job_event("skill_expand_job_started", job)
        try:
            result = self._execute(job)
        except Exception as exc:
            duration_seconds = time.monotonic() - started
            error_preview = _safe_error_preview(exc)
            logger.exception("skill_expand job failed")
            self.job_service.fail(
                job.job_id,
                error=error_preview,
                retry=True,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            self._emit_job_event(
                "skill_expand_job_failed",
                job,
                duration_seconds=duration_seconds,
                exc=exc,
            )
            return True

        self.job_service.complete(job.job_id, result=result)
        duration_seconds = time.monotonic() - started
        self._emit_job_event(
            "skill_expand_job_completed",
            job,
            duration_seconds=duration_seconds,
            extra={
                "gaps_found": result.get("gaps_found"),
                "skills_generated": result.get("skills_generated"),
            },
        )
        return True

    def _execute(self, job: Any) -> dict[str, Any]:
        payload = job.payload if isinstance(job.payload, dict) else {}
        max_new = max(0, int(payload.get("max_new", 2)))
        result = self.skill_registry.auto_expand(max_new=max_new)
        return dict(result or {})

    def _should_stop(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def _emit_job_event(
        self,
        event_type: str,
        job: Any,
        *,
        duration_seconds: float | None = None,
        exc: BaseException | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.observe is None:
            return
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "kind": job.kind,
            "attempts": job.attempts,
        }
        if duration_seconds is not None:
            payload["duration_seconds"] = round(float(duration_seconds), 3)
        if exc is not None:
            payload["error_type"] = exc.__class__.__name__
            payload["error_preview"] = _safe_error_preview(exc)
        if extra:
            payload.update(extra)
        self.observe.emit(event_type, payload=payload)


def _safe_error_preview(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret|authorization|cookie)(\s*[=:]\s*)\S+",
        r"\1\2REDACTED",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "REDACTED", text)
    if len(text) > _ERROR_PREVIEW_LIMIT:
        return f"{text[:_ERROR_PREVIEW_LIMIT]}..."
    return text
