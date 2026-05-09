"""Verifier swap point for the Petri-backed evidence verifier.

Spec: ``docs/superpowers/specs/2026-05-01-petri-evidence-verifier-design.md``
section 4.5 + commit #8.

This module owns the decision of whether the legacy ``verify_change`` worker
or the new Petri judge runs for a given task. The decision is gated by:

1. ``CLAW_PETRI_VERIFIER_ENABLED`` env flag (default ``"0"`` until commit #9
   ships and we have soak-monitor data).
2. Per-task opt-in: tasks whose metadata declares ``verify="strict"`` are
   eligible. Routine tasks keep using the legacy verifier even when the flag
   is on, so the cost increase ($0.10 -> $0.15 per verification) is bounded.

The orchestrator does NOT modify the legacy verifier path. The existing
``verify_change`` worker continues to run unchanged. Wiring the runtime to
consult :func:`should_use_petri_verifier` and dispatch accordingly is the
job of commit #9, after we have observed Petri scores against real
transcripts on a per-task opt-in basis.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claw_v2.verification.judge import (
    JudgeDimension,
    JudgeFn,
    JudgeReport,
    load_dimensions,
    run_judge,
)
from claw_v2.verification.transcript import read_target_stream

logger = logging.getLogger(__name__)


PETRI_VERIFIER_ENV_FLAG = "CLAW_PETRI_VERIFIER_ENABLED"
"""Env flag name. Default ``"0"`` (legacy verifier). Flip to ``"1"`` to
enable Petri for ``verify=strict`` tasks. The flag stays here even after
commit #9 changes the default so operators can roll back without a deploy."""


def petri_verifier_enabled(env: dict[str, str] | None = None) -> bool:
    """Return True iff the Petri verifier should be considered for any task.

    Reads ``CLAW_PETRI_VERIFIER_ENABLED`` from the supplied mapping or
    ``os.environ``. Treats ``"1"`` / ``"true"`` / ``"yes"`` (case-insensitive)
    as enabled; everything else is disabled."""
    source = env if env is not None else os.environ
    raw = str(source.get(PETRI_VERIFIER_ENV_FLAG, "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def should_use_petri_verifier(
    task_metadata: dict[str, Any] | None,
    *,
    env: dict[str, str] | None = None,
) -> bool:
    """Decide whether ``task`` should be verified by the Petri judge.

    Two conditions must both hold:

    1. The env flag is enabled (operator opt-in across the runtime).
    2. ``task_metadata["verify"] == "strict"`` (per-task opt-in).

    Either guard alone is enough to keep a task on the legacy verifier."""
    if not petri_verifier_enabled(env=env):
        return False
    if not task_metadata:
        return False
    return str(task_metadata.get("verify", "")).strip().lower() == "strict"


@dataclass(frozen=True, slots=True)
class PetriRunOutcome:
    report: JudgeReport
    dimensions_used: tuple[str, ...]
    transcript_records: int


def run_petri_judge_for_task(
    *,
    task_id: str,
    telemetry_root: Path | str,
    dimensions_root: Path | str,
    judge_fn: JudgeFn,
    extra_dimensions: list[JudgeDimension] | None = None,
) -> PetriRunOutcome:
    """Load the target stream for ``task_id`` and run the judge.

    The harness stream is intentionally NOT loaded — spec section 4.4 forbids
    it. The judge call MUST run in a context isolated from the target agent;
    that obligation lives in the injected ``judge_fn``.

    Returns a :class:`PetriRunOutcome` with the report and metadata about
    what was scored. Callers persist the report into the evidence record;
    that wiring lands in commit #9.
    """
    target_records = read_target_stream(telemetry_root, task_id)
    if not target_records:
        raise ValueError(
            f"target stream is empty for task_id={task_id!r}; cannot run judge"
        )
    dimensions = list(load_dimensions(dimensions_root))
    if extra_dimensions:
        dimensions.extend(extra_dimensions)
    if not dimensions:
        raise ValueError("no dimensions found; cannot run judge")
    logger.debug(
        "running petri judge task_id=%s records=%d dimensions=%d",
        task_id,
        len(target_records),
        len(dimensions),
    )
    report = run_judge(
        task_id=task_id,
        target_records=target_records,
        dimensions=dimensions,
        judge_fn=judge_fn,
    )
    return PetriRunOutcome(
        report=report,
        dimensions_used=tuple(d.name for d in dimensions),
        transcript_records=len(target_records),
    )
