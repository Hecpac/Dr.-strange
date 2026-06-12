from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.redaction import redact_sensitive
from claw_v2.sqlite_runtime import (
    connect_runtime_sqlite,
    make_store_wal_heal,
    register_wal_heal,
)
from claw_v2.tracing import TRACE_KEYS


RUN_STATUSES = frozenset({"running", "blocked", "failed", "succeeded", "alarm"})
PHASE_STATUSES = frozenset({"running", "blocked", "failed", "succeeded", "alarm"})
ACK_STATUSES = frozenset({"received", "rejected"})
ARTIFACT_REF_KEYS = frozenset({"artifact_ref", "artifact_refs", "artifact_path", "artifact_paths"})

ARTIFACT_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema_version",
        "artifact_id",
        "run_id",
        "phase",
        "artifact_type",
        "producer_role",
        "consumer_role",
        "trace",
        "payload_sha256",
        "payload",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "const": "orchestration_artifact.v1"},
        "artifact_id": {"type": "string"},
        "run_id": {"type": "string"},
        "phase": {"type": "string"},
        "artifact_type": {"type": "string"},
        "producer_role": {"type": "string"},
        "consumer_role": {"type": ["string", "null"]},
        "trace": {"type": "object"},
        "payload_sha256": {"type": "string"},
        "payload": {"type": "object"},
    },
}

ACK_ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "schema_version",
        "ack_id",
        "artifact_id",
        "run_id",
        "consumer_role",
        "status",
        "expected_artifact_schema",
        "details",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "const": "orchestration_ack.v1"},
        "ack_id": {"type": "string"},
        "artifact_id": {"type": "string"},
        "run_id": {"type": "string"},
        "consumer_role": {"type": "string"},
        "status": {"type": "string", "enum": ["received", "rejected"]},
        "expected_artifact_schema": {"type": "string"},
        "details": {"type": "object"},
    },
}

ORCHESTRATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS orchestration_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_id TEXT,
    objective TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'coordinator',
    status TEXT NOT NULL CHECK(status IN ('running', 'blocked', 'failed', 'succeeded', 'alarm')),
    version INTEGER NOT NULL DEFAULT 1,
    current_phase TEXT,
    checkpoint_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS orchestration_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    phase TEXT,
    version INTEGER NOT NULL,
    trace_id TEXT,
    root_trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    job_id TEXT,
    artifact_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    producer_role TEXT NOT NULL,
    consumer_role TEXT,
    payload_sha256 TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    trace_id TEXT,
    ack_required INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_acks (
    ack_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    consumer_role TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('received', 'rejected')),
    expected_artifact_schema TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    phase TEXT,
    version INTEGER NOT NULL,
    reason TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orchestration_events_run_id
    ON orchestration_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_orchestration_events_trace_id
    ON orchestration_events(trace_id, id);
