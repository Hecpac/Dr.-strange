"""Read-only behavior audit extractor for Dr. Strange (Claw v2).

Reads from data/claw.db (SQLite, opened in read-only mode) and
~/.claw/pending_approvals/ (filesystem), sanitizes via the project's own
claw_v2.redaction + claw_v2.leak_scrub, and emits a single JSONL with
behavior_audit_cases plus per-section raw samples.

No writes to claw.db, task ledger, or memory files. No prints of API keys,
tokens, credentials. All free-text fields pass through redact_sensitive +
scrub_for_persistence before they land in the JSONL.

Usage:
    .venv/bin/python scripts/audit/extract_behavior_cases.py

Defaults (overridable by CLI flags below):
    --messages 500
    --tool-calls 200
    --approvals 100
    --tasks 100
    --memory-writes 100
    --errors 50
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from claw_v2.leak_scrub import scrub_for_persistence
from claw_v2.redaction import redact_sensitive


DB_PATH = REPO_ROOT / "data" / "claw.db"
PENDING_APPROVALS_DIR = Path.home() / ".claw" / "pending_approvals"
OUT_DIR = REPO_ROOT / "reports" / "behavior_audit"
OUT_JSONL = OUT_DIR / "behavior_cases_sample.jsonl"
OUT_SUMMARY = OUT_DIR / "extraction_summary.json"


def _redact(value: Any, *, limit: int = 500) -> Any:
    """Apply both system-reminder scrub and secret redaction."""
    cleaned = scrub_for_persistence(value)
    return redact_sensitive(cleaned, limit=limit)


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def _json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    except (TypeError, ValueError):
        return {"_unparseable": True}


# --------------------------------------------------------------------- fetchers


def fetch_messages(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, session_id, role, content, created_at
        FROM messages
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        content_sanitized = _redact(row["content"] or "", limit=2000)
        out.append({
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "content": content_sanitized,
            "content_len": len(row["content"] or ""),
            "created_at": row["created_at"],
        })
    out.reverse()  # chronological
    return out


def fetch_tool_calls(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, timestamp, event_type, payload, trace_id, root_trace_id, job_id
        FROM observe_stream
        WHERE event_type IN ('sdk_post_tool_use', 'sdk_post_tool_use_failure', 'tool_call')
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _redact(_json_loads(row["payload"]), limit=1000)
        out.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "trace_id": row["trace_id"],
            "tool_name": payload.get("tool_name") if isinstance(payload, dict) else None,
            "session_id": payload.get("session_id") if isinstance(payload, dict) else None,
            "is_failure": row["event_type"] == "sdk_post_tool_use_failure",
            "payload": payload,
        })
    out.reverse()
    return out


def fetch_approvals_fs(limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not PENDING_APPROVALS_DIR.exists():
        return out
    files = sorted(
        PENDING_APPROVALS_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    for path in files:
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        # Strip token outright (don't even redact — drop the field).
        if isinstance(raw, dict):
            for sensitive in ("token", "approval_token", "secret", "signature"):
                raw.pop(sensitive, None)
        sanitized = _redact(raw, limit=2000)
        out.append({
            "approval_id": (raw or {}).get("approval_id") if isinstance(raw, dict) else None,
            "mtime": path.stat().st_mtime,
            "file": path.name,
            "data": sanitized,
        })
    out.reverse()
    return out


def fetch_tasks(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT task_id, session_id, channel, external_session_id, external_user_id,
               objective, mode, runtime, provider, model, status, notify_policy,
               created_at, started_at, completed_at, summary, error, verification_status,
               artifacts_json, route_json, metadata_json, updated_at
        FROM agent_tasks
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        artifacts = _redact(_json_loads(row["artifacts_json"]), limit=800)
        route = _redact(_json_loads(row["route_json"]), limit=400)
        metadata = _redact(_json_loads(row["metadata_json"]), limit=400)
        out.append({
            "task_id": row["task_id"],
            "session_id": row["session_id"],
            "channel": row["channel"],
            "external_user_id_redacted": "[REDACTED]" if row["external_user_id"] else None,
            "objective": _redact(row["objective"] or "", limit=400),
            "mode": row["mode"],
            "runtime": row["runtime"],
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "notify_policy": row["notify_policy"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "duration_seconds": (
                (row["completed_at"] or 0) - (row["started_at"] or 0)
                if row["started_at"] and row["completed_at"]
                else None
            ),
            "summary": _redact(row["summary"] or "", limit=400),
            "error": _redact(row["error"] or "", limit=400),
            "verification_status": row["verification_status"],
            "artifacts": artifacts,
            "route": route,
            "metadata": metadata,
            "updated_at": row["updated_at"],
        })
    out.reverse()
    return out


def fetch_memory_writes(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, key, value, source, source_trust, confidence,
               entity_tags, valid_from, valid_until, conflict_flag,
               agent_name, created_at
        FROM facts
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append({
            "id": row["id"],
            "key": _redact(row["key"] or "", limit=200),
            "value": _redact(row["value"] or "", limit=600),
            "source": row["source"],
            "source_trust": row["source_trust"],
            "confidence": row["confidence"],
            "entity_tags": _redact(_json_loads("[]") if not row["entity_tags"] else json.loads(row["entity_tags"]), limit=200),
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "conflict_flag": bool(row["conflict_flag"]),
            "agent_name": row["agent_name"],
            "created_at": row["created_at"],
        })
    out.reverse()
    return out


def fetch_errors_and_fallbacks(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, timestamp, event_type, payload, trace_id, lane, provider, model
        FROM observe_stream
        WHERE event_type IN (
            'sdk_post_tool_use_failure',
            'llm_fallback',
            'llm_circuit_open',
            'kairos_decide_failed',
            'scheduled_job_error',
            'startup_healthcheck_failed',
            'observation_window_freeze_engaged',
            'observation_window_freeze_released',
            'site_down',
            'p0_orphan_action_recovery',
            'autonomous_task_recovery_bootstrap',
            'evidence_gate_explicit_blocker',
            'brain_pushback_disagreement'
        )
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _redact(_json_loads(row["payload"]), limit=800)
        out.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "lane": row["lane"],
            "provider": row["provider"],
            "model": row["model"],
            "trace_id": row["trace_id"],
            "payload": payload,
        })
    out.reverse()
    return out


def fetch_dispatch_decisions(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, timestamp, payload
        FROM observe_stream
        WHERE event_type = 'dispatch_decision'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_loads(row["payload"])
        out.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "session_id": payload.get("session_id"),
            "handler": payload.get("handler"),
            "route": payload.get("route"),
            "reason": payload.get("reason"),
            "captured": payload.get("captured"),
            "text_preview": _redact(payload.get("text_preview", ""), limit=200),
            "text_len": payload.get("text_len") or payload.get("text_length"),
        })
    out.reverse()
    return out


def fetch_llm_response_costs(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, timestamp, lane, provider, model, payload
        FROM observe_stream
        WHERE event_type = 'llm_response'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_loads(row["payload"])
        out.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "lane": row["lane"],
            "provider": row["provider"],
            "model": row["model"],
            "cost_estimate": payload.get("cost_estimate"),
            "degraded_mode": payload.get("degraded_mode"),
            "session_id": payload.get("session_id"),
            "trace_id": payload.get("trace_id"),
        })
    out.reverse()
    return out


# ------------------------------------------------------------------- correlator


def build_behavior_cases(
    *,
    tasks: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    dispatch_decisions: list[dict[str, Any]],
    llm_costs: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project per-task behavior_audit_cases.

    Correlation key is `task_id` for tasks, with timestamps aligned to find
    related tool_calls (within ±60s of created_at) and llm_responses (within
    the started_at..completed_at window when available).
    """
    # Index tool calls by session_id (best-effort)
    tools_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tc in tool_calls:
        sess = tc.get("session_id") or "_unknown"
        tools_by_session[sess].append(tc)

    # Index dispatch decisions by session_id
    dispatch_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for dd in dispatch_decisions:
        sess = dd.get("session_id") or "_unknown"
        dispatch_by_session[sess].append(dd)

    # Index errors by trace
    errors_by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for err in errors:
        if err.get("trace_id"):
            errors_by_trace[err["trace_id"]].append(err)

    # Approval timestamps (mtime) for cross-correlation
    approval_mtimes = [a["mtime"] for a in approvals]

    cases: list[dict[str, Any]] = []
    for idx, task in enumerate(tasks, start=1):
        sess = task["session_id"]
        related_dispatch = dispatch_by_session.get(sess, [])[-3:]
        related_tools = tools_by_session.get(sess, [])
        tool_names = [t.get("tool_name") for t in related_tools if t.get("tool_name")]
        tool_failures = [t for t in related_tools if t.get("is_failure")]

        # Cost rollup for the session window
        sess_costs = [
            float(c.get("cost_estimate") or 0.0)
            for c in llm_costs
            if c.get("session_id") == sess
        ]
        total_cost = round(sum(sess_costs), 6)

        # Latency from started_at/completed_at if present
        latency = task.get("duration_seconds")

        # Approval window heuristic: was a pending approval created within
        # the task's lifetime?
        approval_in_window = False
        if task["started_at"] and task["completed_at"]:
            approval_in_window = any(
                task["started_at"] - 5 <= mt <= task["completed_at"] + 5
                for mt in approval_mtimes
            )

        # Classify failure_modes
        failure_modes: list[str] = []
        if task["status"] == "completed_unverified":
            failure_modes.append("completed_unverified_default")
        if task["status"] == "failed":
            failure_modes.append("hard_failure")
        if task["verification_status"] == "needs_verification":
            failure_modes.append("verifier_did_not_run_or_passed")
        if tool_failures:
            failure_modes.append(f"tool_failures_x{len(tool_failures)}")
        if not task.get("channel") and task["task_id"].startswith("brain-tooluse:tg-"):
            failure_modes.append("channel_not_populated_for_telegram_task")

        # Classify intent (heuristic from objective)
        objective = (task.get("objective") or "").lower()
        intent_keywords = {
            "install": ["instala", "install", "configurar"],
            "research": ["investiga", "research", "busca"],
            "code": ["fix", "refactor", "edita", "implementa", "code"],
            "deploy": ["deploy", "push", "merge", "publica"],
            "communicate": ["responde", "envía", "mensaje", "mail"],
            "browse": ["abre", "navega", "browse", "url", "página"],
            "automation": ["automatiza", "agenda", "schedule"],
            "review": ["audita", "revisa", "verifica", "valida"],
            "memory": ["recuerda", "guarda", "olvida", "anota"],
            "small_talk": ["dale", "ok", "listo", "continúa", "sigue", "procede", "ahora", "claro"],
        }
        detected_intent = "unknown"
        for label, keywords in intent_keywords.items():
            if any(k in objective for k in keywords):
                detected_intent = label
                break

        cases.append({
            "case_id": f"case-{idx:04d}",
            "timestamp": task["updated_at"],
            "channel": task["channel"] or ("telegram" if task["task_id"].startswith("brain-tooluse:tg-") else "unknown"),
            "session_id": sess,
            "user_request_sanitized": task["objective"],
            "detected_intent": detected_intent,
            "task_type": task["mode"] or task["runtime"] or "brain-tooluse",
            "risk_tier": None,  # not stored per-task today
            "autonomy_level": None,  # session-level, lives in session_state
            "tools_used": tool_names[-20:],  # cap to last 20 for readability
            "tool_calls_count": len(tool_names),
            "approval_requested": approval_in_window,
            "approval_was_necessary": None,  # human judgement; reviewer assigns
            "outcome": task["status"],
            "verification_status": task["verification_status"],
            "memory_written": None,  # not tracked per-task; correlated separately
            "cost_estimate_session_total": total_cost,
            "latency_seconds": latency,
            "failure_modes": failure_modes,
            "workflow_opportunity": None,  # filled in Phase 4 analysis
            "recommended_fix": None,       # filled in Phase 4 analysis
            "evidence_refs": {
                "summary": task.get("summary", "")[:300],
                "dispatch_decisions_last_3": [d.get("handler") for d in related_dispatch],
                "tool_failure_count": len(tool_failures),
                "errors_in_trace": [
                    e["event_type"]
                    for e in errors_by_trace.get(task.get("session_id", ""), [])
                ][:5],
            },
        })
    return cases


