"""NotebookLM research effect spec, adapter, and verifier for F2 durable lane.

These three callables are the NotebookLM-specific plugin for
F2ExternalEffectExecutor. They carry no daemon wiring or feature-flag logic;
that belongs in Phase 3.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from claw_v2.external_effect_executor import AdapterResult, EffectSpec, VerifierVerdict
from claw_v2.f2_durability_store import ExternalEffectRecord


def build_research_effect_spec(
    *,
    job_id: str,
    notebook_id: str,
    query: str,
    mode: str,
    pre_intent_source_count: int,
    task_id: str | None = None,
) -> EffectSpec:
    """Build a fully-populated EffectSpec for a notebooklm_research effect."""
    content_hash = hashlib.sha256(f"{query}|{mode}".encode()).hexdigest()
    return EffectSpec(
        task_id=task_id or job_id,
        run_id=job_id,
        phase="research",
        effect_kind="notebooklm_research",
        target=notebook_id,
        request={
            "notebook_id": notebook_id,
            "query": query,
            "mode": mode,
            "pre_intent_source_count": pre_intent_source_count,
        },
        content_hash=content_hash,
        job_id=job_id,
        verifier_kind="notebooklm_research",
    )


def notebooklm_research_adapter(
    deep_research_fn: Callable[[str, str], Any],
) -> Callable[[EffectSpec], AdapterResult]:
    """Wrap a deep_research callable as an F2 Adapter.

    deep_research_fn(notebook_id, query) -> int | None  (imported source count)
    Classification: imported_count > 0 → applied; == 0 → not applied (zero_imports).
    """

    def adapter(spec: EffectSpec) -> AdapterResult:
        notebook_id: str = spec.request["notebook_id"]
        query: str = spec.request["query"]
        imported = int(deep_research_fn(notebook_id, query) or 0)
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
    """

    def verify(spec: EffectSpec, record: ExternalEffectRecord) -> VerifierVerdict:
        # 1. Adapter committed its result before the crash → already applied.
        if record.result_json:
            return VerifierVerdict(
                "verified_applied",
                {"source": "result_json"},
                "result_present",
            )

        # 2. Need live count to decide.
        pre = int(spec.request.get("pre_intent_source_count", -1))
        if pre < 0:
            # pre_intent_source_count not recorded → cannot make a safe decision.
            return VerifierVerdict(
                "blocked_manual_review",
                {"pre": pre, "current": None},
                "pre_count_unknown",
            )

        try:
            raw = status_fn(spec.target) or {}
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

        current = int(current_raw)
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