CREATE INDEX IF NOT EXISTS idx_orchestration_artifacts_run_id
    ON orchestration_artifacts(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orchestration_acks_artifact_id
    ON orchestration_acks(artifact_id, created_at);
"""


class OrchestrationError(RuntimeError):
    pass


class OrchestrationVersionConflict(OrchestrationError):
    pass


class OrchestrationValidationError(OrchestrationError):
    pass


class OrchestrationGateError(OrchestrationError):
    pass


@dataclass(frozen=True, slots=True)
class OrchestrationRun:
    run_id: str
    task_id: str
    objective: str
    kind: str
    status: str
    version: int
    session_id: str | None = None
    current_phase: str | None = None
    checkpoint_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OrchestrationArtifact:
    artifact_id: str
    run_id: str
    phase: str
    artifact_type: str
    schema_version: str
    producer_role: str
    consumer_role: str | None
    payload_sha256: str
    envelope: dict[str, Any]
    trace_id: str | None
    ack_required: bool
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OrchestrationAck:
    ack_id: str
    artifact_id: str
    run_id: str
    consumer_role: str
    status: str
    expected_artifact_schema: str
    envelope: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OrchestrationStore:
    """Versioned orchestration control plane plus artifact handshakes.

    The shared state is intentionally small: run status, phase, version, and
    checkpoint pointer. Rich worker outputs live as immutable JSON artifacts
    and must be acknowledged by the next consumer role.
    """

    def __init__(self, db_path: Path | str, *, observe: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.observe = observe
        self._conn = connect_runtime_sqlite(self.db_path)
        register_wal_heal(self.db_path, make_store_wal_heal(self))
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(ORCHESTRATION_SCHEMA)
            self._conn.commit()

    def begin_run(
        self,
        *,
        task_id: str,
        objective: str,
        run_id: str | None = None,
        session_id: str | None = None,
        kind: str = "coordinator",
        metadata: dict[str, Any] | None = None,
        trace_context: dict[str, Any] | None = None,
    ) -> OrchestrationRun:
        run_id = run_id or f"run:{task_id}"
        now = time.time()
        clean_metadata = _clean_dict(metadata)
        clean_objective = str(redact_sensitive(objective, limit=0))
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM orchestration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                version = 1
                self._conn.execute(
                    """
                    INSERT INTO orchestration_runs (
                        run_id, task_id, session_id, objective, kind, status,
                        version, current_phase, checkpoint_id, metadata_json,
                        created_at, updated_at, completed_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, NULL, ?, ?, ?, NULL)
                    """,
                    (
                        run_id,
                        task_id,
                        session_id,
                        clean_objective,
                        kind,
                        version,
                        _json_dumps(clean_metadata),
                        now,
                        now,
                    ),
                )
            else:
                version = int(row["version"]) + 1
                self._conn.execute(
                    """
                    UPDATE orchestration_runs
                    SET status = 'running',
                        version = ?,
                        objective = ?,
                        kind = ?,
                        session_id = COALESCE(?, session_id),
                        metadata_json = ?,
                        updated_at = ?,
                        completed_at = NULL
                    WHERE run_id = ?
                    """,
                    (
                        version,
                        clean_objective,
                        kind,
                        session_id,
                        _json_dumps(clean_metadata),
                        now,
                        run_id,
                    ),
                )
            self._insert_event_unlocked(
                run_id=run_id,
                event_type="run_started",
                phase=None,
                version=version,
                trace_context=trace_context,
                payload={"task_id": task_id, "kind": kind},
                artifact_id=None,
                created_at=now,
            )
            self._conn.commit()
        self._emit("orchestration_run_started", run_id=run_id, payload={"task_id": task_id, "kind": kind})
        return self.get_run(run_id)  # type: ignore[return-value]

    def begin_phase(
        self,
        run_id: str,
        phase: str,
        *,
        expected_version: int | None = None,
        trace_context: dict[str, Any] | None = None,
        max_phase_attempts: int | None = None,
    ) -> OrchestrationRun:
        if max_phase_attempts is not None:
            if max_phase_attempts < 1:
                raise ValueError("max_phase_attempts must be >= 1")
            attempts = self.phase_attempt_count(run_id, phase)
            if attempts >= max_phase_attempts:
                self.alarm_run(
                    run_id,
                    reason="max_phase_attempts_exceeded",
                    phase=phase,
                    payload={
                        "attempts": attempts,
                        "max_phase_attempts": max_phase_attempts,
                    },
                )
                raise OrchestrationGateError(
                    f"phase {phase} exceeded max_phase_attempts={max_phase_attempts}"
                )
        return self._transition_run(
            run_id,
            event_type="phase_started",
            status="running",
            phase=phase,
            expected_version=expected_version,
            trace_context=trace_context,
            payload={"phase": phase},
        )

    def finish_phase(
        self,
        run_id: str,
        phase: str,
        *,
        status: str,
        expected_version: int | None = None,
        trace_context: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> OrchestrationRun:
        if status not in PHASE_STATUSES:
            raise ValueError(f"invalid phase status: {status}")
        run_status = "running" if status == "succeeded" else status
        return self._transition_run(
            run_id,
            event_type="phase_finished",
            status=run_status,
            phase=phase,
            expected_version=expected_version,
            trace_context=trace_context,
            payload={"phase": phase, "status": status, **dict(payload or {})},
        )

    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        reason: str = "",
        expected_version: int | None = None,
        trace_context: dict[str, Any] | None = None,
        required_artifact_types: list[str] | tuple[str, ...] | None = None,
    ) -> OrchestrationRun:
        if status not in RUN_STATUSES - {"running"}:
            raise ValueError(f"terminal status required, got {status!r}")
        if status == "succeeded" and required_artifact_types:
            missing = self.missing_required_artifact_types(
                run_id,
                required_artifact_types=required_artifact_types,
            )
            if missing:
                self.alarm_run(
                    run_id,
                    reason="final_gate_missing_required_artifacts",
                    payload={
                        "requested_status": status,
                        "missing_artifact_types": missing,
                    },
                )
                raise OrchestrationGateError(
                    "final gate rejected success; missing artifacts: "
                    + ",".join(missing)
                )
        return self._transition_run(
            run_id,
            event_type="run_completed",
            status=status,
            phase=None,
            expected_version=expected_version,
            trace_context=trace_context,
            payload={"status": status, "reason": reason},
            completed=True,
        )

    def record_artifact(
        self,
        run_id: str,
        *,
        phase: str,
        artifact_type: str,
        payload: dict[str, Any],
        producer_role: str,
        consumer_role: str | None,
        trace_context: dict[str, Any] | None = None,
        ack_required: bool = True,
    ) -> OrchestrationArtifact:
        if not isinstance(payload, dict):
            raise OrchestrationValidationError("artifact payload must be an object")
        now = time.time()
        artifact_id = f"art:{uuid.uuid4().hex[:12]}"
        clean_payload = _clean_dict(payload)
        _validate_artifact_refs(clean_payload)
        payload_hash = _sha256_json(clean_payload)
        envelope = {
            "schema_version": "orchestration_artifact.v1",
            "artifact_id": artifact_id,
            "run_id": run_id,
            "phase": phase,
            "artifact_type": artifact_type,
            "producer_role": producer_role,
            "consumer_role": consumer_role,
            "trace": _trace_dict(trace_context),
            "payload_sha256": payload_hash,
            "payload": clean_payload,
        }
        _validate_artifact_envelope(envelope)
        with self._lock:
            version = self._next_version_unlocked(run_id)
            self._conn.execute(
                """
                INSERT INTO orchestration_artifacts (
                    artifact_id, run_id, phase, artifact_type, schema_version,
                    producer_role, consumer_role, payload_sha256, envelope_json,
                    trace_id, ack_required, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    phase,
                    artifact_type,
                    envelope["schema_version"],
                    producer_role,
                    consumer_role,
                    payload_hash,
                    _json_dumps(envelope),
                    _trace_dict(trace_context).get("trace_id"),
                    1 if ack_required else 0,
                    now,
                ),
            )
            self._insert_event_unlocked(
                run_id=run_id,
                event_type="artifact_recorded",
                phase=phase,
                version=version,
                trace_context=trace_context,
                payload={
                    "artifact_type": artifact_type,
                    "producer_role": producer_role,
                    "consumer_role": consumer_role,
                    "ack_required": ack_required,
                    "payload_sha256": payload_hash,
                },
                artifact_id=artifact_id,
                created_at=now,
            )
            self._conn.commit()
        self._emit(
            "orchestration_artifact_recorded",
            run_id=run_id,
            artifact_id=artifact_id,
            payload={"phase": phase, "artifact_type": artifact_type},
        )
        return self.get_artifact(artifact_id)  # type: ignore[return-value]

    def acknowledge_artifact(
        self,
        artifact_id: str,
        *,
        consumer_role: str,
        status: str = "received",
        expected_artifact_schema: str = "orchestration_artifact.v1",
        details: dict[str, Any] | None = None,
    ) -> OrchestrationAck:
        if status not in ACK_STATUSES:
            raise ValueError(f"invalid ack status: {status}")
        artifact = self.get_artifact(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        if expected_artifact_schema != artifact.schema_version:
            status = "rejected"
            details = {
                **dict(details or {}),
                "reason": "artifact_schema_mismatch",
                "actual_schema": artifact.schema_version,
            }
        if artifact.consumer_role and artifact.consumer_role != consumer_role:
            status = "rejected"
            details = {
                **dict(details or {}),
                "reason": "consumer_role_mismatch",
                "expected_consumer_role": artifact.consumer_role,
            }
        now = time.time()
        ack_id = f"ack:{uuid.uuid4().hex[:12]}"
        envelope = {
            "schema_version": "orchestration_ack.v1",
            "ack_id": ack_id,
            "artifact_id": artifact_id,
            "run_id": artifact.run_id,
            "consumer_role": consumer_role,
            "status": status,
            "expected_artifact_schema": expected_artifact_schema,
            "details": _clean_dict(details),
        }
        _validate_ack_envelope(envelope)
        with self._lock:
            version = self._next_version_unlocked(artifact.run_id)
            self._conn.execute(
                """
                INSERT INTO orchestration_acks (
                    ack_id, artifact_id, run_id, consumer_role, status,
                    expected_artifact_schema, envelope_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ack_id,
                    artifact_id,
                    artifact.run_id,
                    consumer_role,
                    status,
                    expected_artifact_schema,
                    _json_dumps(envelope),
                    now,
                ),
            )
            self._insert_event_unlocked(
                run_id=artifact.run_id,
                event_type="artifact_acknowledged",
                phase=artifact.phase,
                version=version,
                trace_context=artifact.envelope.get("trace") if isinstance(artifact.envelope, dict) else None,
                payload={
                    "ack_id": ack_id,
                    "consumer_role": consumer_role,
                    "status": status,
                    "expected_artifact_schema": expected_artifact_schema,
                },
                artifact_id=artifact_id,
                created_at=now,
            )
            self._conn.commit()
        self._emit(
            "orchestration_artifact_acknowledged",
            run_id=artifact.run_id,
            artifact_id=artifact_id,
            payload={"ack_id": ack_id, "consumer_role": consumer_role, "status": status},
        )
        return self.get_ack(ack_id)  # type: ignore[return-value]

    def require_ack_received(
        self,
        artifact_id: str,
        *,
        consumer_role: str | None = None,
    ) -> OrchestrationAck:
        artifact = self.get_artifact(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        clauses = ["artifact_id = ?", "status = 'received'"]
        params: list[Any] = [artifact_id]
        if consumer_role:
            clauses.append("consumer_role = ?")
            params.append(consumer_role)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT *
                FROM orchestration_acks
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        if row is not None:
            return _ack_from_row(row)
        self.alarm_run(
            artifact.run_id,
            reason="missing_required_ack",
            phase=artifact.phase,
            payload={
                "artifact_id": artifact_id,
                "consumer_role": consumer_role or artifact.consumer_role,
            },
        )
        raise OrchestrationGateError(f"missing required ack for artifact {artifact_id}")

    def checkpoint(
        self,
        run_id: str,
        *,
        phase: str | None,
        reason: str,
        state: dict[str, Any] | None = None,
        artifact_ids: list[str] | None = None,
    ) -> str:
        checkpoint_id = f"orch-ckpt:{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._lock:
            version = self._next_version_unlocked(run_id, checkpoint_id=checkpoint_id)
            self._conn.execute(
                """
                INSERT INTO orchestration_checkpoints (
                    checkpoint_id, run_id, phase, version, reason,
                    state_json, artifact_ids_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    run_id,
                    phase,
                    version,
                    reason,
                    _json_dumps(_clean_dict(state)),
                    _json_dumps([str(item) for item in artifact_ids or []]),
                    now,
                ),
            )
            self._insert_event_unlocked(
                run_id=run_id,
                event_type="checkpoint_created",
                phase=phase,
                version=version,
                trace_context=None,
                payload={"checkpoint_id": checkpoint_id, "reason": reason},
                artifact_id=None,
                created_at=now,
            )
            self._conn.commit()
        self._emit("orchestration_checkpoint_created", run_id=run_id, payload={"checkpoint_id": checkpoint_id})
        return checkpoint_id

    def alarm_run(
        self,
        run_id: str,
        *,
        reason: str,
        phase: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> OrchestrationRun:
        return self._transition_run(
            run_id,
            event_type="orchestration_alarm",
            status="alarm",
            phase=phase,
            expected_version=None,
            trace_context=None,
            payload={"reason": reason, **dict(payload or {})},
        )

    def get_run(self, run_id: str) -> OrchestrationRun | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestration_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def get_artifact(self, artifact_id: str) -> OrchestrationArtifact | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestration_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return _artifact_from_row(row) if row is not None else None

    def get_ack(self, ack_id: str) -> OrchestrationAck | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orchestration_acks WHERE ack_id = ?",
                (ack_id,),
            ).fetchone()
        return _ack_from_row(row) if row is not None else None

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM orchestration_events
                WHERE run_id = ?
                ORDER BY id ASC
                """,
                (run_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def phase_attempt_count(self, run_id: str, phase: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*)
                FROM orchestration_events
                WHERE run_id = ?
                  AND phase = ?
                  AND event_type = 'phase_started'
                """,
                (run_id, phase),
            ).fetchone()
        return int(row[0] if row is not None else 0)

    def missing_required_artifact_types(
        self,
        run_id: str,
        *,
        required_artifact_types: list[str] | tuple[str, ...],
    ) -> list[str]:
        missing: list[str] = []
        with self._lock:
            for artifact_type in required_artifact_types:
                row = self._conn.execute(
                    """
                    SELECT a.artifact_id
                    FROM orchestration_artifacts a
                    WHERE a.run_id = ?
                      AND a.artifact_type = ?
                      AND (
                          a.ack_required = 0
                          OR EXISTS (
                              SELECT 1
                              FROM orchestration_acks k
                              WHERE k.artifact_id = a.artifact_id
                                AND k.status = 'received'
                          )
                      )
                    LIMIT 1
                    """,
                    (run_id, str(artifact_type)),
                ).fetchone()
                if row is None:
                    missing.append(str(artifact_type))
        return missing

    def audit_report(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        events = self.run_events(run_id)
        gaps: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for event in events:
            if previous is not None:
                gaps.append(
                    {
                        "from_event": previous["event_type"],
                        "to_event": event["event_type"],
                        "gap_seconds": round(float(event["created_at"]) - float(previous["created_at"]), 3),
                    }
                )
            previous = event
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT a.artifact_id, a.phase, a.artifact_type, a.consumer_role
                FROM orchestration_artifacts a
                WHERE a.run_id = ?
                  AND a.ack_required = 1
                  AND NOT EXISTS (
                      SELECT 1
                      FROM orchestration_acks k
                      WHERE k.artifact_id = a.artifact_id
                        AND k.status = 'received'
                  )
                ORDER BY a.created_at ASC
                """,
                (run_id,),
            ).fetchall()
        return {
            "run": run.to_dict() if run is not None else None,
            "event_count": len(events),
            "events": events,
            "gaps": gaps,
            "missing_acks": [dict(row) for row in rows],
        }

    def _transition_run(
        self,
        run_id: str,
        *,
        event_type: str,
        status: str,
        phase: str | None,
        expected_version: int | None,
        trace_context: dict[str, Any] | None,
        payload: dict[str, Any],
        completed: bool = False,
    ) -> OrchestrationRun:
        if status not in RUN_STATUSES:
            raise ValueError(f"invalid run status: {status}")
        now = time.time()
        with self._lock:
            version = self._next_version_unlocked(
                run_id,
                expected_version=expected_version,
                status=status,
                phase=phase,
                completed_at=now if completed else None,
            )
            self._insert_event_unlocked(
                run_id=run_id,
                event_type=event_type,
                phase=phase,
                version=version,
                trace_context=trace_context,
                payload=payload,
                artifact_id=None,
                created_at=now,
            )
            self._conn.commit()
        emit_event_type = (
            event_type if event_type.startswith("orchestration_")
            else f"orchestration_{event_type}"
        )
        self._emit(emit_event_type, run_id=run_id, payload=payload)
        return self.get_run(run_id)  # type: ignore[return-value]

    def _next_version_unlocked(
        self,
        run_id: str,
        *,
        expected_version: int | None = None,
        status: str | None = None,
        phase: str | None = None,
        checkpoint_id: str | None = None,
        completed_at: float | None = None,
    ) -> int:
        row = self._conn.execute(
            "SELECT version FROM orchestration_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        current = int(row["version"])
        if expected_version is not None and expected_version != current:
            raise OrchestrationVersionConflict(
                f"run {run_id} expected version {expected_version}, found {current}"
            )
        version = current + 1
        assignments = ["version = ?", "updated_at = ?"]
        params: list[Any] = [version, time.time()]
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if phase is not None:
            assignments.append("current_phase = ?")
            params.append(phase)
        if checkpoint_id is not None:
            assignments.append("checkpoint_id = ?")
            params.append(checkpoint_id)
        if completed_at is not None:
            assignments.append("completed_at = ?")
            params.append(completed_at)
        params.append(run_id)
        self._conn.execute(
            f"UPDATE orchestration_runs SET {', '.join(assignments)} WHERE run_id = ?",
            params,
        )
        return version

    def _insert_event_unlocked(
        self,
        *,
        run_id: str,
        event_type: str,
        phase: str | None,
        version: int,
        trace_context: dict[str, Any] | None,
        payload: dict[str, Any],
        artifact_id: str | None,
        created_at: float,
    ) -> None:
        trace = _trace_dict(trace_context)
        clean_payload = _clean_dict(payload)
        self._conn.execute(
            """
            INSERT INTO orchestration_events (
                run_id, event_type, phase, version,
                trace_id, root_trace_id, span_id, parent_span_id,
                job_id, artifact_id, payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                phase,
                version,
                trace.get("trace_id"),
                trace.get("root_trace_id"),
                trace.get("span_id"),
                trace.get("parent_span_id"),
                trace.get("job_id"),
                artifact_id or trace.get("artifact_id"),
                _json_dumps(clean_payload),
                created_at,
            ),
        )

    def _emit(
        self,
        event_type: str,
        *,
        run_id: str,
        payload: dict[str, Any],
        artifact_id: str | None = None,
    ) -> None:
        if self.observe is None:
            return
        self.observe.emit(
            event_type,
            lane="orchestration",
            job_id=run_id,
            artifact_id=artifact_id,
            payload={"run_id": run_id, **_clean_dict(payload)},
        )


