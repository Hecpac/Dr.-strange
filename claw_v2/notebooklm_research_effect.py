"""NotebookLM research effect spec, adapter, and verifier for F2 durable lane.

These three callables are the NotebookLM-specific plugin for
F2ExternalEffectExecutor. They carry no daemon wiring or feature-flag logic;
that belongs in Phase 3.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from claw_v2.external_effect_executor import AdapterResult, EffectSpec, VerifierVerdict
from claw_v2.f2_durability_store import ExternalEffectRecord

_EFFECT_KIND = "notebooklm_research"
_PHASE = "research"


def imported_count_from_result(result_json: str | None) -> int | None:
    """Extract imported_count from a stored result JSON string.

    Returns None when the result is absent, unparseable, not a dict, missing the
    key, or non-int — so callers can fail closed on anything other than a clean
    positive count. Shared by the verifier (here) and the runner's completion
    metadata.
    """
    if not result_json:
        return None
    try:
        data = json.loads(result_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or "imported_count" not in data:
        return None
    try:
        return int(data["imported_count"])
    except (ValueError, TypeError):
        return None


def research_content_hash(query: str, mode: str) -> str:
    """Stable content hash for a (query, mode) research request.

    Used both for ``EffectSpec.content_hash`` (idempotency-key input) and the
    ``JobService`` resume_key in ``start_research`` — they MUST agree, so they
    share this single definition.
    """
    return hashlib.sha256(f"{query}|{mode}".encode()).hexdigest()


def build_research_effect_spec(
    *,
    job_id: str,
    notebook_id: str,
    query: str,
    mode: str,
    pre_intent_source_count: int,
    task_id: str | None = None,
    max_attempts: int = 3,
) -> EffectSpec:
    """Build a fully-populated EffectSpec for a notebooklm_research effect.

    ``max_attempts`` bounds the effect-level apply budget (spec §7: "bounded by
    the job's max_attempts"); the runner passes the originating job's value.
    """
    if not isinstance(pre_intent_source_count, int) or pre_intent_source_count < 0:
        raise ValueError(
            f"pre_intent_source_count must be a non-negative int, got {pre_intent_source_count!r}"
        )
    content_hash = research_content_hash(query, mode)
    return EffectSpec(
        task_id=task_id or job_id,
        run_id=job_id,
        phase=_PHASE,
        effect_kind=_EFFECT_KIND,
        target=notebook_id,
        request={
            "notebook_id": notebook_id,
            "query": query,
            "mode": mode,
            "pre_intent_source_count": pre_intent_source_count,
        },
        content_hash=content_hash,
        job_id=job_id,
        verifier_kind=_EFFECT_KIND,
        max_attempts=max_attempts,
    )


def notebooklm_research_adapter(
    deep_research_fn: Callable[[str, str], Any],
) -> Callable[[EffectSpec], AdapterResult]:
    """Wrap a deep_research callable as an F2 Adapter.

    deep_research_fn(notebook_id, query) -> int | None  (imported source count)
    Classification: imported_count > 0 → applied; <= 0 → not applied (zero_imports).
    A negative return is an anomaly and is clamped to 0 (not applied).
    """

    def adapter(spec: EffectSpec) -> AdapterResult:
        notebook_id: str = spec.request["notebook_id"]
        query: str = spec.request["query"]
        imported = max(0, int(deep_research_fn(notebook_id, query) or 0))
        return AdapterResult(
            applied=imported > 0,
            result={"imported_count": imported},
            reason=None if imported > 0 else "zero_imports",
        )

    return adapter


def notebooklm_research_verifier(
    status_fn: Callable[[str], dict[str, Any]],
) -> Callable[[EffectSpec, ExternalEffectRecord], VerifierVerdict]:
    """Build a recovery verifier for notebooklm_research effects.

    status_fn(notebook_id) -> {"source_count": int}

    Recovery policy (§5 of the spec):
    - result_json present on the record → verified_applied (adapter finished).
    - count unchanged vs pre_intent_source_count, no result → verified_absent.
    - count moved, or pre unknown, or status_fn raises/missing → blocked_manual_review.

    CRITICAL (dedup guarantee, §3/§5): the baseline (pre_intent_source_count) is
    read from the PERSISTED RECORD — the first-intent request — NOT from the
    ``spec`` argument. On a crash-recovery the runner rebuilds the spec with the
    CURRENT live count as its "pre", so comparing the spec's pre against the live
    count would always look unchanged → false verified_absent → re-run → duplicate
    imported sources. The record's request_json (preserved by
    ``record_external_effect``'s ON CONFLICT DO NOTHING) holds the TRUE original
    baseline, so everything the verifier decides on is derived from the record.
    """

    def verify(_spec: EffectSpec, record: ExternalEffectRecord) -> VerifierVerdict:
        # 1. Adapter committed its result before the crash. Only a positive
        # imported_count is a clean apply (spec §5: "result_json exists AND
        # imported_count > 0"). A committed result with no imports — or an
        # unparseable result — is not a silent success; fail closed.
        if record.result_json:
            imported = imported_count_from_result(record.result_json)
            if imported is not None and imported > 0:
                return VerifierVerdict(
                    "verified_applied",
                    {"source": "result_json", "imported_count": imported},
                    "result_present",
                )
            return VerifierVerdict(
                "blocked_manual_review",
                {"source": "result_json", "imported_count": imported},
                "result_present_but_no_imports",
            )

        # 2. Baseline comes from the PERSISTED RECORD (first intent), not the
        # rebuilt spec. record.request is the parsed dict (None on a JSON parse
        # error). Fail closed if the original request / baseline is unavailable.
        record_request = record.request
        if not isinstance(record_request, dict) or "pre_intent_source_count" not in record_request:
            return VerifierVerdict(
                "blocked_manual_review",
                {"pre": None, "current": None},
                "pre_count_unavailable",
            )
        pre_raw = record_request.get("pre_intent_source_count")
        try:
            pre = int(pre_raw)
        except (ValueError, TypeError):
            return VerifierVerdict(
                "blocked_manual_review",
                {"pre": pre_raw, "current": None},
                "pre_count_not_int",
            )
        if pre < 0:
            # pre_intent_source_count not recorded → cannot make a safe decision.
            return VerifierVerdict(
                "blocked_manual_review",
                {"pre": pre, "current": None},
                "pre_count_unknown",
            )

        try:
            raw = status_fn(record.target) or {}
            current_raw = raw.get("source_count")
        except Exception as exc:
            return VerifierVerdict(
                "blocked_manual_review",
                {"error": str(exc)},
                "status_unavailable",
            )

        if current_raw is None:
            return VerifierVerdict(
                "blocked_manual_review",
                {"pre": pre, "current": None},
                "source_count_missing",
            )

        try:
            current = int(current_raw)
        except (ValueError, TypeError):
            return VerifierVerdict(
                "blocked_manual_review",
                {"pre": pre, "current": current_raw},
                "source_count_not_int",
            )
        if current == pre:
            return VerifierVerdict(
                "verified_absent",
                {"pre": pre, "current": current},
                "count_unchanged",
            )
        return VerifierVerdict(
            "blocked_manual_review",
            {"pre": pre, "current": current},
            "count_moved_or_unknown",
        )

    return verify
