from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from claw_v2.jobs import JobService
from claw_v2.observe import ObserveStream

logger = logging.getLogger(__name__)

WIKI_RESEARCH_JOB_KIND = "scheduler.wiki_research"
WIKI_RESEARCH_RESUME_KEY = "scheduler:wiki_research"
WIKI_SCRAPE_JOB_KIND = "scheduler.wiki_scrape"
WIKI_SCRAPE_RESUME_KEY = "scheduler:wiki_scrape"
PERF_OPTIMIZER_JOB_KIND = "scheduler.perf_optimizer"
PERF_OPTIMIZER_RESUME_KEY = "scheduler:perf_optimizer"
KAIROS_TICK_JOB_KIND = "scheduler.kairos_tick"
KAIROS_TICK_RESUME_KEY = "scheduler:kairos_tick"
SELF_IMPROVE_JOB_KIND = "scheduler.self_improve"
SELF_IMPROVE_RESUME_KEY = "scheduler:self_improve"
PIPELINE_POLL_JOB_KIND = "scheduler.pipeline_poll"
PIPELINE_POLL_RESUME_KEY = "scheduler:pipeline_poll"
PIPELINE_POLL_MERGES_JOB_KIND = "scheduler.pipeline_poll_merges"
PIPELINE_POLL_MERGES_RESUME_KEY = "scheduler:pipeline_poll_merges"
A2A_PROCESS_INBOX_JOB_KIND = "scheduler.a2a_process_inbox"
A2A_PROCESS_INBOX_RESUME_KEY = "scheduler:a2a_process_inbox"
APPROVAL_SWEEP_JOB_KIND = "scheduler.approval_sweep"
APPROVAL_SWEEP_RESUME_KEY = "scheduler:approval_sweep"
SUB_AGENT_JOB_KIND = "scheduler.sub_agent"
AUTO_DREAM_JOB_KIND = "scheduler.auto_dream"
AUTO_DREAM_RESUME_KEY = "scheduler:auto_dream"
LEARNING_CONSOLIDATE_JOB_KIND = "scheduler.learning_consolidate"
LEARNING_CONSOLIDATE_RESUME_KEY = "scheduler:learning_consolidate"
LEARNING_SOUL_SUGGESTIONS_JOB_KIND = "scheduler.learning_soul_suggestions"
LEARNING_SOUL_SUGGESTIONS_RESUME_KEY = "scheduler:learning_soul_suggestions"
SCHEDULED_BACKGROUND_STALE_RUNNING_SECONDS = 60 * 60
_ERROR_PREVIEW_LIMIT = 200
_RESULT_STRING_LIMIT = 200


def enqueue_scheduled_background_job(
    *,
    job_name: str,
    job_kind: str,
    resume_key: str,
    job_service: JobService | None,
    observe: ObserveStream | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = 3,
) -> str | None:
    if job_service is None:
        if observe is not None:
            observe.emit(
                "scheduled_job_skipped",
                payload={"job": job_name, "reason": "job_service_unavailable"},
            )
        return None

    try:
        job = job_service.enqueue(
            kind=job_kind,
            payload={"requested_at": time.time(), **dict(payload or {})},
            resume_key=resume_key,
            metadata={"source": f"scheduler.{job_name}"},
            max_attempts=max_attempts,
        )
    except Exception as exc:
        logger.exception("%s enqueue failed", job_name)
        if observe is not None:
            observe.emit(
                "scheduled_job_error",
                payload={"job": job_name, "error": _safe_error_preview(exc)},
            )
        return None

    if observe is not None:
        observe.emit(
            "scheduled_job_enqueued",
            payload={
                "job": job_name,
                "job_id": job.job_id,
                "kind": job.kind,
                "status": job.status,
                "resume_key": job.resume_key,
            },
        )
    return str(job.job_id)


