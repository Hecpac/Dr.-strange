from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from claw_v2 import liveness
from claw_v2.f2_durability_schema import F2_DURABILITY_TABLES
from claw_v2.redaction import redact_sensitive


Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_LABEL = "com.pachano.claw"
DEFAULT_PORT = 8765
DEFAULT_DB_PATH = Path("data/claw.db")
DEFAULT_ACK_PATH = Path("data/diagnostics_acks.json")
HEARTBEAT_STALE_AFTER_S = 180.0
CLOCK_SKEW_TOLERANCE_S = 60.0
AUTONOMY_SKIP_WINDOW_HOURS = 24
AUTONOMY_SCHEDULED_SKIP_ATTENTION_THRESHOLD = 20
AUTONOMY_STALE_RUNNING_JOB_SECONDS = 6 * 60 * 60
AUTONOMY_COMPLETED_UNVERIFIED_DEADLINE_SECONDS = 24 * 60 * 60
AUTONOMY_FAILED_TASK_WARNING_THRESHOLD = 50
ACTIONABLE_OBSERVE_EVENT_TYPES = (
    "scheduled_job_error",
    "daemon_tick_error",
    "llm_circuit_open",
    "llm_circuit_blocked",
    "nlm_research_failed",
    "nlm_research_degraded",
    "firecrawl_paused",
    "auto_research_adapter_error",
    "perf_optimizer_paused",
    "pipeline_poll_degraded",
    "telegram_transport_stop_error",
    "computer_session_failed",
    "computer_browser_use_timeout",
    "computer_screenshot_failed",
    "computer_approval_screenshot_failed",
)
NARRATIVE_EVENT_TYPES = ("llm_decision", "llm_response")
NARRATIVE_ERROR_PATTERNS = (
    "database is locked",
    "runtime_db",
    "runtimedb",
    "sqlite",
    "wal",
)
F2_DIAGNOSTIC_MAX_LIMIT = 100
F2_MANUAL_REVIEW_EFFECT_STATUSES = frozenset(
    {
        "intent_recorded",
        "apply_in_progress",
        "applied",
        "failed",
        "verification_required",
        "blocked_manual_review",
    }
)
F2_VERIFIED_ABSENT_STATUS = "verified_absent"


