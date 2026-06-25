from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from claw_v2.f2_durability_store import ExternalEffectRecord, F2DurabilityStore


@dataclass(frozen=True, slots=True)
class EffectSpec:
    task_id: str
    run_id: str
    phase: str
    effect_kind: str
    target: str
    request: dict[str, Any]
    content_hash: str
    job_id: str | None = None
    verifier_kind: str | None = None
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class AdapterResult:
    applied: bool
    result: dict[str, Any]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class VerifierVerdict:
    classification: str  # "verified_applied" | "verified_absent" | "blocked_manual_review"
    verification: dict[str, Any]
    reason: str


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    status: str  # applied | verified_applied | blocked_manual_review
    record: ExternalEffectRecord


# adapter: (EffectSpec) -> AdapterResult, may raise
Adapter = Callable[["EffectSpec"], "AdapterResult"]
# verifier: (EffectSpec, ExternalEffectRecord) -> VerifierVerdict
Verifier = Callable[["EffectSpec", "ExternalEffectRecord"], "VerifierVerdict"]

# Statuses an interrupted record may carry into a recovery pass.
_RECOVERABLE_STATUSES = frozenset({"intent_recorded", "apply_in_progress", "verification_required"})
# Verdict classifications the verifier is allowed to return.
_VERIFIER_CLASSIFICATIONS = frozenset(
    {"verified_applied", "verified_absent", "blocked_manual_review"}
)


class F2ExternalEffectExecutor:
    def __init__(self, store: F2DurabilityStore) -> None:
        self._store = store

    def execute(self, spec: EffectSpec, adapter: Adapter, verifier: Verifier) -> EffectOutcome:
        record = self._store.record_external_effect(
            task_id=spec.task_id,
            run_id=spec.run_id,
            phase=spec.phase,
            effect_kind=spec.effect_kind,
            target=spec.target,
            request=spec.request,
            content_hash=spec.content_hash,
            job_id=spec.job_id,
            verifier_kind=spec.verifier_kind,
            status="intent_recorded",
        )
        return self._drive(spec, record, adapter, verifier)

    def _transition(
        self,
        external_effect_id: str,
        **kwargs: Any,
    ) -> ExternalEffectRecord:
        updated = self._store.update_external_effect_status(external_effect_id, **kwargs)
        if updated is None:
            raise RuntimeError(f"external_effect {external_effect_id} disappeared mid-transition")
        return updated

    def _drive(
        self,
        spec: EffectSpec,
        record: ExternalEffectRecord,
        adapter: Adapter,
        verifier: Verifier,
    ) -> EffectOutcome:
        status = record.status
        if status in ("applied", "verified_applied"):
            return EffectOutcome("applied", record)
        if status == "blocked_manual_review":
            return EffectOutcome("blocked_manual_review", record)
        if status == "verified_absent" or (
            status == "intent_recorded" and record.attempt_count == 0
        ):
            return self._apply(spec, record, adapter, verifier)
        # Interrupted attempt: a recoverable in-flight status carried over a crash.
        if status == "apply_in_progress" or (
            status in _RECOVERABLE_STATUSES and record.attempt_count > 0
        ):
            return self._recover(spec, record, adapter, verifier)
        raise ValueError(f"unroutable external-effect status: {status}")

    def _apply(
        self,
        spec: EffectSpec,
        record: ExternalEffectRecord,
        adapter: Adapter,
        verifier: Verifier,
    ) -> EffectOutcome:
        if record.attempt_count >= spec.max_attempts:
            blocked = self._transition(
                record.external_effect_id,
                status="blocked_manual_review",
                error="max_attempts_exhausted",
            )
            return EffectOutcome("blocked_manual_review", blocked)
        record = self._transition(
            record.external_effect_id,
            status="apply_in_progress",
            increment_attempt_count=True,
        )
        try:
            ar = adapter(spec)
        except Exception as exc:
            # Leave apply_in_progress + record error; next attempt enters _recover
            self._transition(
                record.external_effect_id,
                status="apply_in_progress",
                error=str(exc),
            )
            raise
        if ar.applied:
            applied = self._transition(
                record.external_effect_id,
                status="applied",
                result=ar.result,
            )
            return EffectOutcome("applied", applied)
        blocked = self._transition(
            record.external_effect_id,
            status="blocked_manual_review",
            result=ar.result,
            error=ar.reason or "adapter_not_applied",
        )
        return EffectOutcome("blocked_manual_review", blocked)

    def _recover(
        self,
        spec: EffectSpec,
        record: ExternalEffectRecord,
        adapter: Adapter,
        verifier: Verifier,
    ) -> EffectOutcome:
        verdict = verifier(spec, record)
        if verdict.classification not in _VERIFIER_CLASSIFICATIONS:
            raise ValueError(f"invalid verifier classification: {verdict.classification}")
        # Persist the verifier's reason into record.error ONLY when blocking, so
        # record.error uniformly carries the blocking reason on both the
        # adapter-blocked path (e.g. "zero_imports") and the verifier-blocked
        # path (e.g. "count_moved_or_unknown"). verified_applied/verified_absent
        # are not failures, so they must not write an error.
        blocking = verdict.classification == "blocked_manual_review"
        updated = self._transition(
            record.external_effect_id,
            status=verdict.classification,
            verification=verdict.verification,
            verifier_kind=spec.verifier_kind,
            error=verdict.reason if blocking else None,
        )
        if verdict.classification == "verified_applied":
            return EffectOutcome("verified_applied", updated)
        if verdict.classification == "verified_absent":
            return self._apply(spec, updated, adapter, verifier)
        return EffectOutcome("blocked_manual_review", updated)
