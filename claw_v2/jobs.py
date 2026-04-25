from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
JOB_ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_approval", "retrying"})
JOB_VALID_STATUSES = frozenset({*JOB_ACTIVE_STATUSES, *JOB_TERMINAL_STATUSES})


JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'waiting_approval', 'retrying', 'completed', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    resume_key TEXT UNIQUE,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worker_id TEXT,
    next_run_at REAL,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_next_run
    ON agent_jobs(status, next_run_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_kind_status
    ON agent_jobs(kind, status, updated_at DESC);
"""


@dataclass(slots=True)
class JobRecord:
    job_id: str
    kind: str
    status: str
    payload: dict[str, Any] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    resume_key: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    worker_id: str | None = None
    next_run_at: float | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobService:
    """Durable generic job registry for resumable background work."""

    def __init__(self, db_path: Path | str, *, observe: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.observe = observe
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(JOBS_SCHEMA)
            self._conn.commit()

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        resume_key: str | None = None,
        metadata: dict[str, Any] | None = None,
        max_attempts: int = 3,
        job_id: str | None = None,
    ) -> JobRecord:
        if not kind.strip():
            raise ValueError("job kind is required")
        if resume_key:
            existing = self.get_by_resume_key(resume_key)
            if existing is not None:
                return existing
        now = time.time()
        record = JobRecord(
            job_id=job_id or f"job:{uuid.uuid4().hex[:12]}",
            kind=kind.strip(),
            status="queued",
            payload=dict(payload or {}),
            resume_key=resume_key,
            metadata=dict(metadata or {}),
            max_attempts=max(1, int(max_attempts)),
            next_run_at=now,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_jobs (
                    job_id, kind, status, payload_json, checkpoint_json, result_json,
                    metadata_json, error, resume_key, attempts, max_attempts, worker_id,
                    next_run_at, created_at, started_at, completed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._record_values(record),
            )
            self._conn.commit()
        self._emit("job_enqueued", record)
        return self.get(record.job_id) or record

    def claim_next(
        self,
        *,
        worker_id: str,
        kinds: Iterable[str] | None = None,
        now: float | None = None,
    ) -> JobRecord | None:
        now = time.time() if now is None else now
        kind_list = [kind for kind in (kinds or []) if kind]
        where = "status IN ('queued', 'retrying') AND COALESCE(next_run_at, 0) <= ?"
        params: list[Any] = [now]
        if kind_list:
            placeholders = ", ".join("?" for _ in kind_list)
            where += f" AND kind IN ({placeholders})"
            params.extend(kind_list)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT *
                FROM agent_jobs
                WHERE {where}
                ORDER BY created_at ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            attempts = int(row["attempts"] or 0) + 1
            self._conn.execute(
                """
                UPDATE agent_jobs
                SET status = 'running',
                    worker_id = ?,
                    attempts = ?,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE job_id = ?
                """,
                (worker_id, attempts, now, now, job_id),
            )
            self._conn.commit()
        record = self.get(job_id)
        if record is not None:
            self._emit("job_claimed", record)
        return record

    def checkpoint(self, job_id: str, checkpoint: dict[str, Any]) -> JobRecord | None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE agent_jobs
                SET checkpoint_json = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (json.dumps(dict(checkpoint), sort_keys=True), now, job_id),
            )
            self._conn.commit()
        record = self.get(job_id)
        if record is not None:
            self._emit("job_checkpointed", record)
        return record

    def wait_for_approval(self, job_id: str, *, checkpoint: dict[str, Any] | None = None) -> JobRecord | None:
        return self._update(
            job_id,
            status="waiting_approval",
            checkpoint=checkpoint,
            event_type="job_waiting_approval",
        )

    def complete(self, job_id: str, *, result: dict[str, Any] | None = None) -> JobRecord | None:
        return self._update(
            job_id,
            status="completed",
            result=result,
            completed_at=time.time(),
            event_type="job_completed",
        )

    def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry: bool = True,
        retry_delay_seconds: float = 60.0,
        checkpoint: dict[str, Any] | None = None,
    ) -> JobRecord | None:
        record = self.get(job_id)
        if record is None:
            return None
        if retry and record.attempts < record.max_attempts:
            return self._update(
                job_id,
                status="retrying",
                error=error,
                checkpoint=checkpoint,
                next_run_at=time.time() + max(0.0, retry_delay_seconds),
                event_type="job_retrying",
            )
        return self._update(
            job_id,
            status="failed",
            error=error,
            checkpoint=checkpoint,
            completed_at=time.time(),
            event_type="job_failed",
        )

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> JobRecord | None:
        record = self.get(job_id)
        if record is None:
            return None
        if record.status in JOB_TERMINAL_STATUSES:
            return record
        return self._update(
            job_id,
            status="cancelled",
            error=reason,
            completed_at=time.time(),
            event_type="job_cancelled",
        )

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_by_resume_key(self, resume_key: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agent_jobs WHERE resume_key = ?", (resume_key,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list(
        self,
        *,
        statuses: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[JobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if statuses is not None:
            status_list = list(statuses)
            for status in status_list:
                self._validate_status(status)
            if status_list:
                placeholders = ", ".join("?" for _ in status_list)
                clauses.append(f"status IN ({placeholders})")
                params.extend(status_list)
        if kinds is not None:
            kind_list = [kind for kind in kinds if kind]
            if kind_list:
                placeholders = ", ".join("?" for _ in kind_list)
                clauses.append(f"kind IN ({placeholders})")
                params.extend(kind_list)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM agent_jobs {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def summary(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS count FROM agent_jobs GROUP BY status",
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def resume_candidates(self, *, limit: int = 20) -> list[JobRecord]:
        return self.list(statuses=("queued", "running", "waiting_approval", "retrying"), limit=limit)

    def _update(
        self,
        job_id: str,
        *,
        status: str,
        checkpoint: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        next_run_at: float | None = None,
        completed_at: float | None = None,
        event_type: str,
    ) -> JobRecord | None:
        self._validate_status(status)
        now = time.time()
        assignments = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        if checkpoint is not None:
            assignments.append("checkpoint_json = ?")
            params.append(json.dumps(dict(checkpoint), sort_keys=True))
        if result is not None:
            assignments.append("result_json = ?")
            params.append(json.dumps(dict(result), sort_keys=True))
        if error is not None:
            assignments.append("error = ?")
            params.append(error)
        if next_run_at is not None:
            assignments.append("next_run_at = ?")
            params.append(next_run_at)
        if completed_at is not None:
            assignments.append("completed_at = ?")
            params.append(completed_at)
        params.append(job_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE agent_jobs SET {', '.join(assignments)} WHERE job_id = ?",
                params,
            )
            self._conn.commit()
        record = self.get(job_id)
        if record is not None:
            self._emit(event_type, record)
        return record

    def _record_values(self, record: JobRecord) -> tuple[Any, ...]:
        return (
            record.job_id,
            record.kind,
            record.status,
            json.dumps(record.payload, sort_keys=True),
            json.dumps(record.checkpoint, sort_keys=True),
            json.dumps(record.result, sort_keys=True),
            json.dumps(record.metadata, sort_keys=True),
            record.error,
            record.resume_key,
            record.attempts,
            record.max_attempts,
            record.worker_id,
            record.next_run_at,
            record.created_at,
            record.started_at,
            record.completed_at,
            record.updated_at,
        )

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=str(row["job_id"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            payload=_loads_json(row["payload_json"]),
            checkpoint=_loads_json(row["checkpoint_json"]),
            result=_loads_json(row["result_json"]),
            metadata=_loads_json(row["metadata_json"]),
            error=str(row["error"] or ""),
            resume_key=_as_optional_str(row["resume_key"]),
            attempts=int(row["attempts"] or 0),
            max_attempts=int(row["max_attempts"] or 1),
            worker_id=_as_optional_str(row["worker_id"]),
            next_run_at=_as_optional_float(row["next_run_at"]),
            created_at=float(row["created_at"]),
            started_at=_as_optional_float(row["started_at"]),
            completed_at=_as_optional_float(row["completed_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in JOB_VALID_STATUSES:
            raise ValueError(f"invalid job status: {status}")

    def _emit(self, event_type: str, record: JobRecord) -> None:
        if self.observe is None:
            return
        self.observe.emit(
            event_type,
            lane="job_service",
            job_id=record.job_id,
            payload=record.to_dict(),
        )


def _loads_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