def collect_f2_recovery_report(
    db_path: str | Path,
    task_id: str | None = None,
    run_id: str | None = None,
    limit: int = 20,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Collect a safe, read-only F2 durability/recovery diagnostic report.

    This function intentionally uses direct SQLite read-only mode instead of
    RuntimeDb/F2DurabilityStore. Constructing those runtime objects can create
    or migrate F2 tables, while this diagnostic surface must never mutate the
    inspected database.
    """
    db = Path(db_path)
    requested_limit = limit
    safe_limit = _f2_diagnostic_limit(limit)
    report = _empty_f2_report(
        db,
        requested_limit=requested_limit,
        limit=safe_limit,
        include_payload=include_payload,
    )
    if not db.exists():
        report["reason"] = "db_missing"
        report["readiness"] = _f2_readiness(
            report["status"],
            report["tables_present"],
            orphaned_count=0,
            manual_review_count=0,
            verified_absent_count=0,
        )
        return report

    try:
        conn = _open_readonly_sqlite(db)
    except sqlite3.Error as exc:
        report.update(
            {
                "status": "error",
                "reason": "db_unreadable",
                "error_type": type(exc).__name__,
            }
        )
        report["readiness"] = _f2_readiness(
            report["status"],
            report["tables_present"],
            orphaned_count=0,
            manual_review_count=0,
            verified_absent_count=0,
        )
        return report

    try:
        tables_present = {table: _table_exists(conn, table) for table in F2_DURABILITY_TABLES}
        report["tables_present"] = tables_present
        present_count = sum(1 for present in tables_present.values() if present)
        if present_count == 0:
            report["reason"] = "f2_tables_absent"
        elif present_count != len(F2_DURABILITY_TABLES):
            report.update({"status": "schema_error", "reason": "f2_schema_partial"})
        else:
            report.update(
                {
                    "status": "ok",
                    "enabled": True,
                    "reason": "f2_tables_present",
                    "counts": _f2_counts(conn, task_id=task_id, run_id=run_id),
                    "recent_records": _f2_recent_records(
                        conn,
                        task_id=task_id,
                        run_id=run_id,
                        limit=safe_limit,
                        include_payload=include_payload,
                    ),
                    "external_effects": _f2_external_effect_diagnostics(
                        conn,
                        task_id=task_id,
                        run_id=run_id,
                        limit=safe_limit,
                    ),
                }
            )
    except sqlite3.Error as exc:
        report.update(
            {
                "status": "error",
                "enabled": False,
                "reason": "f2_query_error",
                "error_type": type(exc).__name__,
            }
        )
    finally:
        conn.close()

    effects = report.get("external_effects") if isinstance(report.get("external_effects"), dict) else {}
    report["readiness"] = _f2_readiness(
        report["status"],
        report["tables_present"],
        orphaned_count=len(effects.get("orphaned") or []),
        manual_review_count=len(effects.get("manual_review_required") or []),
        verified_absent_count=len(effects.get("verified_absent_requires_future_execution") or []),
    )
    return report


def collect_diagnostics(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    port: int = DEFAULT_PORT,
    label: str = DEFAULT_LABEL,
    limit: int = 10,
    runner: Runner = subprocess.run,
    ack_path: Path | str = DEFAULT_ACK_PATH,
) -> dict[str, Any]:
    db = Path(db_path)
    acknowledgements = _load_acknowledgements(Path(ack_path))
    launchd_domain = f"gui/{os.getuid()}/{label}"
    commands = {
        "launchctl_list": _run_command(["launchctl", "list", label], runner=runner),
        "launchctl_print": _run_command(["launchctl", "print", launchd_domain], runner=runner),
        "processes": _run_command(["pgrep", "-fl", "claw_v2.main"], runner=runner),
        "port_listener": _run_command(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], runner=runner
        ),
    }
    database = _database_summary(db, limit=limit, acknowledgements=acknowledgements)
    checks = _checks(commands=commands, database=database, port=port)
    return {
        "label": label,
        "port": port,
        "db_path": str(db),
        "checks": checks,
        "commands": commands,
        "database": database,
        "ack_path": str(ack_path),
    }


def _run_command(args: list[str], *, runner: Runner, timeout: float = 5.0) -> dict[str, Any]:
    try:
        result = runner(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc), "cmd": args}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": f"timed out after {timeout}s",
            "cmd": args,
        }
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": redact_sensitive((result.stdout or "").strip(), limit=4000),
        "stderr": redact_sensitive((result.stderr or "").strip(), limit=4000),
        "cmd": args,
    }


def _database_summary(
    db_path: Path,
    *,
    limit: int,
    acknowledgements: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not db_path.exists():
        return {"present": False, "error": "database not found"}
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as exc:
        return {"present": True, "error": str(exc)}
    try:
        try:
            summary: dict[str, Any] = {"present": True}
            if _table_exists(conn, "observe_stream"):
                heartbeat = _heartbeat_summary(conn)
                summary["heartbeat"] = heartbeat
                current_window = _current_daemon_window(conn, heartbeat=heartbeat)
                latest_error_candidates = _latest_events(
                    conn,
                    event_types=ACTIONABLE_OBSERVE_EVENT_TYPES,
                    limit=limit * 5,
                    hours=24,
                )
                latest_error_candidates = _merge_events(
                    latest_error_candidates,
                    _latest_computer_use_errors(conn, limit=limit * 5, hours=24),
                    limit=limit * 5,
                )
                classified_errors = _classify_error_events(
                    latest_error_candidates,
                    current_window=current_window,
                    limit=limit,
                    now=time.time(),
                )
                actionable_errors, acknowledged_errors = _partition_acknowledged_events(
                    classified_errors["actionable"],
                    acknowledgements or {},
                )
                summary["observe"] = {
                    "recent_events": _recent_events(conn, limit=limit),
                    "event_counts_24h": _event_counts_24h(conn),
                    "current_daemon_window": current_window,
                    "latest_errors": actionable_errors,
                    "stale_historical_errors": classified_errors["stale"],
                    "unknown_relevance_errors": classified_errors["unknown"],
                    "narrative_non_error_matches": _latest_narrative_error_mentions(
                        conn,
                        limit=limit,
                        hours=24,
                    ),
                    "acknowledged_errors": acknowledged_errors,
                }
            else:
                summary["observe"] = {"present": False}
            if _table_exists(conn, "agent_jobs"):
                summary["jobs"] = {
                    "counts": _status_counts(conn, "agent_jobs"),
                    "active": _active_jobs(conn, limit=limit),
                }
            else:
                summary["jobs"] = {"present": False}
            if _table_exists(conn, "agent_tasks"):
                summary["tasks"] = {
                    "counts": _status_counts(conn, "agent_tasks"),
                    "active": _active_tasks(conn, limit=limit),
                }
            else:
                summary["tasks"] = {"present": False}
            if _table_exists(conn, "cron_state"):
                summary["cron"] = _cron_state(conn, limit=limit)
            summary["autonomy"] = _autonomy_summary(conn, limit=limit)
            return summary
        except sqlite3.Error as exc:
            return {"present": True, "error": str(exc)}
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _open_readonly_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _empty_f2_report(
    db_path: Path,
    *,
    requested_limit: int,
    limit: int,
    include_payload: bool,
) -> dict[str, Any]:
    return {
        "status": "disabled",
        "enabled": False,
        "db_path": str(db_path),
        "requested_limit": requested_limit,
        "limit": limit,
        "tables_present": {table: False for table in F2_DURABILITY_TABLES},
        "counts": _empty_f2_counts(),
        "recent_records": _empty_f2_recent_records(),
        "external_effects": {
            "orphaned": [],
            "manual_review_required": [],
            "verified_absent_requires_future_execution": [],
        },
        "readiness": {"status": "warning", "checks": []},
        "payload_policy": {
            "include_payload_requested": bool(include_payload),
            "raw_payloads_included": False,
            "mode": "safe_summaries_only",
        },
        "generated_at": _utc_iso_now(),
    }


def _empty_f2_counts() -> dict[str, Any]:
    return {
        "phase_checkpoints": {"total": 0, "by_status": {}},
        "phase_checkpoint_writes": {"total": 0, "by_write_kind": {}},
        "external_effect_records": {"total": 0, "by_status": {}},
        "phase_recovery_cursors": {"total": 0, "by_cursor_status": {}},
    }


def _empty_f2_recent_records() -> dict[str, list[dict[str, Any]]]:
    return {
        "phase_checkpoints": [],
        "phase_checkpoint_writes": [],
        "external_effect_records": [],
        "phase_recovery_cursors": [],
    }


def _f2_diagnostic_limit(limit: int) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = 20
    return max(1, min(F2_DIAGNOSTIC_MAX_LIMIT, parsed))


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _f2_counts(
    conn: sqlite3.Connection,
    *,
    task_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    return {
        "phase_checkpoints": _f2_grouped_count(
            conn,
            "phase_checkpoints",
            "status",
            task_id=task_id,
            run_id=run_id,
            group_key="by_status",
        ),
        "phase_checkpoint_writes": _f2_grouped_count(
            conn,
            "phase_checkpoint_writes",
            "write_kind",
            task_id=task_id,
            run_id=run_id,
            group_key="by_write_kind",
        ),
        "external_effect_records": _f2_grouped_count(
            conn,
            "external_effect_records",
            "status",
            task_id=task_id,
            run_id=run_id,
            group_key="by_status",
        ),
        "phase_recovery_cursors": _f2_grouped_count(
            conn,
            "phase_recovery_cursors",
            "cursor_status",
            task_id=task_id,
            run_id=run_id,
            group_key="by_cursor_status",
        ),
    }


def _f2_grouped_count(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    *,
    task_id: str | None,
    run_id: str | None,
    group_key: str,
) -> dict[str, Any]:
    where, params = _f2_where(task_id=task_id, run_id=run_id)
    total = conn.execute(f"SELECT COUNT(*) AS count FROM {table}{where}", params).fetchone()
    grouped_rows = conn.execute(
        f"""
        SELECT {column} AS value, COUNT(*) AS count
        FROM {table}
        {where}
        GROUP BY {column}
        ORDER BY {column} ASC
        """,
        params,
    ).fetchall()
    return {
        "total": int(total["count"]),
        group_key: {str(row["value"]): int(row["count"]) for row in grouped_rows},
    }


def _f2_recent_records(
    conn: sqlite3.Connection,
    *,
    task_id: str | None,
    run_id: str | None,
    limit: int,
    include_payload: bool,
) -> dict[str, list[dict[str, Any]]]:
    where, params = _f2_where(task_id=task_id, run_id=run_id)
    checkpoints = conn.execute(
        f"""
        SELECT checkpoint_id, task_id, run_id, job_id, session_id, phase,
               phase_version, status, schema_version, last_write_order,
               payload_json, payload_sha256, orchestration_run_id,
               orchestration_checkpoint_id, created_at
        FROM phase_checkpoints
        {where}
        ORDER BY created_at DESC, phase_version DESC, checkpoint_id ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    writes = conn.execute(
        f"""
        SELECT write_id, task_id, run_id, job_id, phase, write_order, write_kind,
               write_key, schema_version, payload_json, payload_sha256,
               external_effect_id, created_at
        FROM phase_checkpoint_writes
        {where}
        ORDER BY created_at DESC, write_order DESC, write_id ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    effects = conn.execute(
        f"""
        SELECT external_effect_id, idempotency_key, task_id, run_id, job_id,
               phase, effect_kind, content_hash, request_json, request_sha256,
               status, attempt_count, verifier_kind, verification_json,
               result_json, result_sha256, error, schema_version, created_at,
               updated_at
        FROM external_effect_records
        {where}
        ORDER BY updated_at DESC, external_effect_id ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    cursors = conn.execute(
        f"""
        SELECT recovery_cursor_id, task_id, run_id, job_id, session_id, phase,
               cursor_status, last_checkpoint_id, last_write_order,
               external_effect_id, resume_payload_json, schema_version,
               created_at, updated_at
        FROM phase_recovery_cursors
        {where}
        ORDER BY updated_at DESC, recovery_cursor_id ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return {
        "phase_checkpoints": [
            _f2_checkpoint_summary(row, include_payload=include_payload)
            for row in checkpoints
        ],
        "phase_checkpoint_writes": [
            _f2_write_summary(row, include_payload=include_payload) for row in writes
        ],
        "external_effect_records": [
            _f2_effect_summary(row, include_payload=include_payload) for row in effects
        ],
        "phase_recovery_cursors": [
            _f2_cursor_summary(row, include_payload=include_payload) for row in cursors
        ],
    }


def _f2_checkpoint_summary(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
    summary = {
        "checkpoint_id": row["checkpoint_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "phase": row["phase"],
        "phase_version": int(row["phase_version"]),
        "status": row["status"],
        "schema_version": int(row["schema_version"]),
        "last_write_order": int(row["last_write_order"]),
        "payload_sha256": row["payload_sha256"],
        "created_at": row["created_at"],
    }
    if row["job_id"]:
        summary["job_id"] = row["job_id"]
    if row["session_id"]:
        summary["session_id"] = row["session_id"]
    if row["orchestration_run_id"]:
        summary["orchestration_run_id"] = row["orchestration_run_id"]
    if row["orchestration_checkpoint_id"]:
        summary["orchestration_checkpoint_id"] = row["orchestration_checkpoint_id"]
    if include_payload:
        summary["payload_summary"] = _f2_json_summary(
            row["payload_json"],
            sha256=row["payload_sha256"],
        )
    return redact_sensitive(summary, limit=4000)


def _f2_write_summary(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
    summary = {
        "write_id": row["write_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "phase": row["phase"],
        "write_order": int(row["write_order"]),
        "write_kind": row["write_kind"],
        "schema_version": int(row["schema_version"]),
        "payload_sha256": row["payload_sha256"],
        "external_effect_id": row["external_effect_id"],
        "created_at": row["created_at"],
    }
    if row["job_id"]:
        summary["job_id"] = row["job_id"]
    if row["write_key"]:
        summary["write_key"] = str(redact_sensitive(row["write_key"], limit=200))
    if include_payload:
        summary["payload_summary"] = _f2_json_summary(
            row["payload_json"],
            sha256=row["payload_sha256"],
        )
    return redact_sensitive(summary, limit=4000)


def _f2_effect_summary(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
    summary = {
        "external_effect_id": row["external_effect_id"],
        "idempotency_key": row["idempotency_key"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "phase": row["phase"],
        "effect_kind": row["effect_kind"],
        "content_hash": row["content_hash"],
        "request_sha256": row["request_sha256"],
        "status": row["status"],
        "attempt_count": int(row["attempt_count"]),
        "verifier_kind": row["verifier_kind"],
        "result_sha256": row["result_sha256"],
        "error_present": bool(row["error"]),
        "schema_version": int(row["schema_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["job_id"]:
        summary["job_id"] = row["job_id"]
    if include_payload:
        summary["request_summary"] = _f2_json_summary(
            row["request_json"],
            sha256=row["request_sha256"],
        )
        summary["verification_summary"] = _f2_json_summary(row["verification_json"])
        summary["result_summary"] = _f2_json_summary(
            row["result_json"],
            sha256=row["result_sha256"],
        )
        if row["error"]:
            summary["error_summary"] = _safe_f2_text_summary(row["error"])
    return redact_sensitive(summary, limit=4000)


def _f2_cursor_summary(row: sqlite3.Row, *, include_payload: bool) -> dict[str, Any]:
    summary = {
        "recovery_cursor_id": row["recovery_cursor_id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "phase": row["phase"],
        "cursor_status": row["cursor_status"],
        "last_checkpoint_id": row["last_checkpoint_id"],
        "last_write_order": int(row["last_write_order"]),
        "external_effect_id": row["external_effect_id"],
        "schema_version": int(row["schema_version"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["job_id"]:
        summary["job_id"] = row["job_id"]
    if row["session_id"]:
        summary["session_id"] = row["session_id"]
    if include_payload:
        summary["resume_payload_summary"] = _f2_json_summary(row["resume_payload_json"])
    return redact_sensitive(summary, limit=4000)


def _f2_json_summary(raw: Any, *, sha256: str | None = None) -> dict[str, Any]:
    if raw is None:
        return {"present": False}
    text = str(raw)
    summary: dict[str, Any] = {
        "present": True,
        "bytes": len(text.encode("utf-8", errors="replace")),
        "raw_included": False,
        "redacted": True,
    }
    if sha256:
        summary["sha256"] = sha256
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        summary["json_type"] = "invalid"
        return summary
    if isinstance(value, dict):
        summary["json_type"] = "object"
        summary["top_level_key_count"] = len(value)
    elif isinstance(value, list):
        summary["json_type"] = "array"
        summary["item_count"] = len(value)
    else:
        summary["json_type"] = type(value).__name__
    return summary


def _safe_f2_text_summary(value: Any) -> dict[str, Any]:
    text = str(redact_sensitive(str(value), limit=200))
    return {
        "present": bool(text),
        "text": text,
        "truncated": len(str(value)) > 200,
    }


def _f2_external_effect_diagnostics(
    conn: sqlite3.Connection,
    *,
    task_id: str | None,
    run_id: str | None,
    limit: int,
) -> dict[str, list[dict[str, Any]]]:
    where, params = _f2_where(task_id=task_id, run_id=run_id, prefix="e.")
    linked_where = f"{where} AND w.write_id IS NULL" if where else " WHERE w.write_id IS NULL"
    orphaned = conn.execute(
        f"""
        SELECT e.external_effect_id, e.idempotency_key, e.task_id, e.run_id, e.phase,
               e.effect_kind, e.status, e.attempt_count, e.request_sha256,
               e.result_sha256, e.updated_at
        FROM external_effect_records e
        LEFT JOIN phase_checkpoint_writes w
          ON w.external_effect_id = e.external_effect_id
        {linked_where}
        ORDER BY e.updated_at DESC, e.external_effect_id ASC
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    manual_statuses = tuple(sorted(F2_MANUAL_REVIEW_EFFECT_STATUSES))
    placeholders = ",".join("?" for _ in manual_statuses)
    manual_where = (
        f"{where} AND e.status IN ({placeholders})"
        if where
        else f" WHERE e.status IN ({placeholders})"
    )
    manual = conn.execute(
        f"""
        SELECT e.external_effect_id, e.idempotency_key, e.task_id, e.run_id, e.phase,
               e.effect_kind, e.status, e.attempt_count, e.request_sha256,
               e.result_sha256, e.updated_at
        FROM external_effect_records e
        {manual_where}
        ORDER BY e.updated_at DESC, e.external_effect_id ASC
        LIMIT ?
        """,
        [*params, *manual_statuses, limit],
    ).fetchall()
    absent_where = (
        f"{where} AND e.status = ?"
        if where
        else " WHERE e.status = ?"
    )
    verified_absent = conn.execute(
        f"""
        SELECT e.external_effect_id, e.idempotency_key, e.task_id, e.run_id, e.phase,
               e.effect_kind, e.status, e.attempt_count, e.request_sha256,
               e.result_sha256, e.updated_at
        FROM external_effect_records e
        {absent_where}
        ORDER BY e.updated_at DESC, e.external_effect_id ASC
        LIMIT ?
        """,
        [*params, F2_VERIFIED_ABSENT_STATUS, limit],
    ).fetchall()
    return {
        "orphaned": [
            _f2_effect_issue(row, reason="orphaned_external_effect")
            for row in orphaned
        ],
        "manual_review_required": [
            _f2_effect_issue(row, reason="unsafe_external_effect_status")
            for row in manual
        ],
        "verified_absent_requires_future_execution": [
            _f2_effect_issue(
                row,
                reason="verified_absent_future_execution_required",
            )
            for row in verified_absent
        ],
    }


def _f2_effect_issue(row: sqlite3.Row, *, reason: str) -> dict[str, Any]:
    return redact_sensitive(
        {
            "external_effect_id": row["external_effect_id"],
            "idempotency_key": row["idempotency_key"],
            "task_id": row["task_id"],
            "run_id": row["run_id"],
            "phase": row["phase"],
            "effect_kind": row["effect_kind"],
            "status": row["status"],
            "attempt_count": int(row["attempt_count"]),
            "request_sha256": row["request_sha256"],
            "result_sha256": row["result_sha256"],
            "updated_at": row["updated_at"],
            "reason": reason,
        },
        limit=4000,
    )


def _f2_readiness(
    report_status: str,
    tables_present: dict[str, bool],
    *,
    orphaned_count: int,
    manual_review_count: int,
    verified_absent_count: int,
) -> dict[str, Any]:
    present_count = sum(1 for present in tables_present.values() if present)
    all_present = present_count == len(F2_DURABILITY_TABLES)
    none_present = present_count == 0
    checks = [
        {
            "code": "f2_flags_disabled_by_default",
            "status": "pass",
            "detail": "F2 diagnostics do not enable durability flags.",
        },
        {
            "code": "f2_tables_state",
            "status": "pass" if all_present or none_present else "fail",
            "present_count": present_count,
            "expected_count": len(F2_DURABILITY_TABLES),
        },
        {
            "code": "diagnostics_read_only",
            "status": "pass",
            "detail": "SQLite is opened with mode=ro and no RuntimeDb/F2 store is constructed.",
        },
        {
            "code": "no_diagnostics_migration_or_write_path",
            "status": "pass",
            "detail": "Report generation does not call F2 schema/store migration helpers.",
        },
        {
            "code": "no_replay_or_execution_behavior",
            "status": "pass",
            "detail": "Report generation summarizes records only and does not execute recovery.",
        },
        {
            "code": "orphaned_external_effect_count",
            "status": "warning" if orphaned_count else "pass",
            "count": orphaned_count,
        },
        {
            "code": "manual_review_blockers_count",
            "status": "warning" if manual_review_count else "pass",
            "count": manual_review_count,
        },
        {
            "code": "verified_absent_future_execution_count",
            "status": "warning" if verified_absent_count else "pass",
            "count": verified_absent_count,
        },
    ]
    if report_status in {"schema_error", "error"}:
        status = "fail"
    elif any(check["status"] == "fail" for check in checks):
        status = "fail"
    elif any(check["status"] == "warning" for check in checks):
        status = "warning"
    else:
        status = "pass"
    return {"status": status, "checks": checks}


def _f2_where(
    *,
    task_id: str | None,
    run_id: str | None,
    prefix: str = "",
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if task_id is not None:
        clauses.append(f"{prefix}task_id = ?")
        params.append(task_id)
    if run_id is not None:
        clauses.append(f"{prefix}run_id = ?")
        params.append(run_id)
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def _recent_events(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_event_row(row) for row in rows]


def _latest_events(
    conn: sqlite3.Connection,
    *,
    event_types: tuple[str, ...],
    limit: int,
    hours: int | None = None,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in event_types)
    params: list[Any] = [*event_types]
    age_filter = ""
    if hours is not None:
        age_filter = "AND timestamp >= datetime('now', ?)"
        params.append(f"-{hours} hours")
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        WHERE event_type IN ({placeholders})
          {age_filter}
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_event_row(row) for row in rows]


def _latest_computer_use_errors(
    conn: sqlite3.Connection,
    *,
    limit: int,
    hours: int | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = []
    age_filter = ""
    if hours is not None:
        age_filter = "AND timestamp >= datetime('now', ?)"
        params.append(f"-{hours} hours")
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        WHERE event_type = 'error'
          AND json_extract(payload, '$.source') = 'computer_use'
          {age_filter}
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_event_row(row) for row in rows]


def _current_daemon_window(
    conn: sqlite3.Connection,
    *,
    heartbeat: dict[str, Any],
) -> dict[str, Any]:
    pid = heartbeat.get("pid")
    boot_id = heartbeat.get("boot_id")
    if pid is None and not boot_id:
        return {
            "known": False,
            "source": heartbeat.get("source"),
            "missing_reason": "missing_current_daemon_identity",
            "pid": pid,
            "boot_id": boot_id,
        }

    startup = _find_current_startup_event(conn, pid=pid, boot_id=boot_id)
    if startup is None:
        return {
            "known": False,
            "source": heartbeat.get("source"),
            "missing_reason": "missing_current_startup_event",
            "pid": pid,
            "boot_id": boot_id,
        }

    payload = startup["payload"] if isinstance(startup.get("payload"), dict) else {}
    boot_timestamp = startup["timestamp"]
    boot_epoch_s = _parse_observe_timestamp(boot_timestamp)
    if boot_epoch_s is None:
        return {
            "known": False,
            "source": heartbeat.get("source"),
            "missing_reason": "invalid_current_boot_timestamp",
            "startup_event_id": int(startup["id"]),
            "boot_timestamp": boot_timestamp,
            "pid": pid,
            "boot_id": boot_id,
        }
    if boot_epoch_s > time.time() + CLOCK_SKEW_TOLERANCE_S:
        return {
            "known": False,
            "source": heartbeat.get("source"),
            "missing_reason": "future_current_boot_timestamp",
            "startup_event_id": int(startup["id"]),
            "boot_timestamp": boot_timestamp,
            "boot_timestamp_epoch_s": boot_epoch_s,
            "pid": pid,
            "boot_id": boot_id,
        }
    code_version = payload.get("code_version") or payload.get("startup_marker")
    return {
        "known": True,
        "source": heartbeat.get("source"),
        "startup_event_id": int(startup["id"]),
        "boot_timestamp": boot_timestamp,
        "boot_timestamp_epoch_s": boot_epoch_s,
        "startup_event_type": startup["event_type"],
        "pid": pid,
        "boot_id": boot_id,
        "code_version": code_version,
    }


def _find_current_startup_event(
    conn: sqlite3.Connection,
    *,
    pid: Any,
    boot_id: Any,
) -> dict[str, Any] | None:
    for event_type in ("agent_startup_context", "startup_healthcheck_ok"):
        row = _find_startup_event(conn, event_type=event_type, pid=pid, boot_id=boot_id)
        if row is not None:
            return _event_row(row)
    return None


def _find_startup_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    pid: Any,
    boot_id: Any,
) -> sqlite3.Row | None:
    identity_filters: list[str] = []
    params: list[Any] = [event_type]
    if pid is not None:
        identity_filters.append("CAST(json_extract(payload, '$.pid') AS TEXT) = ?")
        params.append(str(pid))
    if boot_id:
        identity_filters.append(
            "(json_extract(payload, '$.boot_id') IS NULL "
            "OR CAST(json_extract(payload, '$.boot_id') AS TEXT) = ?)"
        )
        params.append(str(boot_id))
    if not identity_filters:
        return None
    where = " AND ".join(["event_type = ?", *identity_filters])
    return conn.execute(
        f"""
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        WHERE {where}
        ORDER BY id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


def _classify_error_events(
    events: list[dict[str, Any]],
    *,
    current_window: dict[str, Any],
    limit: int,
    now: float | None = None,
) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {
        "actionable": [],
        "stale": [],
        "unknown": [],
    }
    if not events:
        return buckets
    if not current_window.get("known"):
        buckets["unknown"] = events[:limit]
        return buckets

    boot_epoch_s = current_window.get("boot_timestamp_epoch_s")
    if not isinstance(boot_epoch_s, (int, float)):
        buckets["unknown"] = events[:limit]
        return buckets
    startup_event_id = int(current_window.get("startup_event_id") or 0)
    current = time.time() if now is None else now
    for event in events:
        event_epoch_s = _parse_observe_timestamp(event.get("timestamp"))
        if event_epoch_s is None or event_epoch_s > current + CLOCK_SKEW_TOLERANCE_S:
            buckets["unknown"].append(event)
        elif _event_precedes_current_boot(
            event,
            boot_epoch_s=float(boot_epoch_s),
            event_epoch_s=event_epoch_s,
            startup_event_id=startup_event_id,
        ) or _event_identity_mismatch(event, current_window=current_window):
            buckets["stale"].append(event)
        else:
            buckets["actionable"].append(event)
    return {name: values[:limit] for name, values in buckets.items()}


def _event_precedes_current_boot(
    event: dict[str, Any],
    *,
    boot_epoch_s: float,
    event_epoch_s: float,
    startup_event_id: int,
) -> bool:
    try:
        event_id = int(event["id"])
    except (KeyError, TypeError, ValueError):
        event_id = 0
    return event_id <= startup_event_id or event_epoch_s < boot_epoch_s


def _parse_observe_timestamp(raw: Any) -> float | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.timestamp()


def _event_identity_mismatch(
    event: dict[str, Any],
    *,
    current_window: dict[str, Any],
) -> bool:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for key in ("pid", "boot_id", "code_version"):
        event_value = payload.get(key)
        window_value = current_window.get(key)
        if event_value is None or window_value is None:
            continue
        if str(event_value) != str(window_value):
            return True
    return False


def _latest_narrative_error_mentions(
    conn: sqlite3.Connection,
    *,
    limit: int,
    hours: int | None = None,
) -> list[dict[str, Any]]:
    params: list[Any] = [*NARRATIVE_EVENT_TYPES]
    age_filter = ""
    if hours is not None:
        age_filter = "AND timestamp >= datetime('now', ?)"
        params.append(f"-{hours} hours")
    pattern_filter = " OR ".join("lower(payload) LIKE ?" for _ in NARRATIVE_ERROR_PATTERNS)
    params.extend(f"%{pattern}%" for pattern in NARRATIVE_ERROR_PATTERNS)
    params.append(limit)
    placeholders = ",".join("?" for _ in NARRATIVE_EVENT_TYPES)
    rows = conn.execute(
        f"""
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        WHERE event_type IN ({placeholders})
          {age_filter}
          AND ({pattern_filter})
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [_event_row(row) for row in rows]


def _merge_events(*event_groups: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for group in event_groups:
        for event in group:
            by_id[int(event["id"])] = event
    return [by_id[event_id] for event_id in sorted(by_id, reverse=True)[:limit]]


def _main_db_file(conn: sqlite3.Connection) -> Path | None:
    """Resolve the on-disk path of the connection's main database, or None."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # Rows are (seq, name, file). Handle both sqlite3.Row and tuple.
        try:
            name = row["name"]
            file = row["file"]
        except (IndexError, TypeError, KeyError):
            try:
                name = row[1]
                file = row[2]
            except (IndexError, TypeError):
                continue
        if name == "main" and file:
            return Path(str(file))
    return None


def _heartbeat_summary(conn: sqlite3.Connection, *, now: float | None = None) -> dict[str, Any]:
    """Summarize daemon liveness.

    F0.3: the atomic JSON liveness sink (written by the scheduled lifecycle
    heartbeat) is the single source of truth when present. It carries the
    authoritative ``web_transport_serving`` together with its ``ts`` — never
    combine ts-from-sink with web-from-observe. Only when the sink is
    missing/unreadable do we fall back to the legacy observe_stream query.
    """
    db_file = _main_db_file(conn)
    if db_file is not None:
        record = liveness.read_liveness(liveness.liveness_sink_path(db_file.parent))
        if record is not None:
            raw_ts = record.get("ts")
            age_s: float | None = None
            if isinstance(raw_ts, (int, float)):
                current = time.time() if now is None else now
                age_s = max(0.0, float(current) - float(raw_ts))
            return {
                "present": True,
                "ts": raw_ts,
                "age_s": age_s,
                "web_transport_serving": record.get("web_transport_serving"),
                "pid": record.get("pid"),
                "boot_id": record.get("boot_id"),
                "source": "liveness_sink",
            }

    row = conn.execute(
        """
        SELECT id, timestamp, payload
        FROM observe_stream
        WHERE event_type = 'daemon_heartbeat'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {"present": False}
    payload = _loads_json(row["payload"])
    raw_ts = payload.get("ts") if isinstance(payload, dict) else None
    age_s = None
    if isinstance(raw_ts, (int, float)):
        current = time.time() if now is None else now
        age_s = max(0.0, float(current) - float(raw_ts))
    web_serving = payload.get("web_transport_serving") if isinstance(payload, dict) else None
    return {
        "present": True,
        "id": int(row["id"]),
        "timestamp": row["timestamp"],
        "ts": raw_ts,
        "age_s": age_s,
        "web_transport_serving": web_serving,
        "pid": payload.get("pid") if isinstance(payload, dict) else None,
        "boot_id": payload.get("boot_id") if isinstance(payload, dict) else None,
        "source": "observe_stream",
    }


def _event_counts_24h(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT event_type, COUNT(*) AS count
        FROM observe_stream
        WHERE timestamp >= datetime('now', '-24 hours')
        GROUP BY event_type
        ORDER BY count DESC, event_type ASC
        LIMIT 20
        """
    ).fetchall()
    return {str(row["event_type"]): int(row["count"]) for row in rows}


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "timestamp": row["timestamp"],
        "event_type": row["event_type"],
        "lane": row["lane"],
        "provider": row["provider"],
        "model": row["model"],
        "trace_id": row["trace_id"],
        "job_id": row["job_id"],
        "artifact_id": row["artifact_id"],
        "payload": redact_sensitive(_loads_json(row["payload"]), limit=4000),
    }


def _status_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS count FROM {table} GROUP BY status ORDER BY status ASC"
    ).fetchall()
    return {str(row["status"]): int(row["count"]) for row in rows}


def _active_jobs(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT job_id, kind, status, attempts, max_attempts, worker_id, updated_at, error
        FROM agent_jobs
        WHERE status IN ('queued', 'running', 'waiting_approval', 'retrying')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_redacted_row_dict(row) for row in rows]


def _active_tasks(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT task_id, session_id, objective, runtime, provider, model, status, verification_status, updated_at, error
        FROM agent_tasks
        WHERE status IN ('queued', 'running')
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_redacted_row_dict(row) for row in rows]


def _cron_state(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT job_name, last_run_at, runs
        FROM cron_state
        ORDER BY last_run_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_redacted_row_dict(row) for row in rows]


def _autonomy_summary(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    now = time.time()
    summary: dict[str, Any] = {
        "status": "healthy",
        "scheduled_job_skipped_24h": 0,
        "autonomous_maintenance_disabled_skips_24h": 0,
        "top_scheduled_job_skips_24h": [],
        "stale_running_job_threshold_s": AUTONOMY_STALE_RUNNING_JOB_SECONDS,
        "stale_running_jobs": [],
        "stale_running_jobs_count": 0,
        "completed_unverified_backlog": 0,
        "completed_unverified_overdue": 0,
        "completed_unverified_deadline_s": AUTONOMY_COMPLETED_UNVERIFIED_DEADLINE_SECONDS,
        "lost_tasks": 0,
        "failed_tasks": 0,
        "objective_blockers": [],
        "objective_warnings": [],
    }

    if _table_exists(conn, "observe_stream"):
        skip_summary = _scheduled_skip_summary(conn, limit=limit)
        summary.update(skip_summary)

    if _table_exists(conn, "agent_jobs"):
        stale_jobs = _stale_running_jobs(
            conn,
            now=now,
            stale_after_seconds=AUTONOMY_STALE_RUNNING_JOB_SECONDS,
            limit=limit,
        )
        summary["stale_running_jobs"] = stale_jobs
        summary["stale_running_jobs_count"] = len(stale_jobs)

    if _table_exists(conn, "agent_tasks"):
        task_summary = _autonomy_task_summary(conn, now=now)
        summary.update(task_summary)

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    disabled_skips = int(summary.get("autonomous_maintenance_disabled_skips_24h") or 0)
    total_skips = int(summary.get("scheduled_job_skipped_24h") or 0)
    stale_running = int(summary.get("stale_running_jobs_count") or 0)
    completed_unverified_overdue = int(summary.get("completed_unverified_overdue") or 0)
    lost_tasks = int(summary.get("lost_tasks") or 0)
    failed_tasks = int(summary.get("failed_tasks") or 0)

    if disabled_skips:
        blockers.append(
            {
                "code": "autonomous_maintenance_disabled",
                "category": "AUTONOMY_BLOCKER",
                "count": disabled_skips,
                "message": (
                    "Scheduled autonomous maintenance is disabled by runtime configuration."
                ),
                "operational_change_required": (
                    "Enable CLAW_AUTONOMOUS_MAINTENANCE/CLAW_AUTONOMOUS_MAINTENANCE_ENABLED "
                    "in the daemon environment, then perform an authorized restart."
                ),
            }
        )
    elif total_skips >= AUTONOMY_SCHEDULED_SKIP_ATTENTION_THRESHOLD:
        blockers.append(
            {
                "code": "scheduled_job_skipped_excessive",
                "category": "AUTONOMY_BLOCKER",
                "count": total_skips,
                "threshold": AUTONOMY_SCHEDULED_SKIP_ATTENTION_THRESHOLD,
                "message": "Scheduled job skips exceed the autonomy attention threshold.",
            }
        )

    if stale_running:
        blockers.append(
            {
                "code": "stale_running_jobs",
                "category": "STATE_BLOCKER",
                "count": stale_running,
                "threshold_s": AUTONOMY_STALE_RUNNING_JOB_SECONDS,
                "message": "Running background jobs are older than the stale threshold.",
            }
        )

    if completed_unverified_overdue:
        blockers.append(
            {
                "code": "completed_unverified_overdue",
                "category": "STATE_BLOCKER",
                "count": completed_unverified_overdue,
                "threshold_s": AUTONOMY_COMPLETED_UNVERIFIED_DEADLINE_SECONDS,
                "message": "Completed-unverified task backlog is overdue.",
            }
        )

    if lost_tasks:
        warnings.append({"code": "lost_task_backlog", "count": lost_tasks})
    if failed_tasks >= AUTONOMY_FAILED_TASK_WARNING_THRESHOLD:
        warnings.append(
            {
                "code": "failed_task_backlog",
                "count": failed_tasks,
                "threshold": AUTONOMY_FAILED_TASK_WARNING_THRESHOLD,
            }
        )

    summary["objective_blockers"] = blockers
    summary["objective_warnings"] = warnings
    summary["status"] = "attention" if blockers else "healthy"
    return summary


def _scheduled_skip_summary(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT payload, COUNT(*) AS count
        FROM observe_stream
        WHERE event_type = 'scheduled_job_skipped'
          AND timestamp >= datetime('now', ?)
        GROUP BY payload
        ORDER BY count DESC
        """,
        (f"-{AUTONOMY_SKIP_WINDOW_HOURS} hours",),
    ).fetchall()
    total = 0
    disabled = 0
    grouped: dict[tuple[str, str], int] = {}
    for row in rows:
        count = int(row["count"])
        payload = _loads_json(row["payload"])
        if not isinstance(payload, dict):
            payload = {}
        job = str(payload.get("job") or "unknown")
        reason = str(payload.get("reason") or "unknown")
        total += count
        if reason == "autonomous_maintenance_disabled":
            disabled += count
        grouped[(job, reason)] = grouped.get((job, reason), 0) + count
    top = [
        {"job": job, "reason": reason, "count": count}
        for (job, reason), count in sorted(grouped.items(), key=lambda item: item[1], reverse=True)[
            : max(1, int(limit))
        ]
    ]
    return {
        "scheduled_job_skipped_24h": total,
        "autonomous_maintenance_disabled_skips_24h": disabled,
        "top_scheduled_job_skips_24h": top,
    }


def _stale_running_jobs(
    conn: sqlite3.Connection,
    *,
    now: float,
    stale_after_seconds: float,
    limit: int,
) -> list[dict[str, Any]]:
    cutoff = now - max(0.001, float(stale_after_seconds))
    rows = conn.execute(
        """
        SELECT job_id, kind, status, attempts, max_attempts, worker_id,
               created_at, started_at, updated_at, error
        FROM agent_jobs
        WHERE status = 'running'
          AND COALESCE(updated_at, started_at, created_at) <= ?
        ORDER BY COALESCE(updated_at, started_at, created_at) ASC
        LIMIT ?
        """,
        (cutoff, max(1, int(limit))),
    ).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        data = _redacted_row_dict(row)
        reference = _first_not_none(
            data.get("updated_at"),
            data.get("started_at"),
            data.get("created_at"),
        )
        if isinstance(reference, (int, float)):
            data["age_seconds"] = max(0.0, now - float(reference))
        jobs.append(data)
    return jobs


def _autonomy_task_summary(conn: sqlite3.Connection, *, now: float) -> dict[str, int]:
    overdue_before = now - AUTONOMY_COMPLETED_UNVERIFIED_DEADLINE_SECONDS
    completed_unverified = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM agent_tasks
        WHERE status = 'completed_unverified'
          AND verification_status IN ('needs_verification', 'needs_verify')
        """
    ).fetchone()
    completed_unverified_overdue = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM agent_tasks
        WHERE status = 'completed_unverified'
          AND verification_status IN ('needs_verification', 'needs_verify')
          AND COALESCE(completed_at, updated_at, created_at, 0) <= ?
        """,
        (overdue_before,),
    ).fetchone()
    lost = conn.execute(
        "SELECT COUNT(*) AS count FROM agent_tasks WHERE status = 'lost'"
    ).fetchone()
    failed = conn.execute(
        "SELECT COUNT(*) AS count FROM agent_tasks WHERE status = 'failed'"
    ).fetchone()
    return {
        "completed_unverified_backlog": int(completed_unverified["count"]),
        "completed_unverified_overdue": int(completed_unverified_overdue["count"]),
        "lost_tasks": int(lost["count"]),
        "failed_tasks": int(failed["count"]),
    }


def _redacted_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return redact_sensitive(dict(row), limit=4000)


def _loads_json(raw: Any) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return {"raw": str(raw)[:1000]}


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _load_acknowledgements(path: Path, *, now: float | None = None) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    current = time.time() if now is None else now
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = data.get("acks") if isinstance(data, dict) else []
    active: dict[int, dict[str, Any]] = {}
    if not isinstance(entries, list):
        return active
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            event_id = int(entry.get("event_id"))
            expires_at = float(entry.get("expires_at") or 0)
        except (TypeError, ValueError):
            continue
        if expires_at > current:
            active[event_id] = entry
    return active


def _partition_acknowledged_events(
    events: list[dict[str, Any]],
    acknowledgements: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    actionable: list[dict[str, Any]] = []
    acknowledged: list[dict[str, Any]] = []
    for event in events:
        event_id = int(event["id"])
        ack = acknowledgements.get(event_id)
        if ack is None:
            actionable.append(event)
            continue
        acknowledged_event = dict(event)
        acknowledged_event["acknowledgement"] = {
            "reason": ack.get("reason", ""),
            "expires_at": ack.get("expires_at"),
            "created_at": ack.get("created_at"),
        }
        acknowledged.append(acknowledged_event)
    return actionable, acknowledged


@contextlib.contextmanager
def _ack_file_lock(path: Path) -> Iterator[None]:
    """Serialize concurrent writers using a sidecar lock file.

    Sidecar avoids inode-swap surprises when the JSON itself is replaced
    atomically inside the locked region.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def acknowledge_events(
    event_ids: list[int],
    *,
    ack_path: Path | str = DEFAULT_ACK_PATH,
    hours: float = 24.0,
    reason: str = "",
    now: float | None = None,
) -> list[int]:
    if not event_ids:
        return []
    path = Path(ack_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = time.time() if now is None else now
    expires_at = current + max(0.1, float(hours)) * 3600
    with _ack_file_lock(path):
        active = _load_acknowledgements(path, now=current)
        for event_id in event_ids:
            active[int(event_id)] = {
                "event_id": int(event_id),
                "reason": reason,
                "created_at": current,
                "expires_at": expires_at,
            }
        _atomic_write_json(path, {"acks": list(active.values())})
    return [int(event_id) for event_id in event_ids]


def _checks(
    *, commands: dict[str, dict[str, Any]], database: dict[str, Any], port: int
) -> dict[str, Any]:
    launchd_ok = bool(commands["launchctl_list"].get("ok") or commands["launchctl_print"].get("ok"))
    port_ok = f":{port}" in str(commands["port_listener"].get("stdout") or "")
    process_probe = commands["processes"]
    process_probe_stderr = str(process_probe.get("stderr") or "")
    process_probe_unavailable = not process_probe.get("ok") and (
        "Cannot get process list" in process_probe_stderr
        or "sysmond service not found" in process_probe_stderr
    )
    process_ok = bool(process_probe.get("stdout")) or (
        process_probe_unavailable and launchd_ok and port_ok
    )
    db_ok = bool(database.get("present")) and not database.get("error")
    active_jobs = len((database.get("jobs") or {}).get("active") or [])
    active_tasks = len((database.get("tasks") or {}).get("active") or [])
    observe = database.get("observe") or {}
    latest_errors = len(observe.get("latest_errors") or [])
    stale_errors = len(observe.get("stale_historical_errors") or [])
    unknown_errors = len(observe.get("unknown_relevance_errors") or [])
    narrative_matches = len(observe.get("narrative_non_error_matches") or [])
    acknowledged_errors = len(observe.get("acknowledged_errors") or [])
    autonomy = database.get("autonomy") or {}
    autonomy_blockers = list(autonomy.get("objective_blockers") or [])
    current_window = observe.get("current_daemon_window") or {}
    heartbeat = database.get("heartbeat") or {}
    heartbeat_age = heartbeat.get("age_s")
    heartbeat_present = bool(heartbeat.get("present"))
    heartbeat_stale = (
        heartbeat_present
        and isinstance(heartbeat_age, (int, float))
        and heartbeat_age > HEARTBEAT_STALE_AFTER_S
    )
    web_serving_known = heartbeat.get("web_transport_serving")
    web_thread_dead = web_serving_known is False
    fresh_heartbeat = heartbeat_present and not heartbeat_stale
    transient_port_probe_failure = (
        not port_ok and process_ok and fresh_heartbeat and not web_thread_dead
    )
    status = (
        "healthy"
        if process_ok
        and port_ok
        and db_ok
        and launchd_ok
        and latest_errors == 0
        and unknown_errors == 0
        and not autonomy_blockers
        and not heartbeat_stale
        and not web_thread_dead
        else "attention"
    )
    if (
        not process_ok
        or not db_ok
        or heartbeat_stale
        or web_thread_dead
        or (not port_ok and not transient_port_probe_failure)
    ):
        status = "critical"
    return {
        "status": status,
        "launchd_loaded": launchd_ok,
        "process_running": process_ok,
        "port_listening": port_ok,
        "database_readable": db_ok,
        "active_jobs": active_jobs,
        "active_tasks": active_tasks,
        "recent_error_events": latest_errors,
        "stale_error_events": stale_errors,
        "unknown_relevance_error_events": unknown_errors,
        "narrative_non_error_matches": narrative_matches,
        "acknowledged_error_events": acknowledged_errors,
        "autonomy_objective_status": autonomy.get("status", "unknown"),
        "autonomy_objective_blockers": autonomy_blockers,
        "autonomous_maintenance_disabled": bool(
            autonomy.get("autonomous_maintenance_disabled_skips_24h")
        ),
        "scheduled_job_skipped_24h": int(autonomy.get("scheduled_job_skipped_24h") or 0),
        "autonomous_maintenance_disabled_skips_24h": int(
            autonomy.get("autonomous_maintenance_disabled_skips_24h") or 0
        ),
        "stale_running_jobs": int(autonomy.get("stale_running_jobs_count") or 0),
        "completed_unverified_backlog": int(
            autonomy.get("completed_unverified_backlog") or 0
        ),
        "completed_unverified_overdue": int(autonomy.get("completed_unverified_overdue") or 0),
        "lost_tasks": int(autonomy.get("lost_tasks") or 0),
        "failed_tasks": int(autonomy.get("failed_tasks") or 0),
        "current_daemon_window_known": bool(current_window.get("known")),
        "current_daemon_window_missing_reason": current_window.get("missing_reason"),
        "heartbeat_present": heartbeat_present,
        "heartbeat_age_s": heartbeat_age,
        "heartbeat_stale": heartbeat_stale,
        "web_transport_serving": web_serving_known,
    }


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def format_text(report: dict[str, Any]) -> str:
    checks = report["checks"]
    lines = [
        f"Dr. Strange diagnostics: {checks['status']}",
        f"label={report['label']} port={report['port']} db={report['db_path']}",
        "",
        "Checks:",
    ]
    for key, value in checks.items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    lines.append("Commands:")
    for name, result in report["commands"].items():
        lines.append(f"- {name}: rc={result['returncode']} ok={result['ok']}")
        if result.get("stdout"):
            lines.append(f"  stdout: {result['stdout'].splitlines()[0][:160]}")
        if result.get("stderr"):
            lines.append(f"  stderr: {result['stderr'].splitlines()[0][:160]}")
    database = report.get("database") or {}
    heartbeat = database.get("heartbeat") or {}
    if heartbeat.get("present"):
        age = heartbeat.get("age_s")
        age_text = f"{age:.0f}s" if isinstance(age, (int, float)) else "unknown"
        lines.append("")
        lines.append(
            f"Heartbeat: age={age_text} web_transport_serving={heartbeat.get('web_transport_serving')}"
        )
    observe = database.get("observe") or {}
    current_window = observe.get("current_daemon_window") or {}
    if current_window:
        lines.append("")
        lines.append(
            "Current daemon window: "
            f"known={current_window.get('known')} "
            f"pid={current_window.get('pid')} "
            f"boot_id={current_window.get('boot_id')} "
            f"boot_timestamp={current_window.get('boot_timestamp')} "
            f"missing_reason={current_window.get('missing_reason')}"
        )
    if observe.get("latest_errors"):
        lines.append("")
        lines.append("Recent actionable events:")
        for event in observe["latest_errors"][:5]:
            lines.append(f"- {event['timestamp']} {event['event_type']} {event['payload']}")
    if observe.get("stale_historical_errors"):
        lines.append("")
        lines.append("Stale historical events:")
        for event in observe["stale_historical_errors"][:5]:
            lines.append(f"- {event['timestamp']} {event['event_type']} {event['payload']}")
    if observe.get("unknown_relevance_errors"):
        lines.append("")
        lines.append("Unknown-relevance events:")
        for event in observe["unknown_relevance_errors"][:5]:
            lines.append(f"- {event['timestamp']} {event['event_type']} {event['payload']}")
    if observe.get("narrative_non_error_matches"):
        lines.append("")
        lines.append("Narrative non-error matches:")
        for event in observe["narrative_non_error_matches"][:5]:
            lines.append(f"- {event['timestamp']} {event['event_type']} {event['payload']}")
    if observe.get("acknowledged_errors"):
        lines.append("")
        lines.append("Acknowledged events:")
        for event in observe["acknowledged_errors"][:5]:
            ack = event.get("acknowledgement") or {}
            lines.append(
                f"- {event['timestamp']} {event['event_type']} ack_until={ack.get('expires_at')} "
                f"reason={ack.get('reason', '')}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect local Dr. Strange runtime diagnostics.")
    parser.add_argument("--db", default=os.getenv("DB_PATH", str(DEFAULT_DB_PATH)))
    parser.add_argument("--port", type=int, default=_env_int("WEB_CHAT_PORT", DEFAULT_PORT))
    parser.add_argument("--label", default=os.getenv("CLAW_LAUNCHD_LABEL", DEFAULT_LABEL))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--ack-path", default=os.getenv("DIAGNOSTICS_ACK_PATH", str(DEFAULT_ACK_PATH))
    )
    parser.add_argument("--ack-event", action="append", type=int, default=[])
    parser.add_argument(
        "--ack-current", action="store_true", help="Acknowledge all currently actionable events."
    )
    parser.add_argument("--ack-hours", type=float, default=24.0)
    parser.add_argument("--ack-reason", default="")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of readable text.")
    args = parser.parse_args(argv)
    ack_event_ids = list(args.ack_event)
    if args.ack_current:
        current_report = collect_diagnostics(
            db_path=args.db,
            port=args.port,
            label=args.label,
            limit=args.limit,
            ack_path=args.ack_path,
        )
        ack_event_ids.extend(
            int(event["id"])
            for event in ((current_report.get("database") or {}).get("observe") or {}).get(
                "latest_errors"
            )
            or []
        )
    if ack_event_ids:
        acknowledge_events(
            ack_event_ids,
            ack_path=args.ack_path,
            hours=args.ack_hours,
            reason=args.ack_reason,
        )
    report = collect_diagnostics(
        db_path=args.db,
        port=args.port,
        label=args.label,
        limit=args.limit,
        ack_path=args.ack_path,
    )
    if ack_event_ids:
        report["acknowledged_now"] = sorted(set(ack_event_ids))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
