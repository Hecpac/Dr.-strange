#!/usr/bin/env python3
"""Read-only smoke check for watchdog stale-event filtering.

This script is intentionally observational: it opens the runtime database in
SQLite read-only mode, classifies recent observe errors against the latest
startup context, and prints a reload-safety candidate report. It never calls
launchctl, restart.sh, the watchdog wrapper, or diagnostics' mutating ack path.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, IO, Sequence

from claw_v2.diagnostics import (
    ACTIONABLE_OBSERVE_EVENT_TYPES,
    _classify_error_events,
    _event_row,
    _latest_computer_use_errors,
    _latest_events,
    _merge_events,
    _parse_observe_timestamp,
    _table_exists,
)

DEFAULT_DB_PATH = Path("data/claw.db")
DEFAULT_EXPECTED_CODE_VERSION = "c42ae47"
DEFAULT_RECENT_HOURS = 24
DEFAULT_LIMIT = 20

NOT_EXECUTED_COMMANDS = [
    'launchctl print "gui/$(id -u)/com.pachano.claw-watchdog"',
    'launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.pachano.claw-watchdog.plist"',
    'launchctl kickstart -k "gui/$(id -u)/com.pachano.claw-watchdog"',
    'launchctl bootout "gui/$(id -u)/com.pachano.claw-watchdog"',
]


def collect_smoke_report(
    *,
    db_path: Path | str = DEFAULT_DB_PATH,
    expected_code_version: str = DEFAULT_EXPECTED_CODE_VERSION,
    recent_hours: int = DEFAULT_RECENT_HOURS,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Return a read-only watchdog stale-filter smoke report."""

    db = Path(db_path)
    report: dict[str, Any] = {
        "status": "unknown",
        "recommendation": "REVIEW",
        "db_path": str(db),
        "dry_run": True,
        "database_open_mode": "read_only",
        "expected_code_version": expected_code_version,
        "recent_hours": recent_hours,
        "reload_safe_candidate": False,
        "next_manual_step": "Review the smoke output before making any watchdog reload decision.",
        "side_effects": {
            "database": "read_only",
            "launchd": "not_touched",
            "watchdog": "not_reloaded",
            "daemon": "not_restarted",
        },
        "not_executed_commands": list(NOT_EXECUTED_COMMANDS),
    }
    if not db.exists():
        report["status"] = "missing_db"
        report["error"] = "database not found"
        return _finalize_report(report)

    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
    except sqlite3.Error as exc:
        report["status"] = "db_open_error"
        report["error"] = str(exc)
        return _finalize_report(report)

    try:
        if not _table_exists(conn, "observe_stream"):
            report["status"] = "missing_observe_stream"
            report["error"] = "observe_stream table not found"
            return _finalize_report(report)

        startup = _latest_startup_context(conn)
        if startup is None:
            report["status"] = "missing_startup_context"
            report["error"] = "no agent_startup_context event found"
            return _finalize_report(report)

        current_window = _current_window_from_startup(startup)
        error_candidates = _merge_events(
            _latest_events(
                conn,
                event_types=ACTIONABLE_OBSERVE_EVENT_TYPES,
                limit=limit * 5,
                hours=recent_hours,
            ),
            _latest_computer_use_errors(conn, limit=limit * 5, hours=recent_hours),
            limit=limit * 5,
        )
        classified = _classify_error_events(
            error_candidates,
            current_window=current_window,
            limit=limit,
        )

        code_version = current_window.get("code_version")
        code_version_matches = bool(code_version) and str(code_version) == expected_code_version
        report.update(
            {
                "current_daemon_window": current_window,
                "latest_startup_context": {
                    "id": startup["id"],
                    "timestamp": startup["timestamp"],
                    "code_version": code_version,
                    "pid": current_window.get("pid"),
                    "boot_id": current_window.get("boot_id"),
                },
                "code_version_matches": code_version_matches,
                "candidate_event_count": len(error_candidates),
                "stale_historical_event_count": len(classified["stale"]),
                "actionable_event_count": len(classified["actionable"]),
                "unknown_relevance_event_count": len(classified["unknown"]),
                "stale_filter_exercised": bool(classified["stale"]),
                "stale_historical_events": classified["stale"],
                "actionable_events": classified["actionable"],
                "unknown_relevance_events": classified["unknown"],
            }
        )

        if not current_window.get("known"):
            report["status"] = "unknown_current_startup_window"
        elif not code_version_matches:
            report["status"] = "version_mismatch"
        elif classified["actionable"]:
            report["status"] = "actionable_events_present"
        elif classified["unknown"]:
            report["status"] = "unknown_relevance_events_present"
        else:
            report["status"] = "safe_candidate"
            report["reload_safe_candidate"] = True
        return _finalize_report(report)
    except sqlite3.Error as exc:
        report["status"] = "db_query_error"
        report["error"] = str(exc)
        return _finalize_report(report)
    finally:
        conn.close()


