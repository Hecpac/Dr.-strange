from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timed_out", "cancelled", "lost"})
VALID_STATUSES = frozenset({"queued", "running", *TERMINAL_STATUSES})


TASK_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    channel TEXT,
    external_session_id TEXT,
    external_user_id TEXT,
    objective TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    runtime TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'timed_out', 'cancelled', 'lost')),
    notify_policy TEXT NOT NULL DEFAULT 'done_only',
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT 'unknown',
    artifacts_json TEXT NOT NULL DEFAULT '{}',
    route_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_session_updated
    ON agent_tasks(session_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_status_updated
    ON agent_tasks(status, updated_at DESC);
"""


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    session_id: str
    objective: str
    runtime: str
    status: str = "queued"
    mode: str = ""
    provider: str | None = None
    model: str | None = None
    channel: str | None = None
    external_session_id: str | None = None
    external_user_id: str | None = None
    notify_policy: str = "done_only"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    summary: str = ""
    error: str = ""
    verification_status: str = "unknown"
    artifacts: dict[str, Any] = field(default_factory=dict)
    route: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskLedger:
    """Durable task activity ledger.

    This is separate from the chat-level task queue. The queue models next
    actions inside a session; the ledger records detached work lifecycle.
    """

    def __init__(self, db_path: Path | str, *, observe: Any | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.observe = observe
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(TASK_LEDGER_SCHEMA)
            self._conn.commit()

    def create(
        self,
        *,
        task_id: str,
        session_id: str,
        objective: str,
        runtime: str,
        mode: str = "",
        provider: str | None = None,
        model: str | None = None,
        status: str = "queued",
        notify_policy: str = "done_only",
        route: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
    ) -> TaskRecord:
        self._validate_status(status)
        now = time.time()
        route = dict(route or {})
        record = TaskRecord(
            task_id=task_id,
            session_id=session_id,
            objective=objective,
            runtime=runtime,
            mode=mode,
            provider=provider,
            model=model,
            status=status,
            notify_policy=notify_policy,
            started_at=now if status == "running" else None,
            completed_at=now if status in TERMINAL_STATUSES else None,
            route=route,
            channel=_as_optional_str(route.get("channel")),
            external_session_id=_as_optional_str(route.get("external_session_id")),
            external_user_id=_as_optional_str(route.get("external_user_id")),
            metadata=dict(metadata or {}),
            artifacts=dict(artifacts or {}),
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO agent_tasks (
                    task_id, session_id, channel, external_session_id, external_user_id,
                    objective, mode, runtime, provider, model, status, notify_policy,
                    created_at, started_at, completed_at, summary, error, verification_status,
                    artifacts_json, route_json, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    started_at = COALESCE(agent_tasks.started_at, excluded.started_at),
                    completed_at = excluded.completed_at,
                    summary = excluded.summary,
                    error = excluded.error,
                    verification_status = excluded.verification_status,
                    artifacts_json = excluded.artifacts_json,
                    route_json = excluded.route_json,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                self._record_values(record),
            )
            self._conn.commit()
        self._emit("task_ledger_created", record.to_dict())
        return self.get(task_id) or record

    def mark_running(self, task_id: str) -> TaskRecord | None:
        now = time.time()
        return self._update_status(task_id, "running", started_at=now, updated_at=now)

    def mark_terminal(
        self,
        task_id: str,
        *,
        status: str,
        summary: str = "",
        error: str = "",
        verification_status: str = "unknown",
        artifacts: dict[str, Any] | None = None,
    ) -> TaskRecord | None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"terminal status required, got {status!r}")
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE agent_tasks
                SET status = ?,
                    completed_at = ?,
                    summary = ?,
                    error = ?,
                    verification_status = ?,
                    artifacts_json = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    status,
                    now,
                    summary,
                    error,
                    verification_status,
                    json.dumps(dict(artifacts or {}), sort_keys=True),
                    now,
                    task_id,
                ),
            )
            self._conn.commit()
        record = self.get(task_id)
        if record is not None:
            self._emit("task_ledger_terminal", record.to_dict())
        return record

    def mark_stale_running_lost(self, *, older_than_seconds: float = 300.0) -> int:
        cutoff = time.time() - older_than_seconds
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE agent_tasks
                SET status = 'lost',
                    completed_at = ?,
                    error = 'runtime lost authoritative backing state',
                    verification_status = 'failed',
                    updated_at = ?
                WHERE status = 'running'
                  AND updated_at < ?
                """,
                (now, now, cutoff),
            )
            self._conn.commit()
            changed = cur.rowcount
        if changed:
            self._emit("task_ledger_reconciled_lost", {"count": changed, "older_than_seconds": older_than_seconds})
        return int(changed or 0)

    def get(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list(
        self,
        *,
        session_id: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[TaskRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if statuses is not None:
            status_list = list(statuses)
            for status in status_list:
                self._validate_status(status)
            if status_list:
                placeholders = ", ".join("?" for _ in status_list)
                clauses.append(f"status IN ({placeholders})")
                params.extend(status_list)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 100)))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM agent_tasks {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def summary(self, *, session_id: str | None = None) -> dict[str, int]:
        params: list[Any] = []
        where = ""
        if session_id:
            where = "WHERE session_id = ?"
            params.append(session_id)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT status, COUNT(*) AS count FROM agent_tasks {where} GROUP BY status",
                params,
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _update_status(self, task_id: str, status: str, **fields: Any) -> TaskRecord | None:
        self._validate_status(status)
        assignments = ["status = ?"]
        params: list[Any] = [status]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            params.append(value)
        params.append(task_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE agent_tasks SET {', '.join(assignments)} WHERE task_id = ?",
                params,
            )
            self._conn.commit()
        record = self.get(task_id)
        if record is not None:
            self._emit("task_ledger_updated", record.to_dict())
        return record

    def _record_values(self, record: TaskRecord) -> tuple[Any, ...]:
        return (
            record.task_id,
            record.session_id,
            record.channel,
            record.external_session_id,
            record.external_user_id,
            record.objective,
            record.mode,
            record.runtime,
            record.provider,
            record.model,
            record.status,
            record.notify_policy,
            record.created_at,
            record.started_at,
            record.completed_at,
            record.summary,
            record.error,
            record.verification_status,
            json.dumps(record.artifacts, sort_keys=True),
            json.dumps(record.route, sort_keys=True),
            json.dumps(record.metadata, sort_keys=True),
            record.updated_at,
        )

    def _row_to_record(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"]),
            objective=str(row["objective"]),
            runtime=str(row["runtime"]),
            status=str(row["status"]),
            mode=str(row["mode"] or ""),
            provider=_as_optional_str(row["provider"]),
            model=_as_optional_str(row["model"]),
            channel=_as_optional_str(row["channel"]),
            external_session_id=_as_optional_str(row["external_session_id"]),
            external_user_id=_as_optional_str(row["external_user_id"]),
            notify_policy=str(row["notify_policy"] or "done_only"),
            created_at=float(row["created_at"]),
            started_at=_as_optional_float(row["started_at"]),
            completed_at=_as_optional_float(row["completed_at"]),
            summary=str(row["summary"] or ""),
            error=str(row["error"] or ""),
            verification_status=str(row["verification_status"] or "unknown"),
            artifacts=_loads_json(row["artifacts_json"]),
            route=_loads_json(row["route_json"]),
            metadata=_loads_json(row["metadata_json"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid task status: {status}")

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.observe is None:
            return
        task_id = _as_optional_str(payload.get("task_id"))
        self.observe.emit(
            event_type,
            lane="task_ledger",
            job_id=task_id,
            artifact_id=_lifecycle_job_artifact_id(payload),
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


def _lifecycle_job_artifact_id(payload: dict[str, Any]) -> str | None:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    lifecycle = artifacts.get("lifecycle")
    if not isinstance(lifecycle, dict):
        return None
    job = lifecycle.get("job")
    if isinstance(job, dict):
        return _as_optional_str(job.get("artifact_id"))
    artifact_ids = lifecycle.get("artifact_ids")
    if isinstance(artifact_ids, list) and artifact_ids:
        return _as_optional_str(artifact_ids[-1])
    return None
