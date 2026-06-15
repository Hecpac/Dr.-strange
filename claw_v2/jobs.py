from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from claw_v2.sqlite_runtime import (
    WAL_HEAL_RETRY_LIMIT,
    RuntimeDb,
    connect_runtime_sqlite,
    heal_wal_after_closed_connection,
    heal_wal_after_disk_io,
    make_store_wal_heal,
    register_wal_heal,
)


JOB_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
JOB_ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_approval", "retrying"})
JOB_VALID_STATUSES = frozenset({*JOB_ACTIVE_STATUSES, *JOB_TERMINAL_STATUSES})


JOBS_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'waiting_approval', 'retrying', 'completed', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    resume_key TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    worker_id TEXT,
    next_run_at REAL,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    updated_at REAL NOT NULL
);
"""

JOBS_INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_next_run
    ON agent_jobs(status, next_run_at, updated_at);

CREATE INDEX IF NOT EXISTS idx_agent_jobs_kind_status
    ON agent_jobs(kind, status, updated_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_active_resume_key
    ON agent_jobs(resume_key)
    WHERE resume_key IS NOT NULL
      AND status IN ('queued', 'running', 'waiting_approval', 'retrying');
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

    def __init__(
        self,
        db_path: Path | str,
        *,
        observe: Any | None = None,
        runtime_db: RuntimeDb | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.observe = observe
        if runtime_db is not None:
            # F1.1a1 production path: share the single RuntimeDb connection +
            # lock; RuntimeDb owns the connection lifecycle and the heal handle.
            self._db: RuntimeDb | None = runtime_db
            self._conn = runtime_db.connection_handle(row_factory=True)
            self._lock = runtime_db.lock
        else:
            # Transitional test/back-compat path (not used by main.py).
            self._db = None
            self._conn = connect_runtime_sqlite(self.db_path)
            register_wal_heal(self.db_path, make_store_wal_heal(self))
            self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(JOBS_TABLE_SCHEMA)
            self._migrate_resume_key_uniqueness()
            self._conn.executescript(JOBS_INDEX_SCHEMA)
            self._conn.commit()

    def _retry_after_disk_io(self, operation: str, callback):
        # M5: a burst of concurrent heals can re-close this connection during
        # the post-heal retry, so absorb a bounded run of heals (not exactly
        # one) before giving up — heals coalesce, so the run converges fast.
        heals = 0
        while True:
            try:
                return callback()
            except sqlite3.OperationalError as exc:
                if heals < WAL_HEAL_RETRY_LIMIT and heal_wal_after_disk_io(
                    self.db_path, exc, context=operation
                ):
                    heals += 1
                    continue
                raise
            except sqlite3.ProgrammingError as exc:
                if heals < WAL_HEAL_RETRY_LIMIT and heal_wal_after_closed_connection(
                    self.db_path, exc, context=operation
                ):
                    heals += 1
                    continue
                raise

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
            existing = self.get_active_by_resume_key(resume_key)
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
            try:
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
            except sqlite3.IntegrityError:
                self._conn.rollback()
                if resume_key:
                    existing = self._get_active_by_resume_key_unlocked(resume_key)
                    if existing is not None:
                        return existing
                raise
        self._emit("job_enqueued", record)
        return self.get(record.job_id) or record

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: float | None = None,
    ) -> JobRecord | None:
        now = time.time() if now is None else now

        def claim_once() -> bool:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        """
                        SELECT *
                        FROM agent_jobs
                        WHERE job_id = ?
                          AND status IN ('queued', 'retrying')
                        """,
                        (job_id,),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return False
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
                          AND status IN ('queued', 'retrying')
                        """,
                        (worker_id, attempts, now, now, job_id),
                    )
                    self._conn.commit()
                    return True
                except Exception:
                    self._conn.rollback()
                    raise

        if not self._retry_after_disk_io("JobService.claim", claim_once):
            return None
        record = self.get(job_id)
        if record is not None:
            self._emit("job_claimed", record)
        return record

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

        def claim_next_once() -> str | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
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
                        self._conn.commit()
                        return None
                    claimed_job_id = str(row["job_id"])
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
                          AND status IN ('queued', 'retrying')
                        """,
                        (worker_id, attempts, now, now, claimed_job_id),
                    )
                    self._conn.commit()
                    return claimed_job_id
                except Exception:
                    self._conn.rollback()
                    raise

        job_id = self._retry_after_disk_io("JobService.claim_next", claim_next_once)
        if job_id is None:
            return None
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

    def wait_for_approval(
        self, job_id: str, *, checkpoint: dict[str, Any] | None = None
    ) -> JobRecord | None:
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
        now = time.time()

        def fail_once() -> JobRecord | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None
                    if row["status"] in JOB_TERMINAL_STATUSES:
                        # Idempotent: a job already terminal must not be moved back to
                        # failed/retrying. Return the row we just read (NOT self.get(),
                        # which would re-acquire the non-reentrant lock).
                        self._conn.commit()
                        return self._row_to_record(row)
                    should_retry = retry and int(row["attempts"] or 0) < int(
                        row["max_attempts"] or 1
                    )
                    status = "retrying" if should_retry else "failed"
                    completed_at = None if should_retry else now
                    next_run_at = (
                        now + max(0.0, retry_delay_seconds) if should_retry else row["next_run_at"]
                    )
                    checkpoint_json = (
                        json.dumps(dict(checkpoint), sort_keys=True)
                        if checkpoint is not None
                        else row["checkpoint_json"]
                    )
                    self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET status = ?,
                            error = ?,
                            checkpoint_json = ?,
                            next_run_at = ?,
                            completed_at = ?,
                            updated_at = ?
                        WHERE job_id = ?
                        """,
                        (status, error, checkpoint_json, next_run_at, completed_at, now, job_id),
                    )
                    self._conn.commit()
                    return None
                except Exception:
                    self._conn.rollback()
                    raise

        terminal_record = self._retry_after_disk_io("JobService.fail", fail_once)
        if terminal_record is not None:
            return terminal_record
        record = self.get(job_id)
        if record is not None:
            self._emit("job_retrying" if record.status == "retrying" else "job_failed", record)
        return record

    def reschedule(
        self,
        job_id: str,
        *,
        checkpoint: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        next_run_at: float | None = None,
    ) -> JobRecord | None:
        """Move a claimed job back to retrying without recording a failure.

        This is for durable pollers whose current observation is legitimately
        pending, e.g. a provider/UI still generating an artifact.
        """
        return self._update(
            job_id,
            status="retrying",
            checkpoint=checkpoint,
            result=result,
            next_run_at=time.time() if next_run_at is None else next_run_at,
            event_type="job_rescheduled",
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
            row = self._conn.execute(
                "SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_by_resume_key(self, resume_key: str) -> JobRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM agent_jobs
                WHERE resume_key = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (resume_key,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def get_active_by_resume_key(self, resume_key: str) -> JobRecord | None:
        with self._lock:
            return self._get_active_by_resume_key_unlocked(resume_key)

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

        def list_once() -> list[sqlite3.Row]:
            with self._lock:
                return self._conn.execute(
                    f"SELECT * FROM agent_jobs {where} ORDER BY updated_at DESC LIMIT ?",
                    params,
                ).fetchall()

        rows = self._retry_after_disk_io("JobService.list", list_once)
        return [self._row_to_record(row) for row in rows]

    def summary(self) -> dict[str, int]:
        def summary_once() -> list[sqlite3.Row]:
            with self._lock:
                return self._conn.execute(
                    "SELECT status, COUNT(*) AS count FROM agent_jobs GROUP BY status",
                ).fetchall()

        rows = self._retry_after_disk_io("JobService.summary", summary_once)
        return {str(row["status"]): int(row["count"]) for row in rows}

    def resume_candidates(self, *, limit: int = 20) -> list[JobRecord]:
        return self.list(
            statuses=("queued", "running", "waiting_approval", "retrying"), limit=limit
        )

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

        def update_once() -> JobRecord | sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    current = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    if current is None:
                        self._conn.commit()
                        return None
                    if current["status"] in JOB_TERMINAL_STATUSES:
                        # Idempotent: never resurrect a terminal job. BEGIN IMMEDIATE
                        # holds the write lock across this read+UPDATE so a sibling
                        # connection cannot flip the row terminal between them. Return
                        # the row we just read (NOT self.get(), which would re-acquire
                        # the non-reentrant lock).
                        self._conn.commit()
                        return self._row_to_record(current)
                    self._conn.execute(
                        f"UPDATE agent_jobs SET {', '.join(assignments)} WHERE job_id = ?",
                        params,
                    )
                    # Read the fresh row inside the same locked transaction: a later
                    # self.get() would re-acquire the lock and reopen a window for a
                    # sibling connection to mutate the row before we read it back.
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        updated = self._retry_after_disk_io(f"JobService.{event_type}", update_once)
        if isinstance(updated, JobRecord):
            return updated
        record = self._row_to_record(updated) if updated is not None else None
        if record is not None:
            self._emit(event_type, record)
        return record

    def _get_active_by_resume_key_unlocked(self, resume_key: str) -> JobRecord | None:
        row = self._conn.execute(
            """
            SELECT *
            FROM agent_jobs
            WHERE resume_key = ?
              AND status IN ('queued', 'running', 'waiting_approval', 'retrying')
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (resume_key,),
        ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def _migrate_resume_key_uniqueness(self) -> None:
        """Crash-safe resume-key migration (mirrors memory.py's pattern).

        Handles steady, legacy (``resume_key TEXT UNIQUE`` table shape), and
        orphan states (an ``agent_jobs_legacy_*`` table left by a crash
        mid-migration — previously never drained, silently losing the whole
        job queue). One BEGIN IMMEDIATE; counts verified before dropping.
        """
        row = self._conn.execute(
            """
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'agent_jobs'
            """
        ).fetchone()
        table_sql = str(row["sql"] or "") if row is not None else ""
        needs_migration = "resume_key TEXT UNIQUE" in table_sql
        orphan_row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'agent_jobs_legacy_%' LIMIT 1"
        ).fetchone()
        orphan_table = str(orphan_row["name"]) if orphan_row is not None else None
        if not needs_migration and orphan_table is None:
            return
        columns = (
            "job_id, kind, status, payload_json, checkpoint_json, result_json, "
            "metadata_json, error, resume_key, attempts, max_attempts, worker_id, "
            "next_run_at, created_at, started_at, completed_at, updated_at"
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            legacy_table = orphan_table
            if needs_migration:
                legacy_table = f"agent_jobs_legacy_{uuid.uuid4().hex[:8]}"
                self._conn.execute(f"ALTER TABLE agent_jobs RENAME TO {legacy_table}")
            # executescript() force-commits the open transaction — run the
            # schema statement by statement to stay inside BEGIN IMMEDIATE.
            for statement in JOBS_TABLE_SCHEMA.split(";"):
                if statement.strip():
                    self._conn.execute(statement)
            self._conn.execute(
                f"INSERT OR IGNORE INTO agent_jobs ({columns}) SELECT {columns} FROM {legacy_table}"
            )
            new_count = int(self._conn.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0])
            old_count = int(
                self._conn.execute(f"SELECT COUNT(*) FROM {legacy_table}").fetchone()[0]
            )
            if new_count < old_count:
                raise sqlite3.IntegrityError(
                    f"agent_jobs migration copied {new_count} of {old_count} rows"
                )
            self._conn.execute(f"DROP TABLE {legacy_table}")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

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