def format_text(report: dict[str, Any]) -> str:
    """Human-readable report for operators."""

    lines = [
        "Watchdog stale-filter smoke (dry run)",
        f"status: {report.get('status')}",
        f"recommendation: {report.get('recommendation')}",
        f"db_path: {report.get('db_path')}",
        f"expected_code_version: {report.get('expected_code_version')}",
        f"reload_safe_candidate: {str(report.get('reload_safe_candidate')).lower()}",
    ]
    startup = report.get("latest_startup_context")
    if isinstance(startup, dict):
        lines.extend(
            [
                (
                    "latest_agent_startup_context: "
                    f"id={startup.get('id')} "
                    f"timestamp={startup.get('timestamp')} "
                    f"code_version={startup.get('code_version')}"
                ),
                f"code_version_matches: {str(report.get('code_version_matches')).lower()}",
            ]
        )
    if "candidate_event_count" in report:
        lines.extend(
            [
                f"candidate_error_events: {report.get('candidate_event_count')}",
                f"stale_historical_events: {report.get('stale_historical_event_count')}",
                f"actionable_events: {report.get('actionable_event_count')}",
                f"unknown_relevance_events: {report.get('unknown_relevance_event_count')}",
            ]
        )
    if report.get("error"):
        lines.append(f"error: {report['error']}")
    if report.get("next_manual_step"):
        lines.append(f"next_manual_step: {report['next_manual_step']}")
    lines.append("side_effects: database=read_only launchd=not_touched watchdog=not_reloaded")
    lines.append("not_executed_commands:")
    lines.extend(f"- {cmd}" for cmd in report.get("not_executed_commands", []))
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None, *, stdout: IO[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run watchdog stale-event filter smoke check."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to claw.db")
    parser.add_argument(
        "--expected-code-version",
        default=DEFAULT_EXPECTED_CODE_VERSION,
        help="Expected code_version in the latest agent_startup_context payload.",
    )
    parser.add_argument(
        "--recent-hours",
        type=int,
        default=DEFAULT_RECENT_HOURS,
        help="Observe error lookback window.",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Event sample limit.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON instead of text.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Kept for explicit operator intent; this script has no execute mode.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    out = sys.stdout if stdout is None else stdout

    report = collect_smoke_report(
        db_path=Path(args.db),
        expected_code_version=args.expected_code_version,
        recent_hours=max(1, args.recent_hours),
        limit=max(1, args.limit),
    )
    if args.json:
        json.dump(report, out, indent=2, sort_keys=True)
        out.write("\n")
    else:
        out.write(format_text(report))
    return _exit_code(report)


def _latest_startup_context(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        WHERE event_type = 'agent_startup_context'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return _event_row(row)


def _current_window_from_startup(startup: dict[str, Any]) -> dict[str, Any]:
    payload = startup.get("payload") if isinstance(startup.get("payload"), dict) else {}
    timestamp = startup.get("timestamp")
    epoch_s = _parse_observe_timestamp(timestamp)
    code_version = payload.get("code_version") or payload.get("startup_marker")
    if epoch_s is None:
        return {
            "known": False,
            "missing_reason": "invalid_startup_timestamp",
            "startup_event_id": startup.get("id"),
            "boot_timestamp": timestamp,
            "pid": payload.get("pid"),
            "boot_id": payload.get("boot_id"),
            "code_version": code_version,
        }
    return {
        "known": True,
        "source": "agent_startup_context",
        "startup_event_id": int(startup["id"]),
        "boot_timestamp": timestamp,
        "boot_timestamp_epoch_s": epoch_s,
        "startup_event_type": startup["event_type"],
        "pid": payload.get("pid"),
        "boot_id": payload.get("boot_id"),
        "code_version": code_version,
    }


def _exit_code(report: dict[str, Any]) -> int:
    if report.get("status") == "safe_candidate":
        return 0
    if report.get("status") in {"missing_db", "db_open_error", "missing_observe_stream"}:
        return 2
    return 1


def _finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    status = str(report.get("status") or "unknown")
    if status == "safe_candidate":
        report["recommendation"] = "PASS"
        report["next_manual_step"] = (
            "Operator may review not_executed_commands and decide whether to reload watchdog manually."
        )
    elif status in {"actionable_events_present", "unknown_relevance_events_present"}:
        report["recommendation"] = "REVIEW"
        report["next_manual_step"] = (
            "Investigate actionable or unknown-relevance events before any watchdog reload."
        )
    else:
        report["recommendation"] = "FAIL"
        report["next_manual_step"] = (
            "Do not reload watchdog from this result; fix the reported condition or rerun with a valid DB."
        )
    return report


if __name__ == "__main__":
    raise SystemExit(main())