class ScheduledBackgroundJobRunner:
    def __init__(
        self,
        *,
        job_name: str,
        job_kind: str,
        job_service: JobService,
        handler: Callable[[dict[str, Any]], object],
        observe: ObserveStream | None = None,
        worker_id: str | None = None,
        retry_delay_seconds: float = 60.0,
        stale_running_seconds: float = SCHEDULED_BACKGROUND_STALE_RUNNING_SECONDS,
        should_stop: Callable[[], bool] | None = None,
        result_summary: Callable[[object], dict[str, Any]] | None = None,
    ) -> None:
        self.job_name = job_name
        self.job_kind = job_kind
        self.job_service = job_service
        self.handler = handler
        self.observe = observe
        self.worker_id = worker_id or f"{job_name}-runner"
        self.retry_delay_seconds = retry_delay_seconds
        self.stale_running_seconds = max(0.001, float(stale_running_seconds))
        self.should_stop = should_stop
        self.result_summary = result_summary

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
            kinds=(self.job_kind,),
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
                f"{self.job_name}_job_stale_reclaimed",
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
            kinds=(self.job_kind,),
            now=now,
        )
        if job is None:
            return False
        started = time.monotonic()
        self._emit_job_event(f"{self.job_name}_job_started", job)
        try:
            result = self._execute(job)
        except Exception as exc:
            duration_seconds = time.monotonic() - started
            error_preview = _safe_error_preview(exc)
            logger.exception("%s job failed", self.job_name)
            self.job_service.fail(
                job.job_id,
                error=error_preview,
                retry=True,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            self._emit_job_event(
                f"{self.job_name}_job_failed",
                job,
                duration_seconds=duration_seconds,
                exc=exc,
            )
            return True

        self.job_service.complete(job.job_id, result=result)
        duration_seconds = time.monotonic() - started
        self._emit_job_event(
            f"{self.job_name}_job_completed",
            job,
            duration_seconds=duration_seconds,
            extra=result,
        )
        return True

    def _execute(self, job: Any) -> dict[str, Any]:
        payload = job.payload if isinstance(job.payload, dict) else {}
        result = self.handler(payload)
        if self.result_summary is not None:
            return self.result_summary(result)
        return _default_result_summary(result)

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


def wiki_research_result_summary(result: object) -> dict[str, Any]:
    data = result if isinstance(result, dict) else {}
    candidates = data.get("candidates")
    return {
        "topics_researched": _safe_int(data.get("topics_researched")),
        "pages_written": _safe_int(data.get("pages_written")),
        "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
    }


def kairos_tick_result_summary(result: object) -> dict[str, Any]:
    action = _safe_text_preview(str(getattr(result, "action", "") or ""), limit=_RESULT_STRING_LIMIT)
    summary: dict[str, Any] = {
        "action": action or "unknown",
        "duration_seconds": _safe_float(getattr(result, "duration_seconds", 0.0)),
    }
    reason = str(getattr(result, "reason", "") or "")
    if reason:
        summary["reason_preview"] = _safe_text_preview(reason, limit=_RESULT_STRING_LIMIT)
    error = str(getattr(result, "error", "") or "")
    if error:
        summary["error_preview"] = _safe_text_preview(error, limit=_RESULT_STRING_LIMIT)
    return summary


def safe_non_negative_int(value: object, *, default: int) -> int:
    if value is None:
        return max(0, int(default))
    try:
        return max(0, int(value))
    except (OverflowError, TypeError, ValueError):
        return max(0, int(default))


def _default_result_summary(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"ok": True}
    summary: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, str):
            summary[str(key)] = _safe_text_preview(value, limit=_RESULT_STRING_LIMIT)
        elif isinstance(value, (int, float, bool)) or value is None:
            summary[str(key)] = value
    return summary or {"ok": True}


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return round(float(value or 0.0), 3)
    except (OverflowError, TypeError, ValueError):
        return 0.0


def _safe_error_preview(exc: BaseException) -> str:
    return _safe_text_preview(str(exc), limit=_ERROR_PREVIEW_LIMIT)


def _safe_text_preview(value: str, *, limit: int) -> str:
    text = value.replace("\n", " ").strip()
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret|authorization|cookie)(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)",
        r"\1\2REDACTED",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "REDACTED", text)
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text