def _validate_artifact_envelope(envelope: dict[str, Any]) -> None:
    _validate_envelope(envelope, ARTIFACT_ENVELOPE_SCHEMA)
    if envelope.get("schema_version") != "orchestration_artifact.v1":
        raise OrchestrationValidationError("invalid artifact schema_version")
    if not isinstance(envelope.get("trace"), dict):
        raise OrchestrationValidationError("artifact trace must be object")
    if not isinstance(envelope.get("payload"), dict):
        raise OrchestrationValidationError("artifact payload must be object")


def _validate_ack_envelope(envelope: dict[str, Any]) -> None:
    _validate_envelope(envelope, ACK_ENVELOPE_SCHEMA)
    if envelope.get("schema_version") != "orchestration_ack.v1":
        raise OrchestrationValidationError("invalid ack schema_version")
    if envelope.get("status") not in ACK_STATUSES:
        raise OrchestrationValidationError("invalid ack status")


def _validate_artifact_refs(value: Any, *, key: str | None = None) -> None:
    if key in ARTIFACT_REF_KEYS:
        _validate_artifact_ref_value(value, key=key)
        return
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _validate_artifact_refs(child_value, key=str(child_key))
        return
    if isinstance(value, list):
        for item in value:
            _validate_artifact_refs(item, key=key)
        return


