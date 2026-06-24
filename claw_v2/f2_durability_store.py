from __future__ import annotations

import hashlib
import json
import threading
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from claw_v2.f2_durability_schema import (
    F2_DURABILITY_SCHEMA_VERSION,
    ensure_f2_durability_schema,
    validate_f2_schema_version,
)
from claw_v2.sqlite_runtime import RuntimeDb


PHASE_CHECKPOINT_STATUSES = frozenset(
    {"started", "succeeded", "failed", "blocked", "recovery_required"}
)
EXTERNAL_EFFECT_STATUSES = frozenset(
    {
        "intent_recorded",
        "apply_in_progress",
        "applied",
        "failed",
        "verification_required",
        "verified_applied",
        "verified_absent",
        "blocked_manual_review",
    }
)
RECOVERY_CURSOR_STATUSES = frozenset(
    {
        "ready_to_start_phase",
        "ready_to_replay_completed_phase",
        "ready_to_resume_phase",
        "effect_verification_required",
        "blocked_manual_review",
        "terminal_recovery_complete",
    }
)

_DEFAULT_MAX_JSON_BYTES = 1_000_000
_SCHEMA_READY_LOCK = threading.Lock()
_SCHEMA_READY_RUNTIME_DBS: weakref.WeakSet[RuntimeDb] = weakref.WeakSet()


@dataclass(frozen=True, slots=True)
class PhaseCheckpointRecord:
    checkpoint_id: str
    task_id: str
    run_id: str
    job_id: str | None
    session_id: str | None
    phase: str
    phase_version: int
    status: str
    schema_version: int
    last_write_order: int
    payload: Any | None
    payload_json: str
    payload_sha256: str
    payload_error: str | None
    orchestration_run_id: str | None
    orchestration_checkpoint_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class CheckpointWriteRecord:
    write_id: str
    task_id: str
    run_id: str
    job_id: str | None
    phase: str
    write_order: int
    write_kind: str
    write_key: str | None
    schema_version: int
    payload: Any | None
    payload_json: str
    payload_sha256: str
    payload_error: str | None
    external_effect_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ExternalEffectRecord:
    external_effect_id: str
    idempotency_key: str
    task_id: str
    run_id: str
    job_id: str | None
    phase: str
    effect_kind: str
    target: str
    content_hash: str
    request: Any | None
    request_json: str
    request_sha256: str
    request_error: str | None
    status: str
    attempt_count: int
    verifier_kind: str | None
    verification: Any | None
    verification_json: str | None
    verification_error: str | None
    result: Any | None
    result_json: str | None
    result_sha256: str | None
    result_error: str | None
    error: str | None
    schema_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class RecoveryCursorRecord:
    recovery_cursor_id: str
    task_id: str
    run_id: str
    job_id: str | None
    session_id: str | None
    phase: str
    cursor_status: str
    last_checkpoint_id: str | None
    last_write_order: int
    external_effect_id: str | None
    resume_payload: Any | None
    resume_payload_json: str
    resume_payload_error: str | None
    schema_version: int
    created_at: str
    updated_at: str


