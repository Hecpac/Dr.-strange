from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.redaction import redact_sensitive
from claw_v2.telemetry import append_jsonl, generate_id, now_iso, read_jsonl

RECALL_REQUEST_SCHEMA_VERSION = "recall_request.v1"
RECALL_RESULT_SCHEMA_VERSION = "recall_result.v1"


@dataclass(slots=True)
class RecallRequest:
    request_id: str
    goal_id: str
    session_id: str
    query: str
    risk_level: str
    action_tier: str
    requested_at: str = field(default_factory=now_iso)
    schema_version: str = RECALL_REQUEST_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "goal_id": self.goal_id,
            "session_id": self.session_id,
            "query": self.query,
            "risk_level": self.risk_level,
            "action_tier": self.action_tier,
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecallRequest":
        return cls(
            request_id=str(data["request_id"]),
            goal_id=str(data["goal_id"]),
            session_id=str(data["session_id"]),
            query=str(data["query"]),
            risk_level=str(data.get("risk_level") or "low"),
            action_tier=str(data.get("action_tier") or "tier_1"),
            requested_at=str(data.get("requested_at") or now_iso()),
            schema_version=str(data.get("schema_version") or RECALL_REQUEST_SCHEMA_VERSION),
        )


@dataclass(slots=True)
class RecallHit:
    memory_id: str
    summary: str
    relevance: float
    source: str
    evidence_refs: list[str] = field(default_factory=list)
    staleness: str = "usable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "summary": self.summary[:240],
            "relevance": round(float(self.relevance), 4),
            "source": self.source,
            "evidence_refs": list(self.evidence_refs),
            "staleness": self.staleness,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecallHit":
        return cls(
            memory_id=str(data["memory_id"]),
            summary=str(data["summary"]),
            relevance=float(data.get("relevance") or 0.0),
            source=str(data.get("source") or "memory"),
            evidence_refs=[str(item) for item in data.get("evidence_refs", [])],
            staleness=str(data.get("staleness") or "usable"),
        )


@dataclass(slots=True)
class QualityGate:
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reason": self.reason[:240]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QualityGate":
        return cls(passed=bool(data.get("passed")), reason=str(data.get("reason") or ""))


@dataclass(slots=True)
class RecallResult:
    request_id: str
    goal_id: str
    hits: list[RecallHit] = field(default_factory=list)
    quality_gate: QualityGate = field(default_factory=lambda: QualityGate(True, "recorded"))
    recorded_at: str = field(default_factory=now_iso)
    schema_version: str = RECALL_RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "goal_id": self.goal_id,
            "hits": [hit.to_dict() for hit in self.hits],
            "quality_gate": self.quality_gate.to_dict(),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecallResult":
        return cls(
            request_id=str(data["request_id"]),
            goal_id=str(data["goal_id"]),
            hits=[RecallHit.from_dict(item) for item in data.get("hits", [])],
            quality_gate=QualityGate.from_dict(data.get("quality_gate") or {}),
            recorded_at=str(data.get("recorded_at") or now_iso()),
            schema_version=str(data.get("schema_version") or RECALL_RESULT_SCHEMA_VERSION),
        )


def request_recall(
    telemetry_root: Path | str,
    *,
    goal_id: str,
    session_id: str,
    query: str,
    risk_level: str,
    action_tier: str,
    observe: Any | None = None,
) -> RecallRequest:
    request = RecallRequest(
        request_id=generate_id("r"),
        goal_id=goal_id,
        session_id=session_id,
        query=query,
        risk_level=risk_level,
        action_tier=action_tier,
        requested_at=now_iso(),
    )
    payload = request.to_dict()
    append_jsonl(_recall_path(telemetry_root), payload)
    if observe is not None:
        observe.emit("recall_requested", payload=payload)
    return request


def record_recall_result(
    telemetry_root: Path | str,
    result: RecallResult,
    *,
    observe: Any | None = None,
) -> RecallResult:
    payload = result.to_dict()
    append_jsonl(_recall_path(telemetry_root), payload)
    if observe is not None:
        observe.emit("recall_result_recorded", payload=payload)
    return result


def search_recall_hits(query: str, candidates: list[dict[str, Any]], *, limit: int = 5) -> list[RecallHit]:
    query_tokens = _tokens(query)
    hits: list[RecallHit] = []
    for index, candidate in enumerate(candidates):
        summary = str(candidate.get("summary") or candidate.get("text") or "")
        if not summary.strip():
            continue
        candidate_tokens = _tokens(summary)
        relevance = _jaccard(query_tokens, candidate_tokens)
        if relevance <= 0:
            continue
        hits.append(
            RecallHit(
                memory_id=str(candidate.get("memory_id") or candidate.get("id") or f"m_{index}"),
                summary=summary,
                relevance=relevance,
                source=str(candidate.get("source") or "memory"),
                evidence_refs=[str(item) for item in candidate.get("evidence_refs", [])],
                staleness=str(candidate.get("staleness") or "usable"),
            )
        )
    return sorted(hits, key=lambda item: item.relevance, reverse=True)[: max(1, limit)]


def quality_gate_for_reflection(
    *,
    goal_id: str | None,
    outcome: str | None,
    evidence_refs: list[str] | None,
    lesson: str,
) -> QualityGate:
    if not goal_id:
        return QualityGate(False, "missing goal_id")
    if outcome not in {"passed", "failed", "blocked"}:
        return QualityGate(False, "missing verifiable outcome")
    if not evidence_refs:
        return QualityGate(False, "missing evidence_refs")
    if redact_sensitive(lesson) != lesson:
        return QualityGate(False, "lesson contains sensitive material")
    if len(_tokens(lesson)) < 5:
        return QualityGate(False, "lesson is too thin to generalize")
    return QualityGate(True, "reflection passes quality gate")


def load_recall_records(telemetry_root: Path | str) -> list[dict[str, Any]]:
    return read_jsonl(_recall_path(telemetry_root))


def _recall_path(telemetry_root: Path | str) -> Path:
    return Path(telemetry_root).expanduser() / "recall.jsonl"


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", text.lower()) if len(token) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