def _validate_artifact_ref_value(value: Any, *, key: str | None) -> None:
    if isinstance(value, list):
        for item in value:
            _validate_artifact_ref_value(item, key=key)
        return
    if isinstance(value, dict):
        for path_key in ("path", "file_path", "artifact_ref"):
            if path_key in value:
                _validate_artifact_ref_value(value[path_key], key=key)
                return
        raise OrchestrationValidationError(f"invalid_artifact_ref:{key}")
    if not isinstance(value, str) or not value:
        raise OrchestrationValidationError(f"invalid_artifact_ref:{key}")
    if value.startswith("art:") or "://" in value:
        return
    path = Path(value).expanduser()
    if not path.exists():
        raise OrchestrationValidationError(f"missing_artifact_ref:{value}")


def _validate_envelope(envelope: dict[str, Any], schema: dict[str, Any]) -> None:
    required = set(schema.get("required") or [])
    missing = required - set(envelope.keys())
    if missing:
        raise OrchestrationValidationError("missing:" + ",".join(sorted(missing)))
    allowed = set((schema.get("properties") or {}).keys())
    extras = set(envelope.keys()) - allowed
    if extras:
        raise OrchestrationValidationError("additional_properties:" + ",".join(sorted(extras)))


def _run_from_row(row: sqlite3.Row) -> OrchestrationRun:
    return OrchestrationRun(
        run_id=str(row["run_id"]),
        task_id=str(row["task_id"]),
        session_id=_optional_str(row["session_id"]),
        objective=str(row["objective"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        version=int(row["version"]),
        current_phase=_optional_str(row["current_phase"]),
        checkpoint_id=_optional_str(row["checkpoint_id"]),
        metadata=_loads_json_object(row["metadata_json"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
        completed_at=_optional_float(row["completed_at"]),
    )


def _artifact_from_row(row: sqlite3.Row) -> OrchestrationArtifact:
    return OrchestrationArtifact(
        artifact_id=str(row["artifact_id"]),
        run_id=str(row["run_id"]),
        phase=str(row["phase"]),
        artifact_type=str(row["artifact_type"]),
        schema_version=str(row["schema_version"]),
        producer_role=str(row["producer_role"]),
        consumer_role=_optional_str(row["consumer_role"]),
        payload_sha256=str(row["payload_sha256"]),
        envelope=_loads_json_object(row["envelope_json"]),
        trace_id=_optional_str(row["trace_id"]),
        ack_required=bool(row["ack_required"]),
        created_at=float(row["created_at"]),
    )


def _ack_from_row(row: sqlite3.Row) -> OrchestrationAck:
    return OrchestrationAck(
        ack_id=str(row["ack_id"]),
        artifact_id=str(row["artifact_id"]),
        run_id=str(row["run_id"]),
        consumer_role=str(row["consumer_role"]),
        status=str(row["status"]),
        expected_artifact_schema=str(row["expected_artifact_schema"]),
        envelope=_loads_json_object(row["envelope_json"]),
        created_at=float(row["created_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "run_id": str(row["run_id"]),
        "event_type": str(row["event_type"]),
        "phase": _optional_str(row["phase"]),
        "version": int(row["version"]),
        "trace_id": _optional_str(row["trace_id"]),
        "root_trace_id": _optional_str(row["root_trace_id"]),
        "span_id": _optional_str(row["span_id"]),
        "parent_span_id": _optional_str(row["parent_span_id"]),
        "job_id": _optional_str(row["job_id"]),
        "artifact_id": _optional_str(row["artifact_id"]),
        "payload": _loads_json_object(row["payload_json"]),
        "created_at": float(row["created_at"]),
    }


def _clean_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    redacted = redact_sensitive(value, limit=0)
    return dict(redacted) if isinstance(redacted, dict) else {}


def _trace_dict(trace_context: dict[str, Any] | None) -> dict[str, str]:
    trace_context = trace_context or {}
    result: dict[str, str] = {}
    for key in TRACE_KEYS:
        value = trace_context.get(key)
        if value is not None:
            result[key] = str(value)
    return result


def _sha256_json(value: dict[str, Any]) -> str:
    return hashlib.sha256(_json_dumps(value).encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)


def _loads_json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)
