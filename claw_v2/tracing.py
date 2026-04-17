from __future__ import annotations

import secrets
from typing import Any


TRACE_KEYS: tuple[str, ...] = (
    "trace_id",
    "root_trace_id",
    "span_id",
    "parent_span_id",
    "job_id",
    "artifact_id",
)


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def new_trace_context(
    *,
    job_id: str | None = None,
    artifact_id: str | None = None,
) -> dict[str, str | None]:
    trace_id = new_trace_id()
    return {
        "trace_id": trace_id,
        "root_trace_id": trace_id,
        "span_id": new_span_id(),
        "parent_span_id": None,
        "job_id": job_id,
        "artifact_id": artifact_id,
    }


def child_trace_context(
    parent: dict[str, Any] | None,
    *,
    artifact_id: str | None = None,
) -> dict[str, str | None]:
    parent = parent or {}
    trace_id = str(parent.get("trace_id") or parent.get("root_trace_id") or new_trace_id())
    parent_span_id = parent.get("span_id")
    return {
        "trace_id": trace_id,
        "root_trace_id": str(parent.get("root_trace_id") or trace_id),
        "span_id": new_span_id(),
        "parent_span_id": str(parent_span_id) if parent_span_id else None,
        "job_id": str(parent.get("job_id")) if parent.get("job_id") else None,
        "artifact_id": artifact_id,
    }


def attach_trace(
    evidence_pack: dict[str, Any] | None,
    trace_context: dict[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(evidence_pack or {})
    if trace_context is None:
        return merged
    for key in TRACE_KEYS:
        value = trace_context.get(key)
        if value is not None:
            merged[key] = value
    return merged


def current_llm_trace(evidence_pack: dict[str, Any] | None) -> dict[str, str | None]:
    evidence = dict(evidence_pack or {})
    trace_id = str(evidence.get("trace_id") or evidence.get("root_trace_id") or new_trace_id())
    parent_span_id = evidence.get("span_id") or evidence.get("parent_span_id")
    return {
        "trace_id": trace_id,
        "root_trace_id": str(evidence.get("root_trace_id") or trace_id),
        "span_id": new_span_id(),
        "parent_span_id": str(parent_span_id) if parent_span_id else None,
        "job_id": str(evidence.get("job_id")) if evidence.get("job_id") else None,
        "artifact_id": str(evidence.get("artifact_id")) if evidence.get("artifact_id") else None,
    }


def trace_metadata(evidence_pack: dict[str, Any] | None) -> dict[str, str]:
    evidence = evidence_pack or {}
    data: dict[str, str] = {}
    for key in TRACE_KEYS:
        value = evidence.get(key)
        if value is not None:
            data[key] = str(value)
    return data
