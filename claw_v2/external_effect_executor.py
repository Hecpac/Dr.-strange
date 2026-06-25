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
    status: str  # applied | verified_applied | verified_absent | blocked_manual_review
    record: ExternalEffectRecord
    should_retry: bool


# adapter: (EffectSpec) -> AdapterResult, may raise
Adapter = Callable[["EffectSpec"], "AdapterResult"]
# verifier: (EffectSpec, ExternalEffectRecord) -> VerifierVerdict
Verifier = Callable[["EffectSpec", "ExternalEffectRecord"], "VerifierVerdict"]


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

    def _drive(
        self,
        spec: EffectSpec,
        record: ExternalEffectRecord,
        adapter: Adapter,
        verifier: Verifier,
    ) -> EffectOutcome:
        status = record.status
        if status in ("applied", "verified_applied"):
            return EffectOutcome("applied", record, should_retry=False)
        if status == "blocked_manual_review":
            return EffectOutcome("blocked_manual_review", record, should_retry=False)
        if status == "verified_absent" or (
            status == "intent_recorded" and record.attempt_count == 0
        ):
            return self._apply(spec, record, adapter, verifier)
        # interrupted attempt (intent_recorded/apply_in_progress with attempt>0, no result)
        return self._recover(spec, record, adapter, verifier)

    def _apply(
        self,
        spec: EffectSpec,
        record: ExternalEffectRecord,
        adapter: Adapter,
        verifier: Verifier,
    ) -> EffectOutcome:
        if record.attempt_count >= spec.max_attempts:
            blocked = self._store.update_external_effect_status(
                record.external_effect_id,
                status="blocked_manual_review",
                error="max_attempts_exhausted",
            )
            return EffectOutcome("blocked_manual_review", blocked, should_retry=False)  # type: ignore[arg-type]
        record = self._store.update_external_effect_status(  # type: ignore[assignment]
            record.external_effect_id,
            status="apply_in_progress",
            increment_attempt_count=True,
        )
        try:
            ar = adapter(spec)
        except Exception as exc:
            # Leave apply_in_progress + record error; next attempt enters _recover
            self._store.update_external_effect_status(
                record.external_effect_id,  # type: ignore[union-attr]
                status="apply_in_progress",
                error=str(exc),
            )
            raise
        if ar.applied:
            applied = self._store.update_external_effect_status(
                record.external_effect_id,  # type: ignore[union-attr]
                status="applied",
                result=ar.result,
            )
            return EffectOutcome("applied", applied, should_retry=False)  # type: ignore[arg-type]
        blocked = self._store.update_external_effect_status(
            record.external_effect_id,  # type: ignore[union-attr]
            status="blocked_manual_review",
            result=ar.result,
            error=ar.reason or "adapter_not_applied",
        )
        return EffectOutcome("blocked_manual_review", blocked, should_retry=False)  # type: ignore[arg-type]

    def _recover(
        self,
        spec: EffectSpec,
        record: ExternalEffectRecord,
        adapter: Adapter,
        verifier: Verifier,
    ) -> EffectOutcome:
        verdict = verifier(spec, record)
        updated = self._store.update_external_effect_status(
            record.external_effect_id,
            status=verdict.classification,
            verification=verdict.verification,
            verifier_kind=spec.verifier_kind,
        )
        if verdict.classification == "verified_applied":
            return EffectOutcome("applied", updated, should_retry=False)  # type: ignore[arg-type]
        if verdict.classification == "verified_absent":
            return self._apply(spec, updated, adapter, verifier)  # type: ignore[arg-type]
        return EffectOutcome("blocked_manual_review", updated, should_retry=False)  # type: ignore[arg-type]
