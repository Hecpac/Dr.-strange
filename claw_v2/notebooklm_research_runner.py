"""Durable runner for notebooklm.research jobs (F2 Phase 3).

Claims one ``notebooklm.research`` job per tick, drives it through
F2ExternalEffectExecutor, and maps the EffectOutcome to a JobService terminal
state. Maintenance-gate (A2) is enforced automatically by ``JobService.claim_next``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from claw_v2 import notebooklm_cdp
from claw_v2.external_effect_executor import F2ExternalEffectExecutor
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.jobs import JobService
from claw_v2.notebooklm_research_effect import (
    build_research_effect_spec,
    notebooklm_research_adapter,
    notebooklm_research_verifier,
)

logger = logging.getLogger(__name__)

_JOB_KIND = "notebooklm.research"
_WORKER_ID = "notebooklm-research"


def make_nlm_status_fn(
    nlm_getter: Callable[[], Any],
) -> Callable[[str], dict[str, Any]]:
    """Build the status_fn closure the runner/verifier consume.

    NotebookLMService.status() returns a NESTED dict
    ``{"notebook": {"sources_count": N, ...}, "sources": [...]}`` (same shape for
    the SDK/CDP and external/Jacob backends). The verifier and the runner's
    pre-intent baseline both read a FLAT ``{"source_count": int}``, so this
    adapter flattens it. ``nlm_getter`` is called lazily (the NotebookLMService is
    wired by lifecycle after build_runtime returns).

    NOTE: status() exceptions are intentionally NOT swallowed — they propagate so
    the verifier classifies ``status_unavailable`` and the runner treats a
    pre-intent baseline read failure as transient (retry), rather than letting a
    transient error masquerade as "0 sources".
    """

    def status_fn(notebook_id: str) -> dict[str, Any]:
        nlm = nlm_getter()
        if nlm is None:
            return {"source_count": 0}
        raw = nlm.status(notebook_id) or {}
        notebook = raw.get("notebook") or {}
        return {"source_count": int(notebook.get("sources_count") or 0)}

    return status_fn


def make_nlm_deep_research_fn(
    nlm_getter: Callable[[], Any],
) -> Callable[[str, str], int]:
    """Build the deep_research_fn closure the runner/adapter consume.

    Selects the research backend (external vs CDP) by mirroring
    ``start_research`` / ``_worker``. The durable runner handles deep-only by
    construction (``start_research`` only routes to the durable lane when
    ``mode == "deep"``), so mode is not threaded through here — the CDP backend
    is inherently deep-only.
    """

    def deep_research_fn(notebook_id: str, query: str) -> int:
        nlm = nlm_getter()
        if nlm is None:
            return 0
        if getattr(nlm, "_use_external_backend", False):
            ext = getattr(nlm, "_external_backend", None)
            if ext is not None:
                return int(ext.deep_research(notebook_id, query, mode="deep") or 0)
        cdp_fn = getattr(nlm, "_cdp_research_fn", None) or notebooklm_cdp.deep_research
        return int(cdp_fn(notebook_id, query) or 0)

    return deep_research_fn


class NotebookLMResearchRunner:
    """Claims and executes one notebooklm.research job per ``run_once()`` call.

    All callables are injected so the runner is testable without any daemon
    infrastructure.

    Args:
        job_service: The singleton JobService (must share the same RuntimeDb as store).
        store: F2DurabilityStore wrapping the same RuntimeDb.
        deep_research_fn: ``(notebook_id, query) -> int`` — imported source count.
        status_fn: ``(notebook_id) -> {"source_count": int}`` — verifier baseline.
        observe: Optional ObserveStream for audit-trail events.
        notifier: Optional ``(message: str) -> None`` for Telegram notifications.
    """

    def __init__(
        self,
        *,
        job_service: JobService,
        store: F2DurabilityStore,
        deep_research_fn: Callable[[str, str], Any],
        status_fn: Callable[[str], dict[str, Any]],
        observe: Any | None = None,
        notifier: Callable[[str], None] | None = None,
    ) -> None:
        self._jobs = job_service
        self._executor = F2ExternalEffectExecutor(store)
        self._deep_research_fn = deep_research_fn
        self._status_fn = status_fn
        self._observe = observe
        self._notifier = notifier

    def _emit(self, event_type: str, **payload: Any) -> None:
        if self._observe is not None:
            self._observe.emit(event_type, payload=payload)

    def run_once(self) -> bool:
        """Claim and process one notebooklm.research job.

        Returns True if a job was claimed and processed (regardless of outcome),
        False if no job was available.
        """
        job = self._jobs.claim_next(
            worker_id=_WORKER_ID,
            kinds=(_JOB_KIND,),
        )
        if job is None:
            return False

        job_id = job.job_id
        payload = job.payload or {}
        notebook_id: str = payload.get("notebook_id", "")
        query: str = payload.get("query", "")
        mode: str = payload.get("mode", "deep")

        # Read pre-intent source count for the verifier baseline. A status read
        # failure here is transient (network/CDP) — fail the job with retry=True
        # rather than recording a bogus 0 baseline (which would corrupt the
        # verifier's later recovery decision). No intent has been recorded yet.
        try:
            pre_count_raw = (self._status_fn(notebook_id) or {}).get("source_count", 0)
            pre_count = int(pre_count_raw)
        except Exception as exc:
            logger.warning(
                "notebooklm_research_runner: pre-intent status read failed for job %s: %s",
                job_id,
                exc,
            )
            self._jobs.fail(job_id, error=f"pre_intent_status_unavailable: {exc}", retry=True)
            return True

        spec = build_research_effect_spec(
            job_id=job_id,
            notebook_id=notebook_id,
            query=query,
            mode=mode,
            pre_intent_source_count=pre_count,
            task_id=job.metadata.get("task_id") if job.metadata else None,
        )

        adapter = notebooklm_research_adapter(self._deep_research_fn)
        verifier = notebooklm_research_verifier(self._status_fn)

        try:
            outcome = self._executor.execute(spec, adapter, verifier)
        except Exception as exc:
            logger.exception("notebooklm_research_runner: adapter raised for job %s", job_id)
            self._jobs.fail(job_id, error=str(exc), retry=True)
            return True

        if outcome.status in ("applied", "verified_applied"):
            self._jobs.complete(
                job_id,
                result={
                    "notebook_id": notebook_id,
                    "imported_count": (
                        outcome.record.result_json
                        and _parse_imported_count(outcome.record.result_json)
                    )
                    or 0,
                    "external_effect_id": outcome.record.external_effect_id,
                },
            )
        else:
            # The executor only returns applied/verified_applied (handled above)
            # or blocked_manual_review here — verified_absent is retried IN-CALL
            # by the executor, never surfaced as an outcome. Assert loudly if a
            # new terminal status is ever introduced.
            assert outcome.status == "blocked_manual_review", (
                f"unexpected outcome status {outcome.status!r} for job {job_id}"
            )
            # blocked_manual_review: terminal, never retry
            meta = {
                "external_effect_id": outcome.record.external_effect_id,
                "idempotency_key": outcome.record.idempotency_key,
                "notebook_id": notebook_id,
                "effect_kind": spec.effect_kind,
                "verifier_reason": getattr(outcome.record, "error", None),
            }
            self._jobs.fail(
                job_id,
                error="effect_blocked_manual_review",
                retry=False,
                checkpoint=meta,
            )
            self._emit(
                "notebooklm_research_effect_blocked_manual_review",
                **meta,
                job_id=job_id,
            )
            if self._notifier is not None:
                try:
                    self._notifier(
                        f"Research para notebook {notebook_id[:8]} en revisión manual; "
                        "no fue re-ejecutado."
                    )
                except Exception:
                    logger.exception(
                        "notebooklm_research_runner: notifier raised for job %s", job_id
                    )

        return True


def _parse_imported_count(result_json: str) -> int | None:
    """Extract imported_count from the stored result JSON string, if present."""
    try:
        data = json.loads(result_json)
        if isinstance(data, dict):
            return int(data.get("imported_count", 0))
    except Exception:
        pass
    return None
