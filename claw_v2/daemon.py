from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from claw_v2.cron import CronScheduler
from claw_v2.heartbeat import HeartbeatService, HeartbeatSnapshot
from claw_v2.observe import ObserveStream
from claw_v2.task_ledger import TaskLedger
from claw_v2.tracing import new_trace_context

logger = logging.getLogger(__name__)

PENDING_VERIFICATION_RECONCILIATION_JOB_KIND = "daemon.pending_verification_reconciliation"
PENDING_VERIFICATION_RECONCILIATION_RESUME_KEY = "daemon:pending_verification_reconciliation"
PENDING_VERIFICATION_RECONCILIATION_STALE_RUNNING_SECONDS = 120.0
_ERROR_PREVIEW_LIMIT = 200


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class TickResult:
    executed_jobs: list[str]
    heartbeat: HeartbeatSnapshot


@dataclass(slots=True)
class BackgroundJobRunner:
    name: str
    handler: Callable[[], object]
    interval: float = 60.0


class ClawDaemon:
    def __init__(
        self,
        *,
        scheduler: CronScheduler,
        heartbeat: HeartbeatService,
        observe: ObserveStream | None = None,
        task_ledger: TaskLedger | None = None,
        job_service: Any | None = None,
        stale_task_seconds: float = 6 * 60 * 60,
        task_reconciliation_interval: float = 5 * 60,
        pending_verification_interval: float = 15 * 60,
        pending_verification_drain_apply: bool | None = None,
        pending_verification_drain_max_apply: int = 10,
        pending_verification_drain_max_scan: int = 500,
        heartbeat_snapshot_interval: float = 300.0,
    ) -> None:
        self.scheduler = scheduler
        self.heartbeat = heartbeat
        self.observe = observe
        self.task_ledger = task_ledger
        self.job_service = job_service
        self.stale_task_seconds = stale_task_seconds
        self.task_reconciliation_interval = task_reconciliation_interval
        self._last_task_reconciliation_at = 0.0
        self.pending_verification_interval = pending_verification_interval
        self._last_pending_verification_at = 0.0
        # Checkpoint D: live drain of the read-only backlog. Default OFF — the
        # env flag (or an explicit arg) must opt in before any row transitions.
        if pending_verification_drain_apply is None:
            pending_verification_drain_apply = _env_flag("CLAW_PENDING_VERIFICATION_DRAIN_APPLY")
        self.pending_verification_drain_apply = bool(pending_verification_drain_apply)
        self.pending_verification_drain_max_apply = pending_verification_drain_max_apply
        self.pending_verification_drain_max_scan = pending_verification_drain_max_scan
        self._background_job_runners: list[BackgroundJobRunner] = []
        # AM-HB (2026-06-12): heartbeat.collect() scans approvals, aggregates
        # cost SQL and reads every agent's state file — running it on EVERY
        # 60s tick is ~30× the intended heartbeat cadence. Cache and refresh
        # at most every heartbeat_snapshot_interval.
        self.heartbeat_snapshot_interval = max(0.0, float(heartbeat_snapshot_interval))
        self._cached_heartbeat_snapshot: HeartbeatSnapshot | None = None
        self._cached_heartbeat_snapshot_at = 0.0

    def register_background_job_runner(
        self,
        *,
        name: str,
        handler: Callable[[], object],
        interval: float = 60.0,
    ) -> None:
        self._background_job_runners.append(
            BackgroundJobRunner(
                name=name,
                handler=handler,
                interval=max(0.001, float(interval)),
            )
        )

    def tick(self, *, now: float | None = None) -> TickResult:
        trace = new_trace_context(artifact_id="daemon_tick")
        reconciled_lost = self._reconcile_stale_tasks(now=now)
        reconciled_orphan_jobs = self._reconcile_orphaned_jobs()
        pending_reconciliation_job_id = self._enqueue_pending_verification_reconciliation(now=now)
        executed_jobs = self.scheduler.run_due(now=now)
        snapshot = self._heartbeat_snapshot(now=now)
        if self.observe is not None:
            payload = {
                "executed_jobs": executed_jobs,
                "heartbeat": asdict(snapshot),
                "reconciled_lost_tasks": reconciled_lost,
                "reconciled_orphan_jobs": reconciled_orphan_jobs,
            }
            # The authoritative backlog count lives in the async
            # pending_verification_reconciliation job result/event, not in the
            # daemon control path.
            if pending_reconciliation_job_id is not None:
                payload["pending_verification_reconciliation_job_id"] = (
                    pending_reconciliation_job_id
                )
            self.observe.emit(
                "daemon_tick",
                trace_id=trace["trace_id"],
                root_trace_id=trace["root_trace_id"],
                span_id=trace["span_id"],
                parent_span_id=trace["parent_span_id"],
                artifact_id=trace["artifact_id"],
                payload=payload,
            )
        return TickResult(executed_jobs=executed_jobs, heartbeat=snapshot)

    def _heartbeat_snapshot(self, *, now: float | None = None) -> HeartbeatSnapshot:
        current = time.time() if now is None else now
        if (
            self._cached_heartbeat_snapshot is None
            or current - self._cached_heartbeat_snapshot_at >= self.heartbeat_snapshot_interval
        ):
            self._cached_heartbeat_snapshot = self.heartbeat.collect()
            self._cached_heartbeat_snapshot_at = current
        return self._cached_heartbeat_snapshot

    def _reconcile_stale_tasks(self, *, now: float | None = None) -> int:
        if self.task_ledger is None:
            return 0
        current = self._last_task_reconciliation_at if now is None else now
        if now is None:
            import time

            current = time.time()
        if current - self._last_task_reconciliation_at < self.task_reconciliation_interval:
            return 0
        changed = self.task_ledger.mark_stale_running_lost(
            older_than_seconds=self.stale_task_seconds
        )
        self._last_task_reconciliation_at = current
        if changed and self.observe is not None:
            self.observe.emit(
                "daemon_task_reconciliation",
                payload={"lost_tasks": changed, "older_than_seconds": self.stale_task_seconds},
            )
        return changed

    def _reconcile_orphaned_jobs(self) -> int:
        if self.task_ledger is None or self.job_service is None:
            return 0
        changed = 0
        active_statuses = ("queued", "running", "waiting_approval", "retrying")
        jobs = self.job_service.list(
            statuses=active_statuses,
            kinds=("coordinator.autonomous_task",),
            limit=100,
        )
        for job in jobs:
            task_id = str((job.payload or {}).get("task_id") or "").strip()
            if not task_id:
                continue
            task = self.task_ledger.get(task_id)
            if task is None or task.status not in {
                "succeeded",
                "completed_unverified",
                "failed",
                "timed_out",
                "cancelled",
                "lost",
            }:
                continue
            cancelled = self.job_service.cancel(
                job.job_id, reason=f"orphaned_by_task:{task.status}"
            )
            if cancelled is not None and cancelled.status == "cancelled":
                changed += 1
        if changed and self.observe is not None:
            self.observe.emit(
                "daemon_job_reconciliation",
                payload={"cancelled_orphan_jobs": changed},
            )
        return changed

    def _enqueue_pending_verification_reconciliation(
        self, *, now: float | None = None
    ) -> str | None:
        """Enqueue pending-verification reconciliation work outside daemon tick.

        Returns the job id when a queued/running active job exists for this
        reconciliation lane, or ``None`` when skipped (no ledger/job service,
        interval not elapsed) or enqueue failed. The actual report and optional
        drain run through ``JobService.claim_next`` in
        ``PendingVerificationReconciliationJobRunner``.
        """
        if self.task_ledger is None:
            return None
        current = time.time() if now is None else now
        if current - self._last_pending_verification_at < self.pending_verification_interval:
            return None
        if self.job_service is None:
            self._last_pending_verification_at = current
            if self.observe is not None:
                self.observe.emit(
                    "pending_verification_reconciliation_enqueue_skipped",
                    payload={"reason": "job_service_unavailable"},
                )
            return None

        payload = {
            "requested_at": current,
            "drain_apply": self.pending_verification_drain_apply,
            "drain_max_apply": self.pending_verification_drain_max_apply,
            "drain_max_scan": self.pending_verification_drain_max_scan,
        }
        metadata = {
            "source": "daemon.tick",
            "interval_seconds": self.pending_verification_interval,
        }
        try:
            job = self.job_service.enqueue(
                kind=PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,
                payload=payload,
                resume_key=PENDING_VERIFICATION_RECONCILIATION_RESUME_KEY,
                metadata=metadata,
                max_attempts=3,
            )
        except Exception as exc:
            logger.exception("pending verification reconciliation enqueue failed")
            if self.observe is not None:
                self.observe.emit(
                    "pending_verification_reconciliation_enqueue_error",
                    payload={"error": str(exc)},
                )
            return None

        self._last_pending_verification_at = current
        if self.observe is not None:
            self.observe.emit(
                "pending_verification_reconciliation_enqueued",
                payload={
                    "job_id": job.job_id,
                    "kind": job.kind,
                    "status": job.status,
                    "resume_key": job.resume_key,
                },
            )
        return str(job.job_id)

    async def run_loop(self, shutdown: asyncio.Event, interval: float = 60.0) -> None:
        background_tasks: list[asyncio.Task[None]] = []
        if self.observe is not None:
            background_tasks.append(
                asyncio.create_task(self._run_liveness_heartbeat_loop(shutdown, interval=interval))
            )
        if self.job_service is not None and self.task_ledger is not None:
            background_tasks.append(
                asyncio.create_task(
                    self._run_pending_verification_reconciliation_job_loop(
                        shutdown,
                        interval=interval,
                    )
                )
            )
        for runner in self._background_job_runners:
            background_tasks.append(
                asyncio.create_task(self._run_background_job_runner_loop(shutdown, runner=runner))
            )
        try:
            while not shutdown.is_set():
                try:
                    await asyncio.to_thread(self.tick)
                except Exception as exc:
                    if self.observe is not None:
                        trace = new_trace_context(artifact_id="daemon_tick")
                        # A contended SQLite write here (synchronous INSERT +
                        # COMMIT with a 15s busy_timeout) must not stall the
                        # event loop — Telegram polling and replies share it —
                        # nor kill this loop if the emit itself fails.
                        await self._emit_off_loop(
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
        finally:
            for task in background_tasks:
                task.cancel()
            for task in background_tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _emit_off_loop(self, event_type: str, **kwargs: Any) -> None:
        """Offload a diagnostic emit to a thread, swallowing failures.

        A contended SQLite write (the very thing M3/M4 address) must neither
        stall the event loop nor — if the emit itself raises — propagate out of
        `await` and terminate the daemon loop. Diagnostic emits are best-effort.
        """
        if self.observe is None:
            return
        try:
            await asyncio.to_thread(self.observe.emit, event_type, **kwargs)
        except Exception:
            logger.warning("off-loop emit of %s failed", event_type, exc_info=True)

    async def _run_liveness_heartbeat_loop(
        self,
        shutdown: asyncio.Event,
        *,
        interval: float,
    ) -> None:
        while not shutdown.is_set():
            await self._emit_off_loop(
                "daemon_heartbeat",
                payload={
                    "pid": os.getpid(),
                    "ts": time.time(),
                    "source": "daemon_liveness_loop",
                },
            )
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_pending_verification_reconciliation_job_loop(
        self,
        shutdown: asyncio.Event,
        *,
        interval: float,
    ) -> None:
        if self.job_service is None or self.task_ledger is None:
            return
        runner = PendingVerificationReconciliationJobRunner(
            job_service=self.job_service,
            task_ledger=self.task_ledger,
            observe=self.observe,
            should_stop=shutdown.is_set,
        )
        while not shutdown.is_set():
            try:
                await asyncio.to_thread(runner.run_available, limit=1)
            except Exception as exc:
                logger.exception("pending verification reconciliation runner failed")
                await self._emit_off_loop(
                    "pending_verification_reconciliation_runner_error",
                    payload={"error": str(exc)},
                )
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _run_background_job_runner_loop(
        self,
        shutdown: asyncio.Event,
        *,
        runner: BackgroundJobRunner,
    ) -> None:
        while not shutdown.is_set():
            try:
                await asyncio.to_thread(runner.handler)
            except Exception as exc:
                logger.exception("daemon background job runner failed: %s", runner.name)
                await self._emit_off_loop(
                    "daemon_background_job_runner_error",
                    payload={"runner": runner.name, "error": _safe_error_preview(exc)},
                )
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=runner.interval)
            except asyncio.TimeoutError:
                pass


