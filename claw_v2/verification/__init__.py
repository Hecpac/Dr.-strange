"""Petri-backed evidence verifier (spec 2026-05-01).

This package owns the verifier work scoped in
``docs/superpowers/specs/2026-05-01-petri-evidence-verifier-design.md``.

Today this module exposes only the v2 telemetry transcript writer/reader
(commit #6). The judge wiring (commit #7), the verifier swap (commit #8),
and the default-on flip (commit #9) land in subsequent commits.
"""
from __future__ import annotations

from claw_v2.verification.judge import (
    DimensionRawResponse,
    DimensionScore,
    JudgeDimension,
    JudgeReport,
    build_dimension_prompt,
    load_dimensions,
    parse_judge_response,
    render_target_transcript,
    run_judge,
)
from claw_v2.verification.runner import (
    PETRI_VERIFIER_ENV_FLAG,
    PetriRunOutcome,
    petri_verifier_enabled,
    run_petri_judge_for_task,
    should_use_petri_verifier,
)
from claw_v2.verification.transcript import (
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptStream,
    harness_stream_path,
    read_harness_stream,
    read_target_stream,
    record_harness_event,
    record_target_event,
    target_stream_path,
)

__all__ = [
    "PETRI_VERIFIER_ENV_FLAG",
    "TRANSCRIPT_SCHEMA_VERSION",
    "DimensionRawResponse",
    "DimensionScore",
    "JudgeDimension",
    "JudgeReport",
    "PetriRunOutcome",
    "TranscriptStream",
    "build_dimension_prompt",
    "harness_stream_path",
    "load_dimensions",
    "parse_judge_response",
    "petri_verifier_enabled",
    "read_harness_stream",
    "read_target_stream",
    "record_harness_event",
    "record_target_event",
    "render_target_transcript",
    "run_judge",
    "run_petri_judge_for_task",
    "should_use_petri_verifier",
    "target_stream_path",
]