class F2DurabilityStore:
    """RuntimeDb-backed storage API for F2 durability tables.

    F2.1 deliberately exposes storage helpers only. It does not wire checkpoint
    writes into TaskHandler, Coordinator, production startup, or external
    adapters.
    """

    def __init__(
        self,
        runtime_db: RuntimeDb,
        *,
        max_json_bytes: int = _DEFAULT_MAX_JSON_BYTES,
    ) -> None:
        if not isinstance(runtime_db, RuntimeDb):
            raise TypeError("F2DurabilityStore requires a RuntimeDb instance")
        if max_json_bytes < 1:
            raise ValueError("max_json_bytes must be positive")
        self._db = runtime_db
        self._max_json_bytes = max_json_bytes
        _ensure_schema_ready(runtime_db)

    def create_phase_checkpoint(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        phase_version: int,
        status: str,
        payload: Any,
        checkpoint_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
        last_write_order: int = 0,
        orchestration_run_id: str | None = None,
        orchestration_checkpoint_id: str | None = None,
        schema_version: int = F2_DURABILITY_SCHEMA_VERSION,
        created_at: str | None = None,
    ) -> PhaseCheckpointRecord:
        validate_f2_schema_version(schema_version)
        _require_nonblank("task_id", task_id)
        _require_nonblank("run_id", run_id)
        _require_nonblank("phase", phase)
        _validate_status("phase checkpoint", status, PHASE_CHECKPOINT_STATUSES)
        if phase_version < 1:
            raise ValueError("phase_version must be >= 1")
        if last_write_order < 0:
            raise ValueError("last_write_order must be >= 0")
        payload_json = self._json_dumps(payload)
        payload_sha256 = _sha256_text(payload_json)
        row_id = checkpoint_id or _new_id("phase-checkpoint")
        now = created_at or _utc_now()
        with self._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO phase_checkpoints (
                    checkpoint_id, task_id, run_id, job_id, session_id, phase,
                    phase_version, status, schema_version, last_write_order,
                    payload_json, payload_sha256, orchestration_run_id,
                    orchestration_checkpoint_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    task_id,
                    run_id,
                    job_id,
                    session_id,
                    phase,
                    phase_version,
                    status,
                    schema_version,
                    last_write_order,
                    payload_json,
                    payload_sha256,
                    orchestration_run_id,
                    orchestration_checkpoint_id,
                    now,
                ),
            )
        record = self.get_phase_checkpoint(row_id)
        if record is None:
            raise RuntimeError(f"inserted phase checkpoint was not readable: {row_id}")
        return record

    def get_phase_checkpoint(self, checkpoint_id: str) -> PhaseCheckpointRecord | None:
        _require_nonblank("checkpoint_id", checkpoint_id)
        with self._db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM phase_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self._phase_checkpoint_from_row(row) if row is not None else None

    def list_phase_checkpoints(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        phase: str | None = None,
        status: str | None = None,
        created_at_after: str | None = None,
        created_at_before: str | None = None,
        order: str = "created_at_desc",
        limit: int | None = None,
    ) -> list[PhaseCheckpointRecord]:
        where, params = _where_filters(
            {
                "task_id": task_id,
                "run_id": run_id,
                "phase": phase,
                "status": status,
            },
            created_at_after=created_at_after,
            created_at_before=created_at_before,
        )
        sql = "SELECT * FROM phase_checkpoints" + where
        sql += " ORDER BY " + _checkpoint_order_by(order)
        sql, params = _append_limit(sql, params, limit)
        with self._db.cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._phase_checkpoint_from_row(row) for row in rows]

    def append_checkpoint_write(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        write_kind: str,
        payload: Any,
        write_order: int | None = None,
        write_id: str | None = None,
        job_id: str | None = None,
        write_key: str | None = None,
        external_effect_id: str | None = None,
        schema_version: int = F2_DURABILITY_SCHEMA_VERSION,
        created_at: str | None = None,
    ) -> CheckpointWriteRecord:
        validate_f2_schema_version(schema_version)
        _require_nonblank("task_id", task_id)
        _require_nonblank("run_id", run_id)
        _require_nonblank("phase", phase)
        _require_nonblank("write_kind", write_kind)
        if write_order is not None and write_order < 1:
            raise ValueError("write_order must be >= 1")
        payload_json = self._json_dumps(payload)
        payload_sha256 = _sha256_text(payload_json)
        row_id = write_id or _new_id("phase-write")
        now = created_at or _utc_now()
        with self._db.transaction() as cur:
            resolved_order = write_order
            if resolved_order is None:
                row = cur.execute(
                    """
                    SELECT COALESCE(MAX(write_order), 0) + 1 AS next_order
                    FROM phase_checkpoint_writes
                    WHERE task_id = ? AND run_id = ? AND phase = ?
                    """,
                    (task_id, run_id, phase),
                ).fetchone()
                resolved_order = int(row["next_order"])
            cur.execute(
                """
                INSERT INTO phase_checkpoint_writes (
                    write_id, task_id, run_id, job_id, phase, write_order,
                    write_kind, write_key, schema_version, payload_json,
                    payload_sha256, external_effect_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    task_id,
                    run_id,
                    job_id,
                    phase,
                    resolved_order,
                    write_kind,
                    write_key,
                    schema_version,
                    payload_json,
                    payload_sha256,
                    external_effect_id,
                    now,
                ),
            )
        record = self.get_checkpoint_write(row_id)
        if record is None:
            raise RuntimeError(f"inserted checkpoint write was not readable: {row_id}")
        return record

    def get_checkpoint_write(self, write_id: str) -> CheckpointWriteRecord | None:
        _require_nonblank("write_id", write_id)
        with self._db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM phase_checkpoint_writes WHERE write_id = ?",
                (write_id,),
            ).fetchone()
        return self._checkpoint_write_from_row(row) if row is not None else None

    def list_checkpoint_writes(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        phase: str | None = None,
        write_kind: str | None = None,
        external_effect_id: str | None = None,
        created_at_after: str | None = None,
        created_at_before: str | None = None,
        order: str = "write_order_asc",
        limit: int | None = None,
    ) -> list[CheckpointWriteRecord]:
        where, params = _where_filters(
            {
                "task_id": task_id,
                "run_id": run_id,
                "phase": phase,
                "write_kind": write_kind,
                "external_effect_id": external_effect_id,
            },
            created_at_after=created_at_after,
            created_at_before=created_at_before,
        )
        sql = "SELECT * FROM phase_checkpoint_writes" + where
        sql += " ORDER BY " + _write_order_by(order)
        sql, params = _append_limit(sql, params, limit)
        with self._db.cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._checkpoint_write_from_row(row) for row in rows]

    def record_external_effect(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        effect_kind: str,
        target: str,
        request: Any,
        content_hash: str | None = None,
        idempotency_key: str | None = None,
        external_effect_id: str | None = None,
        job_id: str | None = None,
        status: str = "intent_recorded",
        attempt_count: int = 0,
        verifier_kind: str | None = None,
        verification: Any | None = None,
        result: Any | None = None,
        error: str | None = None,
        schema_version: int = F2_DURABILITY_SCHEMA_VERSION,
        created_at: str | None = None,
    ) -> ExternalEffectRecord:
        validate_f2_schema_version(schema_version)
        _require_nonblank("task_id", task_id)
        _require_nonblank("run_id", run_id)
        _require_nonblank("phase", phase)
        _require_nonblank("effect_kind", effect_kind)
        _require_nonblank("target", target)
        _validate_status("external effect", status, EXTERNAL_EFFECT_STATUSES)
        if attempt_count < 0:
            raise ValueError("attempt_count must be >= 0")
        request_json = self._json_dumps(request)
        request_sha256 = _sha256_text(request_json)
        resolved_content_hash = content_hash or request_sha256
        resolved_idempotency_key = idempotency_key or compute_external_effect_idempotency_key(
            task_id=task_id,
            run_id=run_id,
            phase=phase,
            effect_kind=effect_kind,
            target=target,
            content_hash=resolved_content_hash,
            schema_version=schema_version,
        )
        verification_json = self._json_dumps(verification) if verification is not None else None
        result_json = self._json_dumps(result) if result is not None else None
        result_sha256 = _sha256_text(result_json) if result_json is not None else None
        row_id = external_effect_id or _new_id("external-effect")
        now = created_at or _utc_now()
        with self._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO external_effect_records (
                    external_effect_id, idempotency_key, task_id, run_id, job_id,
                    phase, effect_kind, target, content_hash, request_json,
                    request_sha256, status, attempt_count, verifier_kind,
                    verification_json, result_json, result_sha256, error,
                    schema_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    row_id,
                    resolved_idempotency_key,
                    task_id,
                    run_id,
                    job_id,
                    phase,
                    effect_kind,
                    target,
                    resolved_content_hash,
                    request_json,
                    request_sha256,
                    status,
                    attempt_count,
                    verifier_kind,
                    verification_json,
                    result_json,
                    result_sha256,
                    error,
                    schema_version,
                    now,
                    now,
                ),
            )
            row = cur.execute(
                "SELECT * FROM external_effect_records WHERE idempotency_key = ?",
                (resolved_idempotency_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("external effect insert did not return a readable row")
        return self._external_effect_from_row(row)

    def get_external_effect(self, external_effect_id: str) -> ExternalEffectRecord | None:
        _require_nonblank("external_effect_id", external_effect_id)
        with self._db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM external_effect_records WHERE external_effect_id = ?",
                (external_effect_id,),
            ).fetchone()
        return self._external_effect_from_row(row) if row is not None else None

    def get_external_effect_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> ExternalEffectRecord | None:
        _require_nonblank("idempotency_key", idempotency_key)
        with self._db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM external_effect_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._external_effect_from_row(row) if row is not None else None

    def list_external_effects(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        phase: str | None = None,
        status: str | None = None,
        effect_kind: str | None = None,
        created_at_after: str | None = None,
        created_at_before: str | None = None,
        updated_at_after: str | None = None,
        updated_at_before: str | None = None,
        order: str = "updated_at_desc",
        limit: int | None = None,
    ) -> list[ExternalEffectRecord]:
        where, params = _where_filters(
            {
                "task_id": task_id,
                "run_id": run_id,
                "phase": phase,
                "status": status,
                "effect_kind": effect_kind,
            },
            created_at_after=created_at_after,
            created_at_before=created_at_before,
            updated_at_after=updated_at_after,
            updated_at_before=updated_at_before,
        )
        sql = "SELECT * FROM external_effect_records" + where
        sql += " ORDER BY " + _external_effect_order_by(order)
        sql, params = _append_limit(sql, params, limit)
        with self._db.cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._external_effect_from_row(row) for row in rows]

    def update_external_effect_status(
        self,
        external_effect_id: str,
        *,
        status: str,
        result: Any | None = None,
        verification: Any | None = None,
        verifier_kind: str | None = None,
        error: str | None = None,
        increment_attempt_count: bool = False,
        updated_at: str | None = None,
    ) -> ExternalEffectRecord | None:
        _require_nonblank("external_effect_id", external_effect_id)
        _validate_status("external effect", status, EXTERNAL_EFFECT_STATUSES)
        result_json = self._json_dumps(result) if result is not None else None
        result_sha256 = _sha256_text(result_json) if result_json is not None else None
        verification_json = self._json_dumps(verification) if verification is not None else None
        attempt_sql = "attempt_count + 1" if increment_attempt_count else "attempt_count"
        now = updated_at or _utc_now()
        with self._db.transaction() as cur:
            cur.execute(
                f"""
                UPDATE external_effect_records
                SET status = ?,
                    attempt_count = {attempt_sql},
                    verifier_kind = COALESCE(?, verifier_kind),
                    verification_json = COALESCE(?, verification_json),
                    result_json = COALESCE(?, result_json),
                    result_sha256 = COALESCE(?, result_sha256),
                    error = ?,
                    updated_at = ?
                WHERE external_effect_id = ?
                """,
                (
                    status,
                    verifier_kind,
                    verification_json,
                    result_json,
                    result_sha256,
                    error,
                    now,
                    external_effect_id,
                ),
            )
            row = cur.execute(
                "SELECT * FROM external_effect_records WHERE external_effect_id = ?",
                (external_effect_id,),
            ).fetchone()
        return self._external_effect_from_row(row) if row is not None else None

    def upsert_recovery_cursor(
        self,
        *,
        task_id: str,
        run_id: str,
        phase: str,
        cursor_status: str,
        resume_payload: Any,
        recovery_cursor_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
        last_checkpoint_id: str | None = None,
        last_write_order: int = 0,
        external_effect_id: str | None = None,
        schema_version: int = F2_DURABILITY_SCHEMA_VERSION,
        updated_at: str | None = None,
    ) -> RecoveryCursorRecord:
        validate_f2_schema_version(schema_version)
        _require_nonblank("task_id", task_id)
        _require_nonblank("run_id", run_id)
        _require_nonblank("phase", phase)
        _validate_status("recovery cursor", cursor_status, RECOVERY_CURSOR_STATUSES)
        if last_write_order < 0:
            raise ValueError("last_write_order must be >= 0")
        resume_payload_json = self._json_dumps(resume_payload)
        row_id = recovery_cursor_id or _new_id("recovery-cursor")
        now = updated_at or _utc_now()
        with self._db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO phase_recovery_cursors (
                    recovery_cursor_id, task_id, run_id, job_id, session_id,
                    phase, cursor_status, last_checkpoint_id, last_write_order,
                    external_effect_id, resume_payload_json, schema_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, run_id) DO UPDATE SET
                    job_id = excluded.job_id,
                    session_id = excluded.session_id,
                    phase = excluded.phase,
                    cursor_status = excluded.cursor_status,
                    last_checkpoint_id = excluded.last_checkpoint_id,
                    last_write_order = excluded.last_write_order,
                    external_effect_id = excluded.external_effect_id,
                    resume_payload_json = excluded.resume_payload_json,
                    schema_version = excluded.schema_version,
                    updated_at = excluded.updated_at
                """,
                (
                    row_id,
                    task_id,
                    run_id,
                    job_id,
                    session_id,
                    phase,
                    cursor_status,
                    last_checkpoint_id,
                    last_write_order,
                    external_effect_id,
                    resume_payload_json,
                    schema_version,
                    now,
                    now,
                ),
            )
            row = cur.execute(
                """
                SELECT * FROM phase_recovery_cursors
                WHERE task_id = ? AND run_id = ?
                """,
                (task_id, run_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("recovery cursor upsert did not return a readable row")
        return self._recovery_cursor_from_row(row)

    def get_recovery_cursor(
        self,
        *,
        task_id: str,
        run_id: str,
    ) -> RecoveryCursorRecord | None:
        _require_nonblank("task_id", task_id)
        _require_nonblank("run_id", run_id)
        with self._db.cursor() as cur:
            row = cur.execute(
                """
                SELECT * FROM phase_recovery_cursors
                WHERE task_id = ? AND run_id = ?
                """,
                (task_id, run_id),
            ).fetchone()
        return self._recovery_cursor_from_row(row) if row is not None else None

    def list_recovery_cursors(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        phase: str | None = None,
        cursor_status: str | None = None,
        updated_at_after: str | None = None,
        updated_at_before: str | None = None,
        order: str = "updated_at_desc",
        limit: int | None = None,
    ) -> list[RecoveryCursorRecord]:
        where, params = _where_filters(
            {
                "task_id": task_id,
                "run_id": run_id,
                "phase": phase,
                "cursor_status": cursor_status,
            },
            updated_at_after=updated_at_after,
            updated_at_before=updated_at_before,
        )
        sql = "SELECT * FROM phase_recovery_cursors" + where
        sql += " ORDER BY " + _recovery_cursor_order_by(order)
        sql, params = _append_limit(sql, params, limit)
        with self._db.cursor() as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._recovery_cursor_from_row(row) for row in rows]

    def _json_dumps(self, value: Any) -> str:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(rendered.encode("utf-8")) > self._max_json_bytes:
            raise ValueError("JSON payload exceeds max_json_bytes")
        return rendered

    def _json_loads(self, raw: str | None) -> tuple[Any | None, str | None]:
        if raw is None:
            return None, None
        if len(raw.encode("utf-8", errors="replace")) > self._max_json_bytes:
            return None, "JSON payload exceeds max_json_bytes"
        try:
            return json.loads(raw), None
        except (TypeError, json.JSONDecodeError) as exc:
            return None, str(exc)

    def _phase_checkpoint_from_row(self, row: Any) -> PhaseCheckpointRecord:
        payload, error = self._json_loads(row["payload_json"])
        return PhaseCheckpointRecord(
            checkpoint_id=row["checkpoint_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            job_id=row["job_id"],
            session_id=row["session_id"],
            phase=row["phase"],
            phase_version=int(row["phase_version"]),
            status=row["status"],
            schema_version=int(row["schema_version"]),
            last_write_order=int(row["last_write_order"]),
            payload=payload,
            payload_json=row["payload_json"],
            payload_sha256=row["payload_sha256"],
            payload_error=error,
            orchestration_run_id=row["orchestration_run_id"],
            orchestration_checkpoint_id=row["orchestration_checkpoint_id"],
            created_at=row["created_at"],
        )

    def _checkpoint_write_from_row(self, row: Any) -> CheckpointWriteRecord:
        payload, error = self._json_loads(row["payload_json"])
        return CheckpointWriteRecord(
            write_id=row["write_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            job_id=row["job_id"],
            phase=row["phase"],
            write_order=int(row["write_order"]),
            write_kind=row["write_kind"],
            write_key=row["write_key"],
            schema_version=int(row["schema_version"]),
            payload=payload,
            payload_json=row["payload_json"],
            payload_sha256=row["payload_sha256"],
            payload_error=error,
            external_effect_id=row["external_effect_id"],
            created_at=row["created_at"],
        )

    def _external_effect_from_row(self, row: Any) -> ExternalEffectRecord:
        request, request_error = self._json_loads(row["request_json"])
        verification, verification_error = self._json_loads(row["verification_json"])
        result, result_error = self._json_loads(row["result_json"])
        return ExternalEffectRecord(
            external_effect_id=row["external_effect_id"],
            idempotency_key=row["idempotency_key"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            job_id=row["job_id"],
            phase=row["phase"],
            effect_kind=row["effect_kind"],
            target=row["target"],
            content_hash=row["content_hash"],
            request=request,
            request_json=row["request_json"],
            request_sha256=row["request_sha256"],
            request_error=request_error,
            status=row["status"],
            attempt_count=int(row["attempt_count"]),
            verifier_kind=row["verifier_kind"],
            verification=verification,
            verification_json=row["verification_json"],
            verification_error=verification_error,
            result=result,
            result_json=row["result_json"],
            result_sha256=row["result_sha256"],
            result_error=result_error,
            error=row["error"],
            schema_version=int(row["schema_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _recovery_cursor_from_row(self, row: Any) -> RecoveryCursorRecord:
        resume_payload, error = self._json_loads(row["resume_payload_json"])
        return RecoveryCursorRecord(
            recovery_cursor_id=row["recovery_cursor_id"],
            task_id=row["task_id"],
            run_id=row["run_id"],
            job_id=row["job_id"],
            session_id=row["session_id"],
            phase=row["phase"],
            cursor_status=row["cursor_status"],
            last_checkpoint_id=row["last_checkpoint_id"],
            last_write_order=int(row["last_write_order"]),
            external_effect_id=row["external_effect_id"],
            resume_payload=resume_payload,
            resume_payload_json=row["resume_payload_json"],
            resume_payload_error=error,
            schema_version=int(row["schema_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def compute_external_effect_idempotency_key(
    *,
    task_id: str,
    run_id: str,
    phase: str,
    effect_kind: str,
    target: str,
    content_hash: str,
    schema_version: int = F2_DURABILITY_SCHEMA_VERSION,
) -> str:
    validate_f2_schema_version(schema_version)
    parts = (task_id, run_id, phase, effect_kind, target, content_hash, str(schema_version))
    for name, value in zip(
        ("task_id", "run_id", "phase", "effect_kind", "target", "content_hash"),
        parts,
        strict=False,
    ):
        _require_nonblank(name, value)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _ensure_schema_ready(runtime_db: RuntimeDb) -> None:
    with _SCHEMA_READY_LOCK:
        if runtime_db in _SCHEMA_READY_RUNTIME_DBS:
            return
        ensure_f2_durability_schema(runtime_db)
        _SCHEMA_READY_RUNTIME_DBS.add(runtime_db)


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex}"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_nonblank(name: str, value: str) -> None:
    if not str(value or "").strip():
        raise ValueError(f"{name} is required")


def _validate_status(label: str, status: str, allowed: frozenset[str]) -> None:
    if status not in allowed:
        raise ValueError(f"invalid {label} status: {status}")


def _where_filters(
    equal_filters: dict[str, str | None],
    *,
    created_at_after: str | None = None,
    created_at_before: str | None = None,
    updated_at_after: str | None = None,
    updated_at_before: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in equal_filters.items():
        if value is None:
            continue
        clauses.append(f"{column} = ?")
        params.append(value)
    if created_at_after is not None:
        clauses.append("created_at >= ?")
        params.append(created_at_after)
    if created_at_before is not None:
        clauses.append("created_at <= ?")
        params.append(created_at_before)
    if updated_at_after is not None:
        clauses.append("updated_at >= ?")
        params.append(updated_at_after)
    if updated_at_before is not None:
        clauses.append("updated_at <= ?")
        params.append(updated_at_before)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _append_limit(sql: str, params: list[Any], limit: int | None) -> tuple[str, list[Any]]:
    if limit is None:
        return sql, params
    if limit < 1:
        raise ValueError("limit must be >= 1")
    return sql + " LIMIT ?", [*params, limit]


def _checkpoint_order_by(order: str) -> str:
    return _order_by(
        order,
        {
            "created_at_asc": "created_at ASC, phase_version ASC, checkpoint_id ASC",
            "created_at_desc": "created_at DESC, phase_version DESC, checkpoint_id ASC",
            "phase_version_asc": "phase_version ASC, created_at ASC, checkpoint_id ASC",
            "phase_version_desc": "phase_version DESC, created_at DESC, checkpoint_id ASC",
        },
    )


def _write_order_by(order: str) -> str:
    return _order_by(
        order,
        {
            "write_order_asc": "write_order ASC, created_at ASC, write_id ASC",
            "write_order_desc": "write_order DESC, created_at DESC, write_id ASC",
            "created_at_asc": "created_at ASC, write_order ASC, write_id ASC",
            "created_at_desc": "created_at DESC, write_order DESC, write_id ASC",
        },
    )


def _external_effect_order_by(order: str) -> str:
    return _order_by(
        order,
        {
            "created_at_asc": "created_at ASC, external_effect_id ASC",
            "created_at_desc": "created_at DESC, external_effect_id ASC",
            "updated_at_asc": "updated_at ASC, external_effect_id ASC",
            "updated_at_desc": "updated_at DESC, external_effect_id ASC",
        },
    )


def _recovery_cursor_order_by(order: str) -> str:
    return _order_by(
        order,
        {
            "created_at_asc": "created_at ASC, recovery_cursor_id ASC",
            "created_at_desc": "created_at DESC, recovery_cursor_id ASC",
            "updated_at_asc": "updated_at ASC, recovery_cursor_id ASC",
            "updated_at_desc": "updated_at DESC, recovery_cursor_id ASC",
        },
    )


def _order_by(order: str, allowed: dict[str, str]) -> str:
    try:
        return allowed[order]
    except KeyError as exc:
        raise ValueError(f"unsupported order: {order}") from exc
