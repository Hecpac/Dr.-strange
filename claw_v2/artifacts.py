from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claw_v2.types import ArtifactKind


ARTIFACT_SCHEMA_VERSION = 102

ARTIFACT_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    trace_id TEXT,
    root_trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    job_id TEXT,
    parent_artifact_id TEXT,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_trace ON artifacts(trace_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts(parent_artifact_id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True, kw_only=True)
class ArtifactRecord:
    summary: str
    artifact_type: ArtifactKind
    payload: dict[str, Any] = field(default_factory=dict)
    artifact_id: str = ""
    created_at: str = field(default_factory=_now)
    trace_id: str | None = None
    root_trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    job_id: str | None = None
    parent_artifact_id: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id:
            self.artifact_id = f"{self.artifact_type}:{uuid.uuid4().hex[:16]}"

    def event_payload(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type,
            "summary": self.summary,
            **self.payload,
        }


@dataclass(slots=True, kw_only=True)
class PlanArtifact(ArtifactRecord):
    artifact_type: ArtifactKind = "plan"


@dataclass(slots=True, kw_only=True)
class ExecutionArtifact(ArtifactRecord):
    artifact_type: ArtifactKind = "execution"


@dataclass(slots=True, kw_only=True)
class VerificationArtifact(ArtifactRecord):
    artifact_type: ArtifactKind = "verification"


@dataclass(slots=True, kw_only=True)
class ApprovalArtifact(ArtifactRecord):
    artifact_type: ArtifactKind = "approval"


@dataclass(slots=True, kw_only=True)
class JobArtifact(ArtifactRecord):
    artifact_type: ArtifactKind = "job"


_ARTIFACT_CLASSES: dict[str, type[ArtifactRecord]] = {
    "plan": PlanArtifact,
    "execution": ExecutionArtifact,
    "verification": VerificationArtifact,
    "approval": ApprovalArtifact,
    "job": JobArtifact,
}


class ArtifactStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(ARTIFACT_SCHEMA)
        self._ensure_schema_version()
        self._lock = threading.Lock()

    def _ensure_schema_version(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        if current < ARTIFACT_SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={ARTIFACT_SCHEMA_VERSION}")
            self._conn.commit()

    def record(self, artifact: ArtifactRecord) -> str:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO artifacts (
                    artifact_id, artifact_type, created_at,
                    trace_id, root_trace_id, span_id, parent_span_id,
                    job_id, parent_artifact_id, summary, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.artifact_type,
                    artifact.created_at,
                    artifact.trace_id,
                    artifact.root_trace_id,
                    artifact.span_id,
                    artifact.parent_span_id,
                    artifact.job_id,
                    artifact.parent_artifact_id,
                    artifact.summary,
                    json.dumps(artifact.payload),
                ),
            )
            self._conn.commit()
        return artifact.artifact_id

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT artifact_id, artifact_type, created_at,
                       trace_id, root_trace_id, span_id, parent_span_id,
                       job_id, parent_artifact_id, summary, payload
                FROM artifacts
                WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        return _artifact_from_row(row) if row else None

    def recent(self, *, limit: int = 20, artifact_type: str | None = None) -> list[ArtifactRecord]:
        query = """
            SELECT artifact_id, artifact_type, created_at,
                   trace_id, root_trace_id, span_id, parent_span_id,
                   job_id, parent_artifact_id, summary, payload
            FROM artifacts
        """
        params: tuple[object, ...] = ()
        if artifact_type is not None:
            query += " WHERE artifact_type = ?"
            params = (artifact_type,)
        query += " ORDER BY created_at DESC LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def trace_artifacts(self, trace_id: str) -> list[ArtifactRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT artifact_id, artifact_type, created_at,
                       trace_id, root_trace_id, span_id, parent_span_id,
                       job_id, parent_artifact_id, summary, payload
                FROM artifacts
                WHERE trace_id = ? OR root_trace_id = ?
                ORDER BY created_at ASC
                """,
                (trace_id, trace_id),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def lineage(self, artifact_id: str) -> list[ArtifactRecord]:
        lineage: list[ArtifactRecord] = []
        seen: set[str] = set()
        current = self.get(artifact_id)
        while current is not None and current.artifact_id not in seen:
            lineage.append(current)
            seen.add(current.artifact_id)
            parent_id = current.parent_artifact_id
            current = self.get(parent_id) if parent_id else None
        return list(reversed(lineage))


def _artifact_from_row(row: tuple[Any, ...]) -> ArtifactRecord:
    cls = _ARTIFACT_CLASSES.get(str(row[1]), ArtifactRecord)
    kwargs = {
        "artifact_id": row[0],
        "artifact_type": row[1],
        "created_at": row[2],
        "trace_id": row[3],
        "root_trace_id": row[4],
        "span_id": row[5],
        "parent_span_id": row[6],
        "job_id": row[7],
        "parent_artifact_id": row[8],
        "summary": row[9],
        "payload": json.loads(row[10]),
    }
    if cls is ArtifactRecord:
        return cls(**kwargs)
    kwargs.pop("artifact_type")
    return cls(**kwargs)
