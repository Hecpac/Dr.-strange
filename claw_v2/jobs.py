from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from claw_v2.artifacts import JobArtifact
from claw_v2.job_records import (
    JOB_SCHEMA,
    JOB_SCHEMA_VERSION,
    TERMINAL_JOB_STATES,
    JobRecord,
    JobState,
    JobStepRecord,
    StepState,
    job_from_row,
    now_utc,
    operation_hash,
    step_from_row,
)


class JobService:
    def __init__(self, db_path: Path | str, *, observe: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.observe = observe
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(JOB_SCHEMA)
        self._ensure_schema_version()
        self._lock = threading.Lock()

    def enqueue(self, *, kind: str, payload: dict[str, Any] | None = None, job_id: str | None = None) -> JobRecord:
        job_id = job_id or f"{kind}:{uuid.uuid4().hex[:16]}"
        existing = self.get(job_id)
        if existing is not None:
            return existing
        merged_payload = {"kind": kind, **(payload or {})}
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, state, payload) VALUES (?, 'queued', ?)",
                (job_id, json.dumps(merged_payload)),
            )
            self._conn.commit()
        job = self.get(job_id)
        self._emit("job_queued", job)
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._conn:
            row = self._conn.execute(
                """
                SELECT id, state, payload, version, lease_owner, lease_expires_at, created_at, updated_at
                FROM jobs WHERE id = ?
                """,
                (job_id,),
            ).fetchone()
        return job_from_row(row) if row else None

    def list_jobs(self, *, limit: int = 20, include_terminal: bool = True) -> list[JobRecord]:
        query = """
            SELECT id, state, payload, version, lease_owner, lease_expires_at, created_at, updated_at
            FROM jobs
        """
        params: tuple[Any, ...] = ()
        if not include_terminal:
            query += " WHERE state NOT IN (?, ?, ?)"
            params = tuple(TERMINAL_JOB_STATES)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [job_from_row(row) for row in rows]

    def start(self, job_id: str, *, lease_owner: str | None = None) -> JobRecord:
        return self.transition(job_id, "running", lease_owner=lease_owner)

    def waiting_approval(self, job_id: str, payload: dict[str, Any] | None = None) -> JobRecord:
        return self.transition(job_id, "waiting_approval", payload=payload)

    def complete(self, job_id: str, payload: dict[str, Any] | None = None) -> JobRecord:
        return self.transition(job_id, "completed", payload=payload)

    def fail(self, job_id: str, *, error: str, payload: dict[str, Any] | None = None) -> JobRecord:
        return self.transition(job_id, "failed", payload={"error": error, **(payload or {})})

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> JobRecord:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state in TERMINAL_JOB_STATES:
            return job
        return self.transition(job_id, "cancelled", payload={"cancel_reason": reason})

    def transition(
        self,
        job_id: str,
        state: JobState,
        *,
        payload: dict[str, Any] | None = None,
        lease_owner: str | None = None,
    ) -> JobRecord:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.state in TERMINAL_JOB_STATES:
            return job
        merged_payload = {**job.payload, **(payload or {})}
        now = now_utc()
        with self._lock:
            self._conn.execute(
                """
                UPDATE jobs
                SET state = ?, version = version + 1, lease_owner = COALESCE(?, lease_owner),
                    updated_at = ?, payload = ?
                WHERE id = ?
                """,
                (state, lease_owner, now, json.dumps(merged_payload), job_id),
            )
            self._conn.commit()
        updated = self.get(job_id)
        self._emit(f"job_{state}", updated)
        return updated

    def record_step(
        self,
        job_id: str,
        name: str,
        *,
        state: StepState = "completed",
        step_class: str = "pure",
        payload: dict[str, Any] | None = None,
        side_effect_ref: str | None = None,
        result_artifact_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> JobStepRecord:
        op_hash = operation_hash(name, payload or {})
        idempotency_key = idempotency_key or f"{job_id}:{name}:{op_hash}"
        existing = self._step_by_idempotency(idempotency_key)
        if existing is not None:
            return existing
        now = now_utc()
        completed_at = now if state in {"completed", "failed", "skipped"} else None
        step_id = f"step:{uuid.uuid4().hex[:16]}"
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO job_steps (
                    id, job_id, name, state, attempt_id, operation_hash, idempotency_key,
                    step_class, side_effect_ref, result_artifact_id, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step_id,
                    job_id,
                    name,
                    state,
                    uuid.uuid4().hex[:12],
                    op_hash,
                    idempotency_key,
                    step_class,
                    side_effect_ref,
                    result_artifact_id,
                    now,
                    completed_at,
                ),
            )
            self._conn.commit()
        return self._step_by_idempotency(idempotency_key)

    def checkpoint(self, job_id: str, name: str, payload: dict[str, Any] | None = None) -> str:
        artifact_id = None
        if self.observe is not None and hasattr(self.observe, "record_artifact"):
            artifact = JobArtifact(summary=f"{name}: {job_id}", job_id=job_id, payload=payload or {})
            artifact_id = self.observe.record_artifact(artifact)
        step = self.record_step(
            job_id,
            name,
            step_class="checkpoint",
            payload=payload,
            result_artifact_id=artifact_id,
        )
        return artifact_id or step.step_id

    def steps(self, job_id: str) -> list[JobStepRecord]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, job_id, name, state, attempt_id, operation_hash, idempotency_key,
                       step_class, side_effect_ref, result_artifact_id, started_at, completed_at
                FROM job_steps WHERE job_id = ? ORDER BY started_at ASC
                """,
                (job_id,),
            ).fetchall()
        return [step_from_row(row) for row in rows]

    def _step_by_idempotency(self, idempotency_key: str) -> JobStepRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, job_id, name, state, attempt_id, operation_hash, idempotency_key,
                       step_class, side_effect_ref, result_artifact_id, started_at, completed_at
                FROM job_steps WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        return step_from_row(row) if row else None

    def _ensure_schema_version(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        if current < JOB_SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version={JOB_SCHEMA_VERSION}")
            self._conn.commit()

    def _emit(self, event_type: str, job: JobRecord | None) -> None:
        if job is None or self.observe is None or not hasattr(self.observe, "emit_artifact"):
            return
        artifact = JobArtifact(summary=f"{job.state}: {job.job_id}", job_id=job.job_id, payload=job.payload)
        self.observe.emit_artifact(event_type, artifact, lane="worker", payload={"state": job.state})