class PendingVerificationReconciliationJobRunner:
    """Minimal runner for the daemon pending-verification reconciliation job."""

    def __init__(
        self,
        *,
        job_service: Any,
        task_ledger: TaskLedger,
        observe: ObserveStream | None = None,
        worker_id: str = "pending-verification-reconciler",
        retry_delay_seconds: float = 60.0,
        stale_running_seconds: float = PENDING_VERIFICATION_RECONCILIATION_STALE_RUNNING_SECONDS,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.job_service = job_service
        self.task_ledger = task_ledger
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
            kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,),
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
                "reason": "stale_running_timeout",
            }
            record = self.job_service.fail(
                job.job_id,
                error="stale_running_timeout",
                retry=True,
                retry_delay_seconds=0,
                checkpoint=checkpoint,
            )
            reclaimed += 1
            self._emit_job_event(
                "daemon_reconciliation_job_stale_reclaimed",
                job,
                duration_seconds=age_seconds,
                extra={
                    "stale_running_seconds": self.stale_running_seconds,
                    "source": "stale_running_reaper",
                    "status": getattr(record, "status", None),
                },
            )
        return reclaimed

    def run_once(self, *, now: float | None = None) -> bool:
        if self._should_stop():
            return False
        job = self.job_service.claim_next(
            worker_id=self.worker_id,
            kinds=(PENDING_VERIFICATION_RECONCILIATION_JOB_KIND,),
            now=now,
        )
        if job is None:
            return False
        started = time.monotonic()
        self._emit_job_event("daemon_reconciliation_job_started", job)
        try:
            result = self._execute(job)
        except Exception as exc:
            duration_seconds = time.monotonic() - started
            logger.exception("pending verification reconciliation job failed")
            self.job_service.fail(
                job.job_id,
                error=str(exc),
                retry=True,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            self._emit_job_event(
                "daemon_reconciliation_job_failed",
                job,
                duration_seconds=duration_seconds,
                exc=exc,
            )
            return True
        self.job_service.complete(job.job_id, result=result)
        duration_seconds = time.monotonic() - started
        self._emit_job_event(
            "daemon_reconciliation_job_completed",
            job,
            duration_seconds=duration_seconds,
            extra={
                "unverified_count": result.get("unverified_count"),
                "overdue_count": result.get("overdue_count"),
                "drain_apply": result.get("drain_apply"),
            },
        )
        if self.observe is not None:
            self.observe.emit(
                "pending_verification_reconciliation_job_completed",
                payload={"job_id": job.job_id, **result},
            )
        return True

    def _execute(self, job: Any) -> dict[str, Any]:
        from claw_v2.reconciliation import build_reconciliation_report

        report = build_reconciliation_report(self.task_ledger, observe=self.observe)
        payload = job.payload if isinstance(job.payload, dict) else {}
        drain_apply = bool(payload.get("drain_apply", False))
        result: dict[str, Any] = {
            "unverified_count": int(report.get("unverified_count", 0)),
            "overdue_count": int(report.get("overdue_count", 0)),
            "by_recommended_action": dict(report.get("by_recommended_action", {}) or {}),
            "drain_apply": drain_apply,
        }
        if not drain_apply:
            return result

        try:
            drain_result = self.task_ledger.drain_reconcilable_unverified(
                apply=True,
                max_scan=int(payload.get("drain_max_scan", 500)),
                max_apply=int(payload.get("drain_max_apply", 10)),
            )
        except Exception as exc:
            logger.exception("pending verification drain failed")
            result["drain_error"] = str(exc)
            return result

        result["drain_result"] = _compact_drain_result(drain_result)
        return result

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


# A recovery job younger than this is still a live promise (the brain just
# told the user it would resume the request "cuando el contexto se limpie"), so
# the drainer leaves it alone and only cleans genuinely-stale backlog.
RECOVERY_JOB_STALE_SECONDS = 86_400.0
_RECOVERY_REQUEST_PREVIEW_CHARS = 300


class RecoveryJobDrainRunner:
    """Drains stale ``recovery_jobs`` off-tick (2026-06-10 audit C1).

    ``recovery_jobs`` accumulated forever because ``resolve_recovery_job`` had
    no runtime caller — a false promise of continuity (the agent told the user
    it would resume a request, then never did). This runner surfaces each
    abandoned request to the operator and marks it resolved.

    Notify-and-close MVP: it NEVER re-executes the request (auto-replay would
    be a separate opt-in evolution). notify-then-resolve ordering means a failed
    notification leaves the job pending for the next cycle rather than silently
    dropping it — we would rather double-notify than lose the promise. Only jobs
    older than ``min_age_seconds`` are touched, and each cycle is capped and
    paced to respect Telegram's per-chat rate limit.
    """

    def __init__(
        self,
        *,
        memory: Any,
        notifier: Callable[[str], object],
        observe: ObserveStream | None = None,
        should_stop: Callable[[], bool] | None = None,
        min_age_seconds: float = RECOVERY_JOB_STALE_SECONDS,
        max_per_cycle: int = 10,
        inter_message_delay_seconds: float = 1.0,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self.memory = memory
        self.notifier = notifier
        self.observe = observe
        self.should_stop = should_stop
        self.min_age_seconds = min_age_seconds
        self.max_per_cycle = max(1, int(max_per_cycle))
        self.inter_message_delay_seconds = max(0.0, float(inter_message_delay_seconds))
        self.sleep = sleep

    def run_once(self) -> int:
        if self.should_stop is not None and self.should_stop():
            return 0
        jobs = self.memory.list_pending_recovery_jobs(
            older_than_seconds=self.min_age_seconds,
            limit=self.max_per_cycle,
        )
        drained = 0
        for index, job in enumerate(jobs):
            if self.should_stop is not None and self.should_stop():
                break
            try:
                self.notifier(self._format(job))
            except Exception:
                # Leave the job pending so the next cycle retries the notify;
                # never resolve a job the operator was not told about.
                logger.exception(
                    "recovery job drain notification failed for job %s",
                    job.get("id"),
                )
                continue
            self.memory.resolve_recovery_job(job["id"], status="resolved")
            drained += 1
            if self.observe is not None:
                self.observe.emit(
                    "recovery_job_drained",
                    payload={
                        "recovery_job_id": job.get("id"),
                        "session_id": job.get("session_id"),
                        "failure_reason": job.get("failure_reason"),
                    },
                )
            # Pace successful sends so a backlog cannot trip Telegram's per-chat
            # rate limit (~1 msg/sec). No pace after the last job of the cycle.
            if index < len(jobs) - 1:
                self.sleep(self.inter_message_delay_seconds)
        return drained

    @staticmethod
    def _format(job: dict[str, Any]) -> str:
        request = (job.get("original_request_sanitized") or "").strip()
        if len(request) > _RECOVERY_REQUEST_PREVIEW_CHARS:
            request = request[: _RECOVERY_REQUEST_PREVIEW_CHARS - 3] + "..."
        reason = job.get("failure_reason") or "desconocido"
        return (
            "Tenía una petición que prometí retomar y no completé "
            f"(motivo: {reason}): «{request}». "
            "La marqué como cerrada — vuelve a pedírmela si todavía la necesitas."
        )


def _compact_drain_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    compact: dict[str, Any] = {}
    missing = object()
    for key in ("scanned", "eligible", "applied", "skipped", "scan_capped", "limit"):
        raw = getattr(value, key, missing)
        if raw is missing:
            continue
        if isinstance(raw, (str, int, float, bool, list, dict)) or raw is None:
            compact[key] = raw
    return compact


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
