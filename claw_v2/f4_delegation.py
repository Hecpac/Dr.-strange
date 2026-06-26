"""Durable F4-B deterministic-delegation delivery lane.

One Telegram delivery enqueues exactly one durable ``f4b.delegation`` job
(keyed by a deterministic ``delivery_key``). :class:`F4DelegationJobRunner`
claims that job through :class:`~claw_v2.jobs.JobService` — so crash recovery is
JobService claim / retry / stale-recovery — and calls
:meth:`TaskHandler.ensure_autonomous_task_enqueued`, which idempotently
materialises exactly one ``agent_tasks`` row plus one
``coordinator.autonomous_task`` job for the deterministic ``task_id``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from claw_v2.observe import ObserveStream

F4_DELEGATION_JOB_KIND = "f4b.delegation"

# The bootstrap (one idempotent ledger upsert + one reserve) is sub-second, so a
# delivery job stuck `running` past this window means the claiming worker crashed
# mid-bootstrap. Reclaim aggressively (15 min) — far below the 6h coordinator-task
# default — so a crash-after-claim-before-bootstrap delivery recovers in minutes,
# not hours; reclaim + re-run is safe because the bootstrap is idempotent on the
# deterministic task_id.
F4_DELEGATION_STALE_RUNNING_SECONDS = 15 * 60


def f4b_delivery_task_id(delivery_key: str) -> str:
    """Deterministic, stable autonomous ``task_id`` for a delivery key.

    The same ``delivery_key`` maps to the same ``task_id`` forever, so a
    redelivery / reclaim of the same Telegram delivery converges on exactly one
    logical autonomous task via the idempotent bootstrap.
    """
    digest = hashlib.sha1(delivery_key.encode("utf-8")).hexdigest()[:16]
    return f"f4bdeliv:{digest}"


class F4DelegationJobRunner:
    """Off-tick runner: claim one ``f4b.delegation`` job -> idempotent bootstrap.

    Mirrors ``PendingVerificationReconciliationJobRunner``: it reclaims stale
    claimed jobs, claims the next queued delivery job through ``JobService``, and
    on a successful bootstrap links the materialised task + coordinator job into
    the delivery job's checkpoint/result before completing it. A structured
    bootstrap failure AND any raised error — from the bootstrap *or* the
    checkpoint/complete linkage — terminalize the delivery job via
    ``fail(retry=True)`` (-> retrying, then ``failed`` after ``max_attempts``).
    No in-process error leaves the claimed job wedged in ``running`` until the
    stale reclaim (``F4_DELEGATION_STALE_RUNNING_SECONDS``), and the row is NEVER
    deleted so the audit trail is preserved.

    Execution is intentionally NOT triggered here. The bootstrap leaves a
    resumable ``running`` / ``coordinator`` / ``autonomous`` ledger row, and the
    already-wired ledger-driven recovery picks it up: startup recovery plus the
    300s ``task_lifecycle_watchdog`` both call
    ``bot.resume_interrupted_tasks`` -> ``TaskHandler.resume_interrupted_autonomous_tasks``.
    Recovery has two windows: (1) ONCE the bootstrap ledger row exists, start
    latency is the watchdog interval (<= ~300s) — that watchdog is also the
    crash backstop for a row that already exists; (2) a crash AFTER the runner
    claimed the job but BEFORE the bootstrap committed the row has no resumable
    row yet, so it recovers via the stale-running reclaim
    (``F4_DELEGATION_STALE_RUNNING_SECONDS``, 15 min) -> retry -> bootstrap.
    Starting the coordinator thread from this runner would only shave the idle
    case (1) latency while coupling the runner to coordinator execution, so we
    deliberately rely on the existing watchdog instead.
    """

    def __init__(
        self,
        *,
        job_service: Any,
        task_handler: Any,
        observe: ObserveStream | None = None,
        worker_id: str = "f4b_delegation",
        stale_running_seconds: float = F4_DELEGATION_STALE_RUNNING_SECONDS,
        retry_delay_seconds: float = 60.0,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.job_service = job_service
        self.task_handler = task_handler
        self.observe = observe
        self.worker_id = worker_id
        self.stale_running_seconds = max(0.001, float(stale_running_seconds))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        # Lets the daemon (Task 6) wire graceful shutdown, e.g.
        # should_stop=shutdown.is_set, mirroring the reference runner.
        self.should_stop = should_stop

    def run_available(self, *, limit: int = 1, now: float | None = None) -> int:
        """Reclaim stale claims, then process up to ``limit`` delivery jobs."""
        if self._should_stop():
            return 0
        self.reclaim_stale_running(now=now)
        processed = 0
        for _ in range(max(0, int(limit))):
            if self._should_stop():
                break
            if not self._run_once(now=now):
                break
            processed += 1
        return processed

    def _should_stop(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def reclaim_stale_running(self, *, now: float | None = None) -> int:
        """Re-queue ``f4b.delegation`` jobs whose claiming worker disappeared."""
        recovered = self.job_service.recover_stale_running(
            stale_after_seconds=self.stale_running_seconds,
            kinds=(F4_DELEGATION_JOB_KIND,),
            now=now,
            error="f4b_delegation_stale_running_timeout",
            event_type="f4_delegation_stale_running_recovered",
        )
        return len(recovered)

    def _run_once(self, *, now: float | None = None) -> bool:
        job = self.job_service.claim_next(
            worker_id=self.worker_id,
            kinds=(F4_DELEGATION_JOB_KIND,),
            now=now,
        )
        if job is None:
            # Covers maintenance-blocked claims too (claim_next returns None).
            return False
        self._emit("f4_delegation_runner_started", job)
        payload = job.payload if isinstance(job.payload, dict) else {}
        try:
            result = self.task_handler.ensure_autonomous_task_enqueued(
                task_id=payload.get("task_id", ""),
                session_id=payload.get("session_id", ""),
                objective=payload.get("objective", ""),
                mode=payload.get("mode", "chat"),
                task_kind=payload.get("task_kind", ""),
                source_text=payload.get("source_text", ""),
                delegation_metadata=payload.get("delegation_metadata"),
            )
            if result.status == "started":
                # Link task + coordinator job into the delivery job, then complete.
                # checkpoint()/complete() are INSIDE the guard: a raise here (e.g.
                # SQLITE_BUSY in checkpoint, which is not retry-wrapped) must
                # recover via the 60s fail(retry=True) path below, not wedge the
                # claimed job in 'running' until the 6h stale reclaim. fail() is
                # idempotent on terminal jobs, so a raise AFTER complete()
                # committed won't double-fail.
                linkage = {
                    "task_id": result.task_id,
                    "coordinator_job_id": result.coordinator_job_id,
                }
                self.job_service.checkpoint(job.job_id, linkage)
                self.job_service.complete(job.job_id, result=linkage)
        except Exception as exc:
            # A *raised* error from the bootstrap or the linkage (e.g. a transient
            # DB write failure): recover for retry now (terminal 'failed' only
            # after max_attempts), mirroring the reference runner. NEVER delete.
            self.job_service.fail(
                job.job_id,
                error=f"f4b_delegation_runner_error: {exc}"[:500],
                retry=True,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            self._emit(
                "f4_delegation_runner_failed",
                job,
                extra={"reason": "runner_exception", "error_type": exc.__class__.__name__},
            )
            return True
        if result.status == "started":
            self._emit("f4_delegation_runner_completed", job, extra={"reason": result.reason})
            return True
        # coordinator_unavailable / failed: terminalize (NEVER delete). The fail()
        # + emit stay OUTSIDE the guard so an emit error can't re-fail a job that
        # fail() just moved to the non-terminal 'retrying' state (double attempt).
        self.job_service.fail(job.job_id, error=result.reason)
        self._emit(
            "f4_delegation_runner_failed",
            job,
            extra={"reason": result.reason, "status": result.status},
        )
        return True

    def _emit(self, event_type: str, job: Any, *, extra: dict[str, Any] | None = None) -> None:
        if self.observe is None:
            return
        # Reason codes + safe job identity only — no secrets / raw payloads.
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "kind": job.kind,
            "attempts": job.attempts,
        }
        if extra:
            payload.update(extra)
        self.observe.emit(event_type, payload=payload)
