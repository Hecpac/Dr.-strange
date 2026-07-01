from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from claw_v2.maintenance import job_claim_block_reason
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
DEFAULT_JOB_LEASE_SECONDS = 15 * 60
JOB_TERMINAL_RETENTION_DAYS = 30
JOB_TERMINAL_PRUNE_MAX_ROWS = 20_000


class FormalLeaseRequiredError(RuntimeError):
    def __init__(self, operation: str) -> None:
        super().__init__(f"{operation} requires formal lease-specific API")
        self.operation = operation
        self.formal_leases_enabled = True


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
    lease_owner TEXT,
    lease_expires_at REAL,
    lease_heartbeat_at REAL,
    lease_generation INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX IF NOT EXISTS idx_agent_jobs_lease_expiry
    ON agent_jobs(status, lease_expires_at, lease_generation);

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
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    lease_heartbeat_at: float | None = None
    lease_generation: int = 0
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
        formal_leases_enabled: bool = False,
        default_lease_seconds: float = DEFAULT_JOB_LEASE_SECONDS,
        admin_cancel_authority_validator: (
            Callable[[str, str, str, str], bool] | None
        ) = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.observe = observe
        self.formal_leases_enabled = bool(formal_leases_enabled)
        self.default_lease_seconds = max(1.0, float(default_lease_seconds))
        self.admin_cancel_authority_validator = admin_cancel_authority_validator
        # In-process safe-mode latch, consulted at every claim site ALONGSIDE
        # the env-based maintenance gate. The daemon's branch-integrity check
        # sets this (P0-2) when the live checkout is stranded on a wrong branch
        # so workers stop claiming until a human runs `git checkout main`.
        self._safe_mode_reason: str | None = None
        if runtime_db is not None:
            # F1.1a1 production path: share the single RuntimeDb connection +
            # lock; RuntimeDb owns the connection lifecycle.
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
            self._ensure_lease_columns()
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
                if (
                    self._db is None
                    and heals < WAL_HEAL_RETRY_LIMIT
                    and heal_wal_after_disk_io(self.db_path, exc, context=operation)
                ):
                    heals += 1
                    continue
                raise
            except sqlite3.ProgrammingError as exc:
                if (
                    self._db is None
                    and heals < WAL_HEAL_RETRY_LIMIT
                    and heal_wal_after_closed_connection(self.db_path, exc, context=operation)
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
                        lease_owner, lease_expires_at, lease_heartbeat_at, lease_generation,
                        next_run_at, created_at, started_at, completed_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

    def reserve(
        self,
        *,
        resume_key: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[JobRecord, bool]:
        """Atomically elect a single creator for an *active* ``resume_key``.

        Returns ``(record, created)`` where ``created`` is True for exactly one
        concurrent caller (the winner) and False for every duplicate that races
        while the key's job is still active (queued/running/waiting_approval/
        retrying). Dedup is scoped to that active window: the unique index
        ``idx_agent_jobs_active_resume_key`` is PARTIAL (active statuses only), so
        once the job terminalizes (completed/failed/cancelled) the key is released
        — a later ``reserve`` of the same key creates a NEW job and returns
        ``created=True``. Callers needing terminal-aware dedup must add their own
        existence check (e.g. the F4 delegation ledger pre-check). The winner is
        identified by the generated ``job_id`` round-tripping back unchanged."""
        my_id = f"job:{uuid.uuid4().hex[:12]}"
        try:
            record = self.enqueue(
                kind=kind,
                payload=payload,
                resume_key=resume_key,
                metadata=metadata,
                job_id=my_id,
            )
        except sqlite3.IntegrityError:
            # resume_key collided with an ACTIVE reserved/running job (the only
            # rows the partial unique index covers) — dedup to that winner.
            existing = self.get_by_resume_key(resume_key)
            if existing is None:  # pragma: no cover - the unique index guarantees a row
                raise
            return existing, False
        return record, record.job_id == my_id

    def set_safe_mode_reason(self, reason: str | None) -> None:
        """Set (or clear, with ``None``) the in-process claim-block latch.

        A non-None value blocks every claim path the same way the env-based
        maintenance gate does, but driven by in-process state the daemon owns
        (P0-2 branch-integrity safe mode). A plain attribute assignment is
        sufficient: it is set from the tick thread and read from worker claim
        threads, and a single str/None write is atomic in CPython.
        """
        self._safe_mode_reason = reason

    def claim(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> JobRecord | None:
        now = time.time() if now is None else now
        blocked_reason = self._safe_mode_reason or job_claim_block_reason()
        if blocked_reason:
            self._emit_claim_blocked(
                operation="claim",
                reason=blocked_reason,
                worker_id=worker_id,
                job_id=job_id,
            )
            return None
        if self.formal_leases_enabled:
            return self._acquire_lease(
                job_id,
                worker_id=worker_id,
                now=now,
                lease_seconds=lease_seconds,
                emit_claimed=True,
            )

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

    def acquire_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> JobRecord | None:
        current = time.time() if now is None else float(now)
        blocked_reason = self._safe_mode_reason or job_claim_block_reason()
        if blocked_reason:
            self._emit_claim_blocked(
                operation="acquire_lease",
                reason=blocked_reason,
                worker_id=worker_id,
                job_id=job_id,
            )
            return None
        return self._acquire_lease(
            job_id,
            worker_id=worker_id,
            now=current,
            lease_seconds=lease_seconds,
            emit_claimed=False,
        )

    def acquire_next_lease(
        self,
        *,
        worker_id: str,
        kinds: Iterable[str] | None = None,
        now: float | None = None,
        lease_seconds: float | None = None,
        emit_claimed: bool = False,
    ) -> JobRecord | None:
        current = time.time() if now is None else float(now)
        if isinstance(kinds, str):
            kinds = (kinds,)
        kind_list = [kind for kind in (kinds or []) if kind]
        blocked_reason = self._safe_mode_reason or job_claim_block_reason()
        if blocked_reason:
            self._emit_claim_blocked(
                operation="acquire_next_lease",
                reason=blocked_reason,
                worker_id=worker_id,
                kinds=kind_list,
            )
            return None
        where = "status IN ('queued', 'retrying') AND COALESCE(next_run_at, 0) <= ?"
        params: list[Any] = [current]
        if kind_list:
            placeholders = ", ".join("?" for _ in kind_list)
            where += f" AND kind IN ({placeholders})"
            params.extend(kind_list)

        def acquire_next_once() -> sqlite3.Row | None:
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
                    updated = self._update_row_to_running_with_lease(
                        row,
                        worker_id=worker_id,
                        now=current,
                        lease_seconds=lease_seconds,
                    )
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.acquire_next_lease", acquire_next_once)
        record = self._row_to_record(row) if row is not None else None
        if record is not None:
            self._emit("job_lease_acquired", record)
            if emit_claimed:
                self._emit("job_claimed", record)
        return record

    def _acquire_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        now: float,
        lease_seconds: float | None,
        emit_claimed: bool,
    ) -> JobRecord | None:
        def acquire_once() -> sqlite3.Row | None:
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
                        return None
                    updated = self._update_row_to_running_with_lease(
                        row,
                        worker_id=worker_id,
                        now=now,
                        lease_seconds=lease_seconds,
                    )
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.acquire_lease", acquire_once)
        record = self._row_to_record(row) if row is not None else None
        if record is not None:
            self._emit("job_lease_acquired", record)
            if emit_claimed:
                self._emit("job_claimed", record)
        return record

    def _update_row_to_running_with_lease(
        self,
        row: sqlite3.Row,
        *,
        worker_id: str,
        now: float,
        lease_seconds: float | None,
    ) -> sqlite3.Row | None:
        attempts = int(row["attempts"] or 0) + 1
        lease_expires_at = now + self._lease_seconds(lease_seconds)
        cursor = self._conn.execute(
            """
            UPDATE agent_jobs
            SET status = 'running',
                worker_id = ?,
                lease_owner = ?,
                lease_expires_at = ?,
                lease_heartbeat_at = ?,
                lease_generation = COALESCE(lease_generation, 0) + 1,
                attempts = ?,
                started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE job_id = ?
              AND status IN ('queued', 'retrying')
            """,
            (
                worker_id,
                worker_id,
                lease_expires_at,
                now,
                attempts,
                now,
                now,
                row["job_id"],
            ),
        )
        if cursor.rowcount != 1:
            return None
        return self._conn.execute(
            "SELECT * FROM agent_jobs WHERE job_id = ?",
            (row["job_id"],),
        ).fetchone()

    def heartbeat_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int | None = None,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> JobRecord | None:
        if lease_generation is None:
            return None
        lease_generation_value = int(lease_generation)
        current = time.time() if now is None else float(now)
        expires_at = current + self._lease_seconds(lease_seconds)

        def heartbeat_once() -> sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        """
                        SELECT *
                        FROM agent_jobs
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                        """,
                        (job_id, worker_id, lease_generation_value),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None
                    current_expiry = _as_optional_float(row["lease_expires_at"])
                    if current_expiry is not None and current_expiry <= current:
                        self._conn.commit()
                        return None
                    cursor = self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET lease_expires_at = ?,
                            lease_heartbeat_at = ?,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at > ?
                        """,
                        (
                            expires_at,
                            current,
                            current,
                            job_id,
                            worker_id,
                            lease_generation_value,
                            current,
                        ),
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.heartbeat_lease", heartbeat_once)
        record = self._row_to_record(row) if row is not None else None
        if record is not None:
            self._emit("job_lease_heartbeat", record)
        return record

    def release_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int | None = None,
        now: float | None = None,
        retry_delay_seconds: float = 0.0,
    ) -> JobRecord | None:
        if lease_generation is None:
            return None
        lease_generation_value = int(lease_generation)
        current = time.time() if now is None else float(now)
        next_run_at = current + max(0.0, float(retry_delay_seconds))

        def release_once() -> sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        """
                        SELECT *
                        FROM agent_jobs
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                        """,
                        (job_id, worker_id, lease_generation_value),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None
                    cursor = self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET status = 'retrying',
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            lease_heartbeat_at = NULL,
                            next_run_at = ?,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                        """,
                        (next_run_at, current, job_id, worker_id, lease_generation_value),
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.release_lease", release_once)
        record = self._row_to_record(row) if row is not None else None
        if record is not None and record.status == "retrying":
            self._emit("job_lease_released", record)
        return record

    def claim_next(
        self,
        *,
        worker_id: str,
        kinds: Iterable[str] | None = None,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> JobRecord | None:
        now = time.time() if now is None else now
        if isinstance(kinds, str):
            kinds = (kinds,)
        kind_list = [kind for kind in (kinds or []) if kind]
        blocked_reason = self._safe_mode_reason or job_claim_block_reason()
        if blocked_reason:
            self._emit_claim_blocked(
                operation="claim_next",
                reason=blocked_reason,
                worker_id=worker_id,
                kinds=kind_list,
            )
            return None
        if self.formal_leases_enabled:
            return self.acquire_next_lease(
                worker_id=worker_id,
                kinds=kind_list,
                now=now,
                lease_seconds=lease_seconds,
                emit_claimed=True,
            )
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

    def checkpoint(
        self,
        job_id: str,
        checkpoint: dict[str, Any],
        *,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> JobRecord | None:
        now = time.time()
        lease_generation_value = self._normalize_lease_generation(lease_generation)
        lease_guard_enabled = self.formal_leases_enabled
        if lease_guard_enabled and not self._lease_credentials_provided(
            lease_owner,
            lease_generation_value,
        ):
            return None

        params: list[Any] = [json.dumps(dict(checkpoint), sort_keys=True), now, job_id]
        where_clause = "job_id = ?"
        if lease_guard_enabled:
            where_clause += """
              AND status = 'running'
              AND lease_owner = ?
              AND lease_generation = ?
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at > ?
            """
            params.extend([lease_owner, lease_generation_value, now])

        def checkpoint_once() -> sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    current = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    if current is None:
                        self._conn.commit()
                        return None
                    if lease_guard_enabled and not self._current_lease_matches(
                        current,
                        lease_owner=lease_owner,
                        lease_generation=lease_generation_value,
                        now=now,
                    ):
                        self._conn.commit()
                        return None
                    cursor = self._conn.execute(
                        f"""
                        UPDATE agent_jobs
                        SET checkpoint_json = ?,
                            updated_at = ?
                        WHERE {where_clause}
                        """,
                        params,
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.checkpoint", checkpoint_once)
        record = self._row_to_record(row) if row is not None else None
        if record is not None:
            self._emit("job_checkpointed", record)
        return record

    def wait_for_approval(
        self,
        job_id: str,
        *,
        checkpoint: dict[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> JobRecord | None:
        return self._update(
            job_id,
            status="waiting_approval",
            checkpoint=checkpoint,
            event_type="job_waiting_approval",
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )

    def complete(
        self,
        job_id: str,
        *,
        result: dict[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> JobRecord | None:
        return self._update(
            job_id,
            status="completed",
            result=result,
            completed_at=time.time(),
            event_type="job_completed",
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )

    def fail(
        self,
        job_id: str,
        *,
        error: str,
        retry: bool = True,
        retry_delay_seconds: float = 60.0,
        checkpoint: dict[str, Any] | None = None,
        lease_owner: str | None = None,
        lease_generation: int | None = None,
    ) -> JobRecord | None:
        now = time.time()
        lease_generation_value = self._normalize_lease_generation(lease_generation)
        lease_guard_enabled = self.formal_leases_enabled
        if lease_guard_enabled and not self._lease_credentials_provided(
            lease_owner,
            lease_generation_value,
        ):
            return None

        def fail_once() -> JobRecord | sqlite3.Row | None:
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
                        # failed/retrying, regardless of lease guard. A terminal job
                        # has no lease (cleared on terminalization), so the lease-match
                        # check below would otherwise return None for a late fail() retry
                        # instead of the terminal record (#153). Return the row we just
                        # read (NOT self.get(), which would re-acquire the non-reentrant
                        # lock).
                        self._conn.commit()
                        return self._row_to_record(row)
                    if lease_guard_enabled:
                        if not self._current_lease_matches(
                            row,
                            lease_owner=lease_owner,
                            lease_generation=lease_generation_value,
                            now=now,
                        ):
                            self._conn.commit()
                            return None
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
                    params: list[Any] = [
                        status,
                        error,
                        checkpoint_json,
                        next_run_at,
                        completed_at,
                        now,
                        job_id,
                    ]
                    where_clause = "job_id = ?"
                    if lease_guard_enabled:
                        where_clause += """
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at > ?
                        """
                        params.extend([lease_owner, lease_generation_value, now])
                    cursor = self._conn.execute(
                        f"""
                        UPDATE agent_jobs
                        SET status = ?,
                            error = ?,
                            checkpoint_json = ?,
                            next_run_at = ?,
                            completed_at = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            lease_heartbeat_at = NULL,
                            updated_at = ?
                        WHERE {where_clause}
                        """,
                        params,
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        updated = self._retry_after_disk_io("JobService.fail", fail_once)
        if isinstance(updated, JobRecord):
            return updated
        record = self._row_to_record(updated) if updated is not None else None
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
        lease_owner: str | None = None,
        lease_generation: int | None = None,
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
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )

    def request_cancel(
        self,
        job_id: str,
        *,
        actor: str,
        reason: str,
        now: float | None = None,
    ) -> JobRecord | None:
        actor = _require_non_empty("actor", actor)
        reason = _require_non_empty("reason", reason)
        current = time.time() if now is None else float(now)

        def request_once() -> JobRecord | sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None
                    if row["status"] in JOB_TERMINAL_STATUSES:
                        self._conn.commit()
                        return self._row_to_record(row)
                    metadata = _loads_json(row["metadata_json"])
                    metadata["cancel_request"] = {
                        "requested_at": current,
                        "requested_by": actor,
                        "reason": reason,
                    }
                    cursor = self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET metadata_json = ?,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status NOT IN ('completed', 'failed', 'cancelled')
                        """,
                        (json.dumps(metadata, sort_keys=True), current, job_id),
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        updated = self._retry_after_disk_io("JobService.request_cancel", request_once)
        if isinstance(updated, JobRecord):
            return updated
        record = self._row_to_record(updated) if updated is not None else None
        if record is not None:
            self._emit("job_cancel_requested", record)
        return record

    def worker_cancel(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_generation: int | None,
        reason: str = "cancelled",
        now: float | None = None,
    ) -> JobRecord | None:
        if not str(lease_owner or "").strip() or lease_generation is None:
            return None
        reason = _require_non_empty("reason", reason)
        lease_generation_value = int(lease_generation)
        current = time.time() if now is None else float(now)

        def cancel_once() -> sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        """
                        SELECT *
                        FROM agent_jobs
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                        """,
                        (job_id, lease_owner, lease_generation_value),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None
                    if not self._current_lease_matches(
                        row,
                        lease_owner=lease_owner,
                        lease_generation=lease_generation_value,
                        now=current,
                    ):
                        self._conn.commit()
                        return None
                    cursor = self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET status = 'cancelled',
                            error = ?,
                            completed_at = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            lease_heartbeat_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_owner = ?
                          AND lease_generation = ?
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at > ?
                        """,
                        (
                            reason,
                            current,
                            current,
                            job_id,
                            lease_owner,
                            lease_generation_value,
                            current,
                        ),
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.worker_cancel", cancel_once)
        record = self._row_to_record(row) if row is not None else None
        if record is not None:
            self._emit("job_worker_cancelled", record)
        return record

    def admin_force_cancel(
        self,
        job_id: str,
        *,
        admin_actor: str,
        reason: str,
        authority_token: str,
        now: float | None = None,
    ) -> JobRecord | None:
        admin_actor = _require_non_empty("admin_actor", admin_actor)
        reason = _require_non_empty("reason", reason)
        authority_token = _require_non_empty("authority_token", authority_token)
        validator = self.admin_cancel_authority_validator
        if validator is None:
            return None
        try:
            if not validator(admin_actor, job_id, reason, authority_token):
                return None
        except Exception:
            return None
        current = time.time() if now is None else float(now)
        correlation_id = f"admin_force_cancel:{uuid.uuid4().hex[:12]}"

        def admin_cancel_once() -> JobRecord | sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if row is None:
                        self._conn.commit()
                        return None
                    if row["status"] in JOB_TERMINAL_STATUSES:
                        self._conn.commit()
                        return self._row_to_record(row)
                    metadata = _loads_json(row["metadata_json"])
                    metadata["admin_force_cancel"] = {
                        "cancelled_at": current,
                        "admin_actor": admin_actor,
                        "reason": reason,
                        "authority_reference": correlation_id,
                        "previous_status": str(row["status"]),
                        "new_status": "cancelled",
                        "previous_worker_id": _as_optional_str(row["worker_id"]),
                        "previous_lease_owner": _as_optional_str(row["lease_owner"]),
                        "previous_lease_generation": int(row["lease_generation"] or 0),
                        "previous_lease_expires_at": _as_optional_float(
                            row["lease_expires_at"]
                        ),
                        "correlation_id": correlation_id,
                    }
                    cursor = self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET status = 'cancelled',
                            metadata_json = ?,
                            error = ?,
                            completed_at = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            lease_heartbeat_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status NOT IN ('completed', 'failed', 'cancelled')
                        """,
                        (
                            json.dumps(metadata, sort_keys=True),
                            reason,
                            current,
                            current,
                            job_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        updated = self._retry_after_disk_io(
            "JobService.admin_force_cancel",
            admin_cancel_once,
        )
        if isinstance(updated, JobRecord):
            return updated
        record = self._row_to_record(updated) if updated is not None else None
        if record is not None:
            self._emit("job_admin_force_cancelled", record)
        return record

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> JobRecord | None:
        if self.formal_leases_enabled:
            raise FormalLeaseRequiredError("JobService.cancel")
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
            require_lease=False,
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

    def prune_terminal(
        self,
        *,
        retention_days: int = JOB_TERMINAL_RETENTION_DAYS,
        max_rows: int = JOB_TERMINAL_PRUNE_MAX_ROWS,
        now: float | None = None,
    ) -> int:
        """Delete old terminal job rows, bounded per call."""
        limit = max(0, int(max_rows))
        if limit <= 0:
            return 0
        retention = max(0, int(retention_days))
        current = time.time() if now is None else float(now)
        cutoff = current - (retention * 86400.0)
        terminal_statuses = tuple(sorted(JOB_TERMINAL_STATUSES))
        placeholders = ", ".join("?" for _ in terminal_statuses)

        def prune_once() -> int:
            with self._lock:
                cursor = self._conn.execute(
                    f"""
                    DELETE FROM agent_jobs
                    WHERE job_id IN (
                        SELECT job_id
                        FROM agent_jobs
                        WHERE status IN ({placeholders})
                          AND COALESCE(completed_at, updated_at, created_at) < ?
                        ORDER BY COALESCE(completed_at, updated_at, created_at) ASC,
                                 updated_at ASC,
                                 job_id ASC
                        LIMIT ?
                    )
                    """,
                    (*terminal_statuses, cutoff, limit),
                )
                deleted = int(cursor.rowcount or 0)
                self._conn.commit()
                return deleted

        deleted = self._retry_after_disk_io("JobService.prune_terminal", prune_once)
        if deleted and self.observe is not None:
            self.observe.emit(
                "agent_jobs_pruned",
                lane="job_service",
                payload={
                    "deleted_rows": deleted,
                    "retention_days": retention,
                    "max_rows": limit,
                },
            )
        return deleted

    def resume_candidates(self, *, limit: int = 20) -> list[JobRecord]:
        return self.list(
            statuses=("queued", "running", "waiting_approval", "retrying"), limit=limit
        )

    def reclaim_expired_leases(
        self,
        *,
        kinds: Iterable[str] | None = None,
        kind_prefix: str | None = None,
        no_retry: bool = False,
        now: float | None = None,
        limit: int = 100,
        retry_delay_seconds: float = 0.0,
        error: str = "lease_expired",
        event_type: str = "job_lease_reclaimed",
    ) -> list[JobRecord]:
        current = time.time() if now is None else float(now)
        if isinstance(kinds, str):
            kinds = (kinds,)
        kind_list = [kind for kind in (kinds or []) if kind]
        where = "status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?"
        params: list[Any] = [current]
        if kind_list:
            placeholders = ", ".join("?" for _ in kind_list)
            where += f" AND kind IN ({placeholders})"
            params.extend(kind_list)
        if kind_prefix:
            where += " AND kind LIKE ?"
            params.append(f"{kind_prefix}%")
        params.append(max(1, min(int(limit), 100)))

        def candidate_ids_once() -> list[str]:
            with self._lock:
                rows = self._conn.execute(
                    f"""
                    SELECT job_id
                    FROM agent_jobs
                    WHERE {where}
                    ORDER BY lease_expires_at ASC, updated_at ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            return [str(row["job_id"]) for row in rows]

        job_ids = self._retry_after_disk_io(
            "JobService.reclaim_expired_leases.candidates",
            candidate_ids_once,
        )
        recovered: list[JobRecord] = []
        for job_id in job_ids:
            record = self._reclaim_expired_lease_job(
                job_id,
                now=current,
                retry_delay_seconds=retry_delay_seconds,
                error=error,
                no_retry=no_retry,
            )
            if record is None:
                continue
            recovered.append(record)
            self._emit("job_retrying" if record.status == "retrying" else "job_failed", record)
            self._emit(event_type, record)
        return recovered

    def _reclaim_expired_lease_job(
        self,
        job_id: str,
        *,
        now: float,
        retry_delay_seconds: float,
        error: str,
        no_retry: bool,
    ) -> JobRecord | None:
        def reclaim_once() -> sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if row is None or row["status"] != "running":
                        self._conn.commit()
                        return None
                    lease_expires_at = _as_optional_float(row["lease_expires_at"])
                    if lease_expires_at is None or lease_expires_at > now:
                        self._conn.commit()
                        return None
                    attempts = int(row["attempts"] or 0)
                    max_attempts = int(row["max_attempts"] or 1)
                    should_retry = attempts < max_attempts and not no_retry
                    status = "retrying" if should_retry else "failed"
                    completed_at = None if should_retry else now
                    next_run_at = (
                        now + max(0.0, retry_delay_seconds) if should_retry else row["next_run_at"]
                    )
                    checkpoint = _loads_json(row["checkpoint_json"])
                    checkpoint["lease_reclaim"] = {
                        "reclaimed_at": now,
                        "lease_owner": row["lease_owner"] or "",
                        "lease_generation": int(row["lease_generation"] or 0),
                        "lease_expired_by_seconds": max(0.0, now - lease_expires_at),
                        "reason": error,
                    }
                    self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET status = ?,
                            error = ?,
                            checkpoint_json = ?,
                            next_run_at = ?,
                            completed_at = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            lease_heartbeat_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status = 'running'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= ?
                        """,
                        (
                            status,
                            error,
                            json.dumps(checkpoint, sort_keys=True),
                            next_run_at,
                            completed_at,
                            now,
                            job_id,
                            now,
                        ),
                    )
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.reclaim_expired_lease", reclaim_once)
        return self._row_to_record(row) if row is not None else None

    def recover_stale_running(
        self,
        *,
        stale_after_seconds: float,
        kinds: Iterable[str] | None = None,
        kind_prefix: str | None = None,
        no_retry: bool = False,
        now: float | None = None,
        limit: int = 100,
        retry_delay_seconds: float = 0.0,
        error: str = "stale_running_timeout",
        event_type: str = "stale_running_job_recovered",
    ) -> list[JobRecord]:
        """Move stale running jobs back to retrying, or failed at max attempts.

        This is intentionally generic and bounded so daemon recovery lanes can
        reclaim jobs that were claimed by a worker/thread that disappeared.
        Recent running jobs are ignored; terminal jobs are never resurrected.
        """
        current = time.time() if now is None else float(now)
        if self.formal_leases_enabled:
            return self.reclaim_expired_leases(
                kinds=kinds,
                kind_prefix=kind_prefix,
                no_retry=no_retry,
                now=current,
                limit=limit,
                retry_delay_seconds=retry_delay_seconds,
                error=error,
                event_type=event_type,
            )
        cutoff = current - max(0.001, float(stale_after_seconds))
        if isinstance(kinds, str):
            kinds = (kinds,)
        kind_list = [kind for kind in (kinds or []) if kind]
        where = "status = 'running' AND COALESCE(updated_at, started_at, created_at) <= ?"
        params: list[Any] = [cutoff]
        if kind_list:
            placeholders = ", ".join("?" for _ in kind_list)
            where += f" AND kind IN ({placeholders})"
            params.extend(kind_list)
        if kind_prefix:
            where += " AND kind LIKE ?"
            params.append(f"{kind_prefix}%")
        params.append(max(1, min(int(limit), 100)))

        def candidate_ids_once() -> list[str]:
            with self._lock:
                rows = self._conn.execute(
                    f"""
                    SELECT job_id
                    FROM agent_jobs
                    WHERE {where}
                    ORDER BY COALESCE(updated_at, started_at, created_at) ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            return [str(row["job_id"]) for row in rows]

        job_ids = self._retry_after_disk_io(
            "JobService.recover_stale_running.candidates",
            candidate_ids_once,
        )
        recovered: list[JobRecord] = []
        for job_id in job_ids:
            record = self._recover_stale_running_job(
                job_id,
                cutoff=cutoff,
                now=current,
                retry_delay_seconds=retry_delay_seconds,
                error=error,
                no_retry=no_retry,
            )
            if record is None:
                continue
            recovered.append(record)
            self._emit("job_retrying" if record.status == "retrying" else "job_failed", record)
            self._emit(event_type, record)
        return recovered

    def _recover_stale_running_job(
        self,
        job_id: str,
        *,
        cutoff: float,
        now: float,
        retry_delay_seconds: float,
        error: str,
        no_retry: bool = False,
    ) -> JobRecord | None:
        def recover_once() -> sqlite3.Row | None:
            with self._lock:
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    row = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    if row is None or row["status"] != "running":
                        self._conn.commit()
                        return None
                    reference = float(
                        _first_not_none(
                            row["updated_at"],
                            row["started_at"],
                            row["created_at"],
                        )
                    )
                    if reference > cutoff:
                        self._conn.commit()
                        return None
                    attempts = int(row["attempts"] or 0)
                    max_attempts = int(row["max_attempts"] or 1)
                    should_retry = attempts < max_attempts and not no_retry
                    status = "retrying" if should_retry else "failed"
                    completed_at = None if should_retry else now
                    next_run_at = (
                        now + max(0.0, retry_delay_seconds) if should_retry else row["next_run_at"]
                    )
                    checkpoint = _loads_json(row["checkpoint_json"])
                    checkpoint["stale_running_recovery"] = {
                        "recovered_at": now,
                        "age_seconds": max(0.0, now - reference),
                        "previous_worker_id": row["worker_id"] or "",
                        "reason": error,
                    }
                    self._conn.execute(
                        """
                        UPDATE agent_jobs
                        SET status = ?,
                            error = ?,
                            checkpoint_json = ?,
                            next_run_at = ?,
                            completed_at = ?,
                            lease_owner = NULL,
                            lease_expires_at = NULL,
                            lease_heartbeat_at = NULL,
                            updated_at = ?
                        WHERE job_id = ?
                          AND status = 'running'
                        """,
                        (
                            status,
                            error,
                            json.dumps(checkpoint, sort_keys=True),
                            next_run_at,
                            completed_at,
                            now,
                            job_id,
                        ),
                    )
                    updated = self._conn.execute(
                        "SELECT * FROM agent_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    self._conn.commit()
                    return updated
                except Exception:
                    self._conn.rollback()
                    raise

        row = self._retry_after_disk_io("JobService.recover_stale_running", recover_once)
        return self._row_to_record(row) if row is not None else None

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
        lease_owner: str | None = None,
        lease_generation: int | None = None,
        require_lease: bool = True,
    ) -> JobRecord | None:
        self._validate_status(status)
        now = time.time()
        lease_generation_value = self._normalize_lease_generation(lease_generation)
        lease_guard_enabled = self.formal_leases_enabled and require_lease
        if lease_guard_enabled and not self._lease_credentials_provided(
            lease_owner,
            lease_generation_value,
        ):
            return None
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
        if status != "running":
            assignments.extend(
                [
                    "lease_owner = NULL",
                    "lease_expires_at = NULL",
                    "lease_heartbeat_at = NULL",
                ]
            )
        where_clause = "job_id = ?"
        where_params: list[Any] = [job_id]
        if lease_guard_enabled:
            where_clause += """
              AND status = 'running'
              AND lease_owner = ?
              AND lease_generation = ?
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at > ?
            """
            where_params.extend([lease_owner, lease_generation_value, now])

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
                        # Idempotent: never resurrect a terminal job, regardless of
                        # lease guard. A terminal job has no lease (cleared on
                        # terminalization), so the lease-match check below would
                        # otherwise return None for a late complete()/fail() retry
                        # instead of the terminal record (#153). BEGIN IMMEDIATE
                        # holds the write lock across this read so a sibling cannot
                        # flip the row between them. Return the row we just read
                        # (NOT self.get(), which would re-acquire the non-reentrant lock).
                        self._conn.commit()
                        return self._row_to_record(current)
                    if lease_guard_enabled:
                        if not self._current_lease_matches(
                            current,
                            lease_owner=lease_owner,
                            lease_generation=lease_generation_value,
                            now=now,
                        ):
                            self._conn.commit()
                            return None
                    cursor = self._conn.execute(
                        f"UPDATE agent_jobs SET {', '.join(assignments)} WHERE {where_clause}",
                        [*params, *where_params],
                    )
                    if cursor.rowcount != 1:
                        self._conn.commit()
                        return None
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

    def _ensure_lease_columns(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(agent_jobs)").fetchall()
        }
        migrations = {
            "lease_owner": "ALTER TABLE agent_jobs ADD COLUMN lease_owner TEXT",
            "lease_expires_at": "ALTER TABLE agent_jobs ADD COLUMN lease_expires_at REAL",
            "lease_heartbeat_at": "ALTER TABLE agent_jobs ADD COLUMN lease_heartbeat_at REAL",
            "lease_generation": (
                "ALTER TABLE agent_jobs ADD COLUMN lease_generation INTEGER NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                self._conn.execute(statement)

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
            record.lease_owner,
            record.lease_expires_at,
            record.lease_heartbeat_at,
            record.lease_generation,
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
            lease_owner=_as_optional_str(row["lease_owner"]),
            lease_expires_at=_as_optional_float(row["lease_expires_at"]),
            lease_heartbeat_at=_as_optional_float(row["lease_heartbeat_at"]),
            lease_generation=int(row["lease_generation"] or 0),
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

    def _lease_seconds(self, lease_seconds: float | None) -> float:
        if lease_seconds is None:
            return self.default_lease_seconds
        return max(1.0, float(lease_seconds))

    @staticmethod
    def _normalize_lease_generation(lease_generation: int | None) -> int | None:
        if lease_generation is None:
            return None
        return int(lease_generation)

    @staticmethod
    def _lease_credentials_provided(
        lease_owner: str | None,
        lease_generation: int | None,
    ) -> bool:
        return bool(lease_owner) and lease_generation is not None

    def _current_lease_matches(
        self,
        row: sqlite3.Row,
        *,
        lease_owner: str | None,
        lease_generation: int | None,
        now: float,
    ) -> bool:
        if not self._lease_credentials_provided(lease_owner, lease_generation):
            return False
        expires_at = _as_optional_float(row["lease_expires_at"])
        return (
            row["status"] == "running"
            and row["lease_owner"] == lease_owner
            and int(row["lease_generation"] or 0) == lease_generation
            and expires_at is not None
            and expires_at > now
        )

    def _emit(self, event_type: str, record: JobRecord) -> None:
        if self.observe is None:
            return
        self.observe.emit(
            event_type,
            lane="job_service",
            job_id=record.job_id,
            payload=record.to_dict(),
        )

    def _emit_claim_blocked(
        self,
        *,
        operation: str,
        reason: str,
        worker_id: str,
        job_id: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> None:
        if self.observe is None:
            return
        payload: dict[str, Any] = {
            "operation": operation,
            "reason": reason,
            "worker_id": worker_id,
        }
        if job_id is not None:
            payload["job_id"] = job_id
        if kinds is not None:
            payload["kinds"] = [kind for kind in kinds if kind]
        self.observe.emit(
            "job_claim_blocked",
            lane="job_service",
            job_id=job_id,
            payload=payload,
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


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _require_non_empty(name: str, value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text
