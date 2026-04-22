from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


JOB_SCHEMA_VERSION = 103
JobState = Literal["queued", "running", "waiting_approval", "completed", "failed", "cancelled"]
StepState = Literal["pending", "running", "completed", "failed", "skipped"]
TERMINAL_JOB_STATES = frozenset({"completed", "failed", "cancelled"})

JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'queued',
    version INTEGER NOT NULL DEFAULT 1,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS job_steps (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt_id TEXT NOT NULL,
    operation_hash TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    step_class TEXT NOT NULL DEFAULT 'pure',
    side_effect_ref TEXT,
    result_artifact_id TEXT,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
"""


@dataclass(slots=True)
class JobRecord:
    job_id: str
    state: str
    payload: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def kind(self) -> str:
        return str(self.payload.get("kind") or "job")


@dataclass(slots=True)
class JobStepRecord:
    step_id: str
    job_id: str
    name: str
    state: str
    attempt_id: str
    operation_hash: str
    idempotency_key: str
    step_class: str = "pure"
    side_effect_ref: str | None = None
    result_artifact_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


def job_from_row(row: tuple[Any, ...]) -> JobRecord:
    return JobRecord(
        job_id=row[0],
        state=row[1],
        payload=json.loads(row[2]),
        version=int(row[3]),
        lease_owner=row[4],
        lease_expires_at=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


def step_from_row(row: tuple[Any, ...]) -> JobStepRecord:
    return JobStepRecord(
        step_id=row[0],
        job_id=row[1],
        name=row[2],
        state=row[3],
        attempt_id=row[4],
        operation_hash=row[5],
        idempotency_key=row[6],
        step_class=row[7],
        side_effect_ref=row[8],
        result_artifact_id=row[9],
        started_at=row[10],
        completed_at=row[11],
    )


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def operation_hash(name: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"name": name, "payload": payload}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