# ------------------------------------------------------------------------ main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", type=int, default=500)
    parser.add_argument("--tool-calls", type=int, default=200)
    parser.add_argument("--approvals", type=int, default=100)
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--memory-writes", type=int, default=100)
    parser.add_argument("--errors", type=int, default=50)
    parser.add_argument("--dispatch-decisions", type=int, default=300)
    parser.add_argument("--llm-costs", type=int, default=300)
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    started = time.time()
    conn = _ro_connect(DB_PATH)
    try:
        messages = fetch_messages(conn, args.messages)
        tool_calls = fetch_tool_calls(conn, args.tool_calls)
        tasks = fetch_tasks(conn, args.tasks)
        memory_writes = fetch_memory_writes(conn, args.memory_writes)
        errors = fetch_errors_and_fallbacks(conn, args.errors)
        dispatch_decisions = fetch_dispatch_decisions(conn, args.dispatch_decisions)
        llm_costs = fetch_llm_response_costs(conn, args.llm_costs)
    finally:
        conn.close()

    approvals = fetch_approvals_fs(args.approvals)

    # Also include all completed_unverified tasks regardless of recency,
    # since they're the most important behavior signal.
    conn = _ro_connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE status='completed_unverified'"
        ).fetchone()
        completed_unverified_total = int(rows[0])
    finally:
        conn.close()

    cases = build_behavior_cases(
        tasks=tasks,
        tool_calls=tool_calls,
        errors=errors,
        messages=messages,
        dispatch_decisions=dispatch_decisions,
        llm_costs=llm_costs,
        approvals=approvals,
    )

    with OUT_JSONL.open("w", encoding="utf-8") as fh:
        # Each line: one JSON object. First line is a manifest.
        manifest = {
            "type": "manifest",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "schema": "behavior_audit_cases.v1",
            "totals": {
                "cases": len(cases),
                "messages": len(messages),
                "tool_calls": len(tool_calls),
                "approvals": len(approvals),
                "tasks": len(tasks),
                "memory_writes": len(memory_writes),
                "errors": len(errors),
                "dispatch_decisions": len(dispatch_decisions),
                "llm_costs": len(llm_costs),
                "completed_unverified_total_in_db": completed_unverified_total,
            },
            "fields": [
                "case_id", "timestamp", "channel", "session_id", "user_request_sanitized",
                "detected_intent", "task_type", "risk_tier", "autonomy_level",
                "tools_used", "tool_calls_count", "approval_requested",
                "approval_was_necessary", "outcome", "verification_status",
                "memory_written", "cost_estimate_session_total", "latency_seconds",
                "failure_modes", "workflow_opportunity", "recommended_fix",
                "evidence_refs",
            ],
        }
        fh.write(json.dumps(manifest, ensure_ascii=False) + "\n")
        for case in cases:
            fh.write(json.dumps({"type": "case", **case}, ensure_ascii=False) + "\n")
        # Append raw samples as separate object groups so downstream review
        # can read the underlying evidence.
        for label, items in (
            ("message_sample", messages),
            ("tool_call_sample", tool_calls),
            ("approval_sample", approvals),
            ("task_sample", tasks),
            ("memory_write_sample", memory_writes),
            ("error_sample", errors),
            ("dispatch_decision_sample", dispatch_decisions),
            ("llm_response_sample", llm_costs),
        ):
            for item in items:
                fh.write(json.dumps({"type": label, **item}, ensure_ascii=False) + "\n")

    # Aggregated metrics for the report
    outcome_counter = Counter(c["outcome"] for c in cases)
    intent_counter = Counter(c["detected_intent"] for c in cases)
    tool_counter: Counter[str] = Counter()
    for c in cases:
        tool_counter.update(c["tools_used"])
    fail_mode_counter: Counter[str] = Counter()
    for c in cases:
        fail_mode_counter.update(c["failure_modes"])
    total_cost = round(sum(c["cost_estimate_session_total"] or 0.0 for c in cases), 4)
    durations = [c["latency_seconds"] for c in cases if c["latency_seconds"]]
    avg_latency = round(sum(durations) / len(durations), 2) if durations else None
    max_latency = round(max(durations), 2) if durations else None

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "duration_seconds": round(time.time() - started, 2),
        "cases_total": len(cases),
        "completed_unverified_total_in_db": completed_unverified_total,
        "outcome_distribution": dict(outcome_counter),
        "intent_distribution": dict(intent_counter),
        "top_10_tools": dict(tool_counter.most_common(10)),
        "failure_modes": dict(fail_mode_counter),
        "session_cost_estimate_sum": total_cost,
        "latency_avg_seconds": avg_latency,
        "latency_max_seconds": max_latency,
        "approvals_pending_fs_count": len(approvals),
        "memory_writes_in_window": len(memory_writes),
        "tool_calls_in_window": len(tool_calls),
        "messages_in_window": len(messages),
        "errors_in_window": len(errors),
        "dispatch_decisions_in_window": len(dispatch_decisions),
        "dispatch_route_distribution": dict(
            Counter(d["route"] for d in dispatch_decisions)
        ),
        "out_jsonl": str(OUT_JSONL.relative_to(REPO_ROOT)),
    }
    OUT_SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
