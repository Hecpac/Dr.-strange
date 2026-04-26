from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


Runner = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_LABEL = "com.pachano.claw"
DEFAULT_PORT = 8765
DEFAULT_DB_PATH = Path("data/claw.db")
DEFAULT_ACK_PATH = Path("data/diagnostics_acks.json")


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
        "port_listener": _run_command(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], runner=runner),
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
        "stdout": (result.stdout or "").strip()[:4000],
        "stderr": (result.stderr or "").strip()[:4000],
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
    except sqlite3.Error as exc:
        return {"present": True, "error": str(exc)}
    try:
        summary: dict[str, Any] = {"present": True}
        if _table_exists(conn, "observe_stream"):
            latest_errors = _latest_events(
                conn,
                event_types=(
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
                ),
                limit=limit,
                hours=24,
            )
            actionable_errors, acknowledged_errors = _partition_acknowledged_events(
                latest_errors,
                acknowledgements or {},
            )
            summary["observe"] = {
                "recent_events": _recent_events(conn, limit=limit),
                "event_counts_24h": _event_counts_24h(conn),
                "latest_errors": actionable_errors,
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
        return summary
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


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
        "payload": _loads_json(row["payload"]),
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
    return [dict(row) for row in rows]


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
    return [dict(row) for row in rows]


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
    return [dict(row) for row in rows]


def _loads_json(raw: Any) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(str(raw))
    except json.JSONDecodeError:
        return {"raw": str(raw)[:1000]}


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
    active = _load_acknowledgements(path, now=current)
    for event_id in event_ids:
        active[int(event_id)] = {
            "event_id": int(event_id),
            "reason": reason,
            "created_at": current,
            "expires_at": expires_at,
        }
    path.write_text(
        json.dumps({"acks": list(active.values())}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return [int(event_id) for event_id in event_ids]


def _checks(*, commands: dict[str, dict[str, Any]], database: dict[str, Any], port: int) -> dict[str, Any]:
    launchd_ok = bool(commands["launchctl_list"].get("ok") or commands["launchctl_print"].get("ok"))
    process_ok = bool(commands["processes"].get("stdout"))
    port_ok = f":{port}" in str(commands["port_listener"].get("stdout") or "")
    db_ok = bool(database.get("present")) and not database.get("error")
    active_jobs = len((database.get("jobs") or {}).get("active") or [])
    active_tasks = len((database.get("tasks") or {}).get("active") or [])
    latest_errors = len((database.get("observe") or {}).get("latest_errors") or [])
    acknowledged_errors = len((database.get("observe") or {}).get("acknowledged_errors") or [])
    status = "healthy" if process_ok and port_ok and db_ok and launchd_ok and latest_errors == 0 else "attention"
    if not process_ok or not port_ok or not db_ok:
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
        "acknowledged_error_events": acknowledged_errors,
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
        f"Claw diagnostics: {checks['status']}",
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
    observe = database.get("observe") or {}
    if observe.get("latest_errors"):
        lines.append("")
        lines.append("Recent actionable events:")
        for event in observe["latest_errors"][:5]:
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
    parser = argparse.ArgumentParser(description="Collect local Claw runtime diagnostics.")
    parser.add_argument("--db", default=os.getenv("DB_PATH", str(DEFAULT_DB_PATH)))
    parser.add_argument("--port", type=int, default=_env_int("WEB_CHAT_PORT", DEFAULT_PORT))
    parser.add_argument("--label", default=os.getenv("CLAW_LAUNCHD_LABEL", DEFAULT_LABEL))
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--ack-path", default=os.getenv("DIAGNOSTICS_ACK_PATH", str(DEFAULT_ACK_PATH)))
    parser.add_argument("--ack-event", action="append", type=int, default=[])
    parser.add_argument("--ack-current", action="store_true", help="Acknowledge all currently actionable events.")
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
            for event in ((current_report.get("database") or {}).get("observe") or {}).get("latest_errors") or []
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
