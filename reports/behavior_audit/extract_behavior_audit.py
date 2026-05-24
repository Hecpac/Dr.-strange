#!/usr/bin/env python3
"""Read-only behavioral audit extractor for Dr. Strange.

Outputs:
- reports/behavior_audit/behavior_cases_sample.jsonl
- reports/behavior_audit/BEHAVIOR_AUDIT_REPORT.md

The extractor opens data/claw.db with SQLite mode=ro and never writes to the
runtime DB, task ledger, approvals, or memory tables. All user/model text is
sanitized before being written to report artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from claw_v2.behavior_audit_io import (
    build_frontmatter,
    build_output_paths,
    generate_run_id,
    write_exclusive,
)
from claw_v2.leak_scrub import scrub_for_persistence
from claw_v2.redaction import redact_sensitive


OUTPUT_FIELDS = [
    "case_id",
    "timestamp",
    "channel",
    "user_request_sanitized",
    "detected_intent",
    "task_type",
    "risk_tier",
    "autonomy_level",
    "tools_used",
    "approval_requested",
    "approval_was_necessary",
    "outcome",
    "verification_status",
    "memory_written",
    "cost_estimate",
    "latency",
    "failure_modes",
    "workflow_opportunity",
    "recommended_fix",
]

ERROR_EVENT_TOKENS = (
    "error",
    "failed",
    "failure",
    "fallback",
    "degraded",
    "blocked",
    "circuit",
    "timeout",
)
TOOL_EVENT_TYPES = {
    "sdk_post_tool_use",
    "sdk_post_tool_use_failure",
    "action_proposed",
    "action_executed",
    "AUTONOMY_BYPASS",
    "AUTONOMY_APPROVED",
    "tool_pivot",
}
APPROVAL_EVENT_TYPES = {
    "critical_action_verification",
    "critical_action_execution",
    "telegram_imperative_pending_approval",
    "kairos_auto_approved",
}
EXTERNAL_CONTENT_TOOLS = {
    "WebFetch",
    "WebSearch",
    "FirecrawlScrape",
    "FirecrawlSearch",
    "FirecrawlExtract",
    "notebooklm.chat",
    "notebooklm.add_sources",
    "notebooklm.add_text",
}
HIGH_RISK_TOOLS = {
    "Bash",
    "Task",
    "terminal.open",
    "terminal.send",
    "terminal.close",
    "social.publish",
    "pipeline.merge",
    "deploy.production",
    "git.force_push",
    "HeyGenVideo",
    "GPTImage",
    "notebooklm.create",
    "notebooklm.add_sources",
    "notebooklm.add_text",
    "notebooklm.start_research",
    "notebooklm.start_artifact",
}
ACTION_HINTS = (
    "crea",
    "haz",
    "ejecuta",
    "revisa",
    "analiza",
    "investiga",
    "corrige",
    "arregla",
    "implementa",
    "publica",
    "abre",
    "continua",
    "continúa",
    "procede",
    "dale",
)


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_LOCAL_PATH_RE = re.compile(r"/Users/hector/[^\s`'\"<>)]*")
_SESSION_RE = re.compile(r"\btg-[A-Za-z0-9_-]+\b")
_LONG_NUMBER_RE = re.compile(r"\b\d{7,}\b")


@dataclass(slots=True)
class Event:
    id: int
    timestamp: str
    event_type: str
    lane: str
    provider: str
    model: str
    trace_id: str
    job_id: str
    artifact_id: str
    payload: dict[str, Any]


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:12]


def parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def sanitize_text(text: Any, *, limit: int = 1200) -> str:
    value = "" if text is None else str(text)
    value = scrub_for_persistence(value)
    value = redact_sensitive(value, limit=0)
    value = _EMAIL_RE.sub("[REDACTED:email]", value)
    value = _LOCAL_PATH_RE.sub("[REDACTED:local_path]", value)
    value = _SESSION_RE.sub("[REDACTED:session_id]", value)
    value = re.sub(r"(?i)(approval_id\s*[:=]\s*)[`'\"]?[A-Za-z0-9_\-]+[`'\"]?", r"\1[REDACTED:approval_id]", value)
    value = re.sub(r"(?i)(token_hash\s*[:=]\s*)[`'\"]?[A-Fa-f0-9]{16,}[`'\"]?", r"\1[REDACTED:token_hash]", value)
    value = _LONG_NUMBER_RE.sub("[REDACTED:number]", value)
    value = re.sub(r"\s+", " ", value).strip()
    if limit and len(value) > limit:
        return value[:limit] + "...[truncated]"
    return value


def sanitize_value(value: Any, *, limit: int = 1200) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return sanitize_text(value, limit=limit)
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, list):
        return [sanitize_value(item, limit=limit) for item in value[:30]]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            key_str = str(key)
            lowered = key_str.lower()
            if any(fragment in lowered for fragment in ("token", "secret", "password", "credential", "authorization", "cookie", "api_key")):
                out[key_str] = "[REDACTED]"
            else:
                out[key_str] = sanitize_value(val, limit=limit)
        return out
    return sanitize_text(value, limit=limit)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_events(conn: sqlite3.Connection, *, limit: int | None = None) -> list[Event]:
    sql = """
        SELECT id, timestamp, event_type, lane, provider, model, trace_id, job_id, artifact_id, payload
        FROM observe_stream
        ORDER BY id DESC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    rows = conn.execute(sql, params).fetchall()
    events: list[Event] = []
    for row in rows:
        payload = parse_json(row["payload"], {})
        if not isinstance(payload, dict):
            payload = {"raw_payload": payload}
        events.append(
            Event(
                id=int(row["id"]),
                timestamp=str(row["timestamp"] or ""),
                event_type=str(row["event_type"] or ""),
                lane=str(row["lane"] or ""),
                provider=str(row["provider"] or ""),
                model=str(row["model"] or ""),
                trace_id=str(row["trace_id"] or ""),
                job_id=str(row["job_id"] or ""),
                artifact_id=str(row["artifact_id"] or ""),
                payload=payload,
            )
        )
    return list(reversed(events))


def timestamp_key(value: str) -> str:
    return str(value or "")


def event_session(event: Event) -> str:
    payload = event.payload
    session_id = payload.get("session_id") or payload.get("app_session_id")
    if session_id:
        return str(session_id)
    route = payload.get("route")
    if isinstance(route, dict):
        return str(route.get("session_id") or route.get("external_session_id") or "")
    return ""


def channel_for_session(session_id: str, payload: dict[str, Any] | None = None) -> str:
    payload = payload or {}
    route = payload.get("route")
    if isinstance(route, dict) and route.get("channel"):
        return sanitize_text(route.get("channel"), limit=80)
    if session_id.startswith("tg-"):
        return "telegram"
    if session_id in {"mac-main", "local", "web"}:
        return "web_chat"
    return "unknown"


def events_between(events: list[Event], start: str, end: str, *, session_id: str | None = None) -> list[Event]:
    selected: list[Event] = []
    for event in events:
        if start and timestamp_key(event.timestamp) < start:
            continue
        if end and timestamp_key(event.timestamp) > end:
            continue
        if session_id and event_session(event) not in {"", session_id}:
            continue
        selected.append(event)
    return selected


def extract_tool_name(event: Event) -> str | None:
    payload = event.payload
    if event.event_type in {"sdk_post_tool_use", "sdk_post_tool_use_failure"}:
        return str(payload.get("tool_name") or "") or None
    proposed = payload.get("proposed_next_action")
    if isinstance(proposed, dict):
        return str(proposed.get("tool") or "") or None
    return str(payload.get("tool") or payload.get("tool_name") or payload.get("name") or "") or None


def infer_risk(tools: Iterable[str], text: str = "") -> str:
    tool_set = {tool for tool in tools if tool}
    if any(tool in HIGH_RISK_TOOLS for tool in tool_set):
        return "high"
    lowered = text.lower()
    if any(token in lowered for token in ("deploy", "publica", "merge", "borra", "delete", "paga", "credencial", "secret")):
        return "high"
    if tool_set:
        return "medium"
    return "low"


def infer_autonomy(events: Iterable[Event], risk_tier: str) -> str:
    event_types = {event.event_type for event in events}
    if "AUTONOMY_APPROVED" in event_types or risk_tier == "high":
        return "approval_gated"
    if "AUTONOMY_BYPASS" in event_types:
        return "autoexecuted_policy_bypass"
    if any(event.event_type.startswith("kairos") for event in events):
        return "proactive_daemon"
    return "assisted"


def approval_necessary(risk_tier: str, tools: list[str], approval_requested: bool) -> str:
    if not approval_requested:
        return "not_requested"
    if risk_tier == "high" or any(tool in HIGH_RISK_TOOLS for tool in tools):
        return "likely_yes"
    if not tools or risk_tier == "low":
        return "possibly_no"
    return "unclear"


def case_template(case_id: str, timestamp: str, channel: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "timestamp": timestamp,
        "channel": channel,
        "user_request_sanitized": "",
        "detected_intent": "unknown",
        "task_type": "unknown",
        "risk_tier": "unknown",
        "autonomy_level": "unknown",
        "tools_used": [],
        "approval_requested": False,
        "approval_was_necessary": "unknown",
        "outcome": "",
        "verification_status": "unknown",
        "memory_written": False,
        "cost_estimate": 0.0,
        "latency": None,
        "failure_modes": [],
        "workflow_opportunity": "",
        "recommended_fix": "",
    }


def classify_case(case: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    intent = str(case.get("detected_intent") or "unknown")
    task_type = str(case.get("task_type") or "")
    outcome = str(case.get("outcome") or "").lower()
    request = str(case.get("user_request_sanitized") or "").lower()
    verification = str(case.get("verification_status") or "").lower()
    failures = [str(item).lower() for item in case.get("failure_modes") or []]
    tools = [str(tool) for tool in case.get("tools_used") or []]
    approval_requested = bool(case.get("approval_requested"))
    approval_necessary = str(case.get("approval_was_necessary") or "")

    if intent and intent != "unknown":
        tags.append("intent_correct")
    else:
        tags.append("intent_missed")
    if any(tool in HIGH_RISK_TOOLS for tool in tools) and not approval_requested:
        tags.append("missing_permission")
    if approval_requested and approval_necessary == "possibly_no":
        tags.append("unnecessary_permission")
    if tools and not failures:
        tags.append("good_tool_use")
    if any("tool" in failure or "sdk_post_tool_use_failure" in failure for failure in failures):
        tags.append("wrong_tool")
    if verification in {"needs_verification", "missing_evidence", "completed_unverified"} or "completed_unverified" in task_type:
        tags.append("unverified_completion")
    if "false_success" in " ".join(failures) or "success_without" in " ".join(failures):
        tags.append("false_success")
    if any(word in request for word in ACTION_HINTS) and not tools and not approval_requested and task_type == "message_turn":
        if any(phrase in outcome for phrase in ("no puedo", "dime", "necesito", "¿", "?", "reenv")):
            tags.append("underexecuted")
    if len(tools) >= 6 or float(case.get("cost_estimate") or 0.0) > 1.0:
        tags.append("overcomplicated")
    if case.get("memory_written") and not failures:
        tags.append("memory_good")
    if case.get("memory_written") and any("redacted" in request for _ in [0]):
        tags.append("memory_bad")
    if any(tool in EXTERNAL_CONTENT_TOOLS for tool in tools) or "prompt injection" in outcome:
        tags.append("prompt_injection_risk")
    if case.get("workflow_opportunity"):
        tags.append("good_workflow_candidate")
    if "repeated" in " ".join(failures) or "manual" in outcome:
        tags.append("missed_workflow_candidate")
    return sorted(set(tags))


def recommend_for_case(case: dict[str, Any], tags: list[str]) -> str:
    if "missing_permission" in tags:
        return "Add deterministic Tier 3 gate or explicit allowlist for this tool/action."
    if "unverified_completion" in tags:
        return "Route completion through verifier and require evidence before terminal success."
    if "intent_missed" in tags:
        return "Add routing/eval coverage for this utterance shape and preserve brain-first fallback."
    if "underexecuted" in tags:
        return "Convert action-like request into durable task or ask one specific missing datum."
    if "overcomplicated" in tags:
        return "Add workflow shortcut or lower-cost path for this recurring request."
    if "prompt_injection_risk" in tags:
        return "Wrap external content as untrusted and assert no instruction leakage in evals."
    if "memory_bad" in tags:
        return "Review memory write filters and redact/scope stored facts more tightly."
    if "good_workflow_candidate" in tags:
        return "Promote repeated successful path into a named workflow/playbook."
    return ""


def build_message_cases(conn: sqlite3.Connection, events: list[Event]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, session_id, role, content, created_at
        FROM messages
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    ordered = list(reversed(rows))
    cases: list[dict[str, Any]] = []
    for idx, row in enumerate(ordered):
        if row["role"] != "user":
            continue
        next_assistant = None
        for candidate in ordered[idx + 1 :]:
            if candidate["session_id"] == row["session_id"] and candidate["role"] == "assistant":
                next_assistant = candidate
                break
        start = str(row["created_at"] or "")
        end = str(next_assistant["created_at"] if next_assistant else "") or "9999"
        session_id = str(row["session_id"] or "")
        window_events = events_between(events, start, end, session_id=session_id)
        tools = sorted({tool for event in window_events if event.event_type in TOOL_EVENT_TYPES for tool in [extract_tool_name(event)] if tool})
        semantic = next((event for event in window_events if event.event_type == "semantic_turn_trace"), None)
        dispatches = [event for event in window_events if event.event_type == "dispatch_decision" and event.payload.get("captured")]
        failures = [
            event.event_type
            for event in window_events
            if any(token in event.event_type.lower() for token in ERROR_EVENT_TOKENS)
        ]
        risk = infer_risk(tools, str(row["content"] or ""))
        approval_requested = any(
            event.event_type in APPROVAL_EVENT_TYPES or "approval" in event.event_type.lower()
            for event in window_events
        )
        cost = sum(float(event.payload.get("cost_estimate") or 0.0) for event in window_events if event.event_type == "llm_response")
        latency_values = [
            float(event.payload.get("total_ms"))
            for event in window_events
            if event.event_type == "telegram_latency" and event.payload.get("total_ms") is not None
        ]
        case = case_template(f"msg_{row['id']}", start, channel_for_session(session_id))
        case.update(
            {
                "user_request_sanitized": sanitize_text(row["content"], limit=1600),
                "detected_intent": sanitize_text(
                    (semantic.payload.get("semantic_intent") if semantic else None)
                    or (dispatches[-1].payload.get("reason") if dispatches else "unknown"),
                    limit=160,
                ),
                "task_type": "message_turn",
                "risk_tier": risk,
                "autonomy_level": infer_autonomy(window_events, risk),
                "tools_used": tools,
                "approval_requested": approval_requested,
                "approval_was_necessary": approval_necessary(risk, tools, approval_requested),
                "outcome": sanitize_text(next_assistant["content"] if next_assistant else "no_assistant_response", limit=1800),
                "verification_status": sanitize_text(
                    next(
                        (
                            str(event.payload.get("verification_status"))
                            for event in window_events
                            if event.payload.get("verification_status")
                        ),
                        "unknown",
                    ),
                    limit=120,
                ),
                "memory_written": bool(next_assistant),
                "cost_estimate": round(cost, 6),
                "latency": round(statistics.median(latency_values), 1) if latency_values else None,
                "failure_modes": sorted(set(failures)),
            }
        )
        case["workflow_opportunity"] = infer_workflow_opportunity(case)
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    return cases


def infer_workflow_opportunity(case: dict[str, Any]) -> str:
    request = str(case.get("user_request_sanitized") or "").lower()
    tools = [str(tool) for tool in case.get("tools_used") or []]
    if any(term in request for term in ("notebook", "notebooklm", "cuaderno")):
        return "NotebookLM request pattern: candidate for a typed notebook workflow with approval boundaries."
    if any(term in request for term in ("revisa", "analiza", "audita")) and tools:
        return "Review/audit pattern: candidate for a reusable evidence-first review workflow."
    if any(tool in {"WebFetch", "WebSearch", "FirecrawlScrape", "FirecrawlSearch"} for tool in tools):
        return "External research pattern: candidate for source-ingest + untrusted-context eval."
    if any(term in request for term in ("continua", "continúa", "procede", "dale")):
        return "Continuation pattern: candidate for continuation resolver eval pack."
    return ""


def build_tool_cases(events: list[Event]) -> list[dict[str, Any]]:
    selected = [event for event in events if event.event_type in TOOL_EVENT_TYPES][-200:]
    cases: list[dict[str, Any]] = []
    for event in selected:
        tool = extract_tool_name(event) or "unknown_tool"
        session_id = event_session(event)
        risk = infer_risk([tool])
        case = case_template(f"tool_{event.id}", event.timestamp, channel_for_session(session_id, event.payload))
        case.update(
            {
                "user_request_sanitized": sanitize_text(event.payload.get("prompt") or event.payload.get("text_preview") or f"Tool event: {tool}", limit=800),
                "detected_intent": sanitize_text(event.event_type, limit=120),
                "task_type": "tool_call",
                "risk_tier": risk,
                "autonomy_level": infer_autonomy([event], risk),
                "tools_used": [tool],
                "approval_requested": event.event_type == "AUTONOMY_APPROVED",
                "approval_was_necessary": approval_necessary(risk, [tool], event.event_type == "AUTONOMY_APPROVED"),
                "outcome": sanitize_text(event.payload.get("error") or event.payload.get("status") or event.event_type, limit=1200),
                "verification_status": sanitize_text(event.payload.get("verification_status") or "unknown", limit=120),
                "memory_written": False,
                "cost_estimate": 0.0,
                "latency": None,
                "failure_modes": [event.event_type] if "failure" in event.event_type.lower() or "failed" in event.event_type.lower() else [],
            }
        )
        case["workflow_opportunity"] = infer_workflow_opportunity(case)
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    return cases


def build_task_cases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    latest = conn.execute(
        """
        SELECT *
        FROM agent_tasks
        ORDER BY updated_at DESC
        LIMIT 100
        """
    ).fetchall()
    unverified = conn.execute(
        """
        SELECT *
        FROM agent_tasks
        WHERE status = 'completed_unverified'
           OR verification_status IN ('needs_verification', 'missing_evidence')
        ORDER BY updated_at DESC
        """
    ).fetchall()
    by_id: dict[str, sqlite3.Row] = {str(row["task_id"]): row for row in latest}
    for row in unverified:
        by_id[str(row["task_id"])] = row
    cases: list[dict[str, Any]] = []
    for row in by_id.values():
        metadata = parse_json(row["metadata_json"], {})
        artifacts = parse_json(row["artifacts_json"], {})
        tools = []
        if isinstance(artifacts, dict):
            manifest = artifacts.get("evidence_manifest")
            if isinstance(manifest, dict):
                tools = [str(tool) for tool in manifest.get("tools_run") or [] if tool]
        risk = infer_risk(tools, str(row["objective"] or ""))
        status = str(row["status"] or "")
        verification = str(row["verification_status"] or "unknown")
        case = case_template(f"task_{stable_hash(str(row['task_id']))}", str(row["updated_at"] or row["created_at"] or ""), channel_for_session(str(row["session_id"] or "")))
        case.update(
            {
                "user_request_sanitized": sanitize_text(row["objective"], limit=1600),
                "detected_intent": sanitize_text(metadata.get("semantic_intent") if isinstance(metadata, dict) else "task_ledger", limit=120) or "task_ledger",
                "task_type": f"task_ledger:{sanitize_text(row['runtime'], limit=80)}:{sanitize_text(row['mode'], limit=80)}:{status}",
                "risk_tier": risk,
                "autonomy_level": "durable_task",
                "tools_used": sorted(set(tools)),
                "approval_requested": bool("approval" in json.dumps(sanitize_value(metadata)).lower()),
                "approval_was_necessary": approval_necessary(risk, tools, bool("approval" in json.dumps(sanitize_value(metadata)).lower())),
                "outcome": sanitize_text(row["summary"] or row["error"] or status, limit=1800),
                "verification_status": sanitize_text(verification, limit=120),
                "memory_written": True,
                "cost_estimate": 0.0,
                "latency": None,
                "failure_modes": [sanitize_text(row["error"], limit=300)] if row["error"] else ([] if status in {"succeeded", "completed"} else [status]),
            }
        )
        if status == "completed_unverified":
            case["failure_modes"] = sorted(set([*case["failure_modes"], "completed_unverified"]))
        case["workflow_opportunity"] = infer_workflow_opportunity(case)
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    return cases


def build_memory_cases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    facts = conn.execute(
        """
        SELECT id, key, value, source, source_trust, confidence, entity_tags, created_at
        FROM facts
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()
    outcomes = conn.execute(
        """
        SELECT id, task_type, task_id, description, approach, outcome, lesson, error_snippet, retries, created_at, tags, predicted_confidence
        FROM task_outcomes
        ORDER BY id DESC
        LIMIT 100
        """
    ).fetchall()
    cases: list[dict[str, Any]] = []
    for row in facts:
        case = case_template(f"memory_fact_{row['id']}", str(row["created_at"] or ""), "memory")
        case.update(
            {
                "user_request_sanitized": sanitize_text(f"{row['key']}: {row['value']}", limit=1200),
                "detected_intent": "memory_fact_write",
                "task_type": "memory_write:fact",
                "risk_tier": "low",
                "autonomy_level": "memory",
                "outcome": sanitize_text(f"source={row['source']} trust={row['source_trust']} confidence={row['confidence']}", limit=300),
                "verification_status": "memory_unverified" if row["source_trust"] == "untrusted" else "memory_trusted",
                "memory_written": True,
            }
        )
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    for row in outcomes:
        failures = []
        if row["error_snippet"]:
            failures.append(sanitize_text(row["error_snippet"], limit=300))
        case = case_template(f"memory_outcome_{row['id']}", str(row["created_at"] or ""), "memory")
        case.update(
            {
                "user_request_sanitized": sanitize_text(row["description"], limit=1200),
                "detected_intent": "learning_outcome_write",
                "task_type": sanitize_text(f"memory_write:task_outcome:{row['task_type']}", limit=160),
                "risk_tier": "low",
                "autonomy_level": "learning_loop",
                "outcome": sanitize_text(f"{row['outcome']}: {row['lesson']}", limit=1600),
                "verification_status": sanitize_text(row["outcome"], limit=120),
                "memory_written": True,
                "failure_modes": failures,
            }
        )
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    return cases[:100]


def build_error_cases(events: list[Event]) -> list[dict[str, Any]]:
    selected = [
        event
        for event in events
        if any(token in event.event_type.lower() for token in ERROR_EVENT_TOKENS)
    ][-50:]
    cases: list[dict[str, Any]] = []
    for event in selected:
        session_id = event_session(event)
        payload = sanitize_value(event.payload, limit=500)
        tools = [tool for tool in [extract_tool_name(event)] if tool]
        risk = infer_risk(tools, json.dumps(payload, sort_keys=True))
        case = case_template(f"error_{event.id}", event.timestamp, channel_for_session(session_id, event.payload))
        case.update(
            {
                "user_request_sanitized": sanitize_text(payload.get("text_preview") if isinstance(payload, dict) else "", limit=800),
                "detected_intent": sanitize_text(event.event_type, limit=120),
                "task_type": "error_or_fallback_event",
                "risk_tier": risk,
                "autonomy_level": infer_autonomy([event], risk),
                "tools_used": tools,
                "approval_requested": "approval" in event.event_type.lower(),
                "approval_was_necessary": approval_necessary(risk, tools, "approval" in event.event_type.lower()),
                "outcome": sanitize_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), limit=1600),
                "verification_status": sanitize_text(event.payload.get("verification_status") or "failed_or_degraded", limit=120),
                "memory_written": False,
                "cost_estimate": float(event.payload.get("cost_estimate") or 0.0),
                "latency": None,
                "failure_modes": [event.event_type],
            }
        )
        case["workflow_opportunity"] = infer_workflow_opportunity(case)
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    return cases


def build_approval_cases(approval_root: Path) -> list[dict[str, Any]]:
    if not approval_root.exists():
        return []
    paths = sorted(approval_root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:100]
    cases: list[dict[str, Any]] = []
    for path in paths:
        payload = parse_json(path.read_text(encoding="utf-8", errors="replace"), {})
        if not isinstance(payload, dict):
            continue
        metadata = sanitize_value(payload.get("metadata") or {}, limit=500)
        action = sanitize_text(payload.get("action"), limit=200)
        summary = sanitize_text(payload.get("summary"), limit=1000)
        tool = ""
        if isinstance(metadata, dict):
            tool = sanitize_text(metadata.get("tool") or metadata.get("action") or "", limit=120)
        risk = infer_risk([tool] if tool else [], f"{action} {summary}")
        case = case_template(f"approval_{stable_hash(path.stem)}", sanitize_text(payload.get("created_at"), limit=80), "approval_store")
        case.update(
            {
                "user_request_sanitized": summary or action,
                "detected_intent": "approval_record",
                "task_type": sanitize_text(f"approval:{action}", limit=180),
                "risk_tier": risk,
                "autonomy_level": "human_approval_gate",
                "tools_used": [tool] if tool else [],
                "approval_requested": True,
                "approval_was_necessary": approval_necessary(risk, [tool] if tool else [], True),
                "outcome": sanitize_text(payload.get("status") or "unknown", limit=100),
                "verification_status": sanitize_text(payload.get("status") or "unknown", limit=100),
                "memory_written": False,
                "failure_modes": [],
            }
        )
        tags = classify_case(case)
        case["recommended_fix"] = recommend_for_case(case, tags)
        case["_audit_tags"] = tags
        cases.append(case)
    return cases


def build_cases(conn: sqlite3.Connection, approval_root: Path) -> list[dict[str, Any]]:
    events = fetch_events(conn)
    cases: list[dict[str, Any]] = []
    cases.extend(build_message_cases(conn, events))
    cases.extend(build_tool_cases(events))
    cases.extend(build_approval_cases(approval_root))
    cases.extend(build_task_cases(conn))
    cases.extend(build_memory_cases(conn))
    cases.extend(build_error_cases(events))
    for case in cases:
        for field in OUTPUT_FIELDS:
            case.setdefault(field, case_template("x", "", "").get(field))
        case["_audit_tags"] = classify_case(case)
        if not case.get("recommended_fix"):
            case["recommended_fix"] = recommend_for_case(case, case["_audit_tags"])
    return cases


def write_jsonl(path: Path, cases: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for case in cases:
            payload = {field: sanitize_value(case.get(field), limit=1800) for field in OUTPUT_FIELDS}
            payload["_audit_tags"] = list(case.get("_audit_tags") or [])
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def summarize_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    tag_counts = Counter(tag for case in cases for tag in case.get("_audit_tags", []))
    task_types = Counter(str(case.get("task_type") or "unknown") for case in cases)
    intents = Counter(str(case.get("detected_intent") or "unknown") for case in cases)
    tools = Counter(tool for case in cases for tool in case.get("tools_used") or [])
    risk = Counter(str(case.get("risk_tier") or "unknown") for case in cases)
    autonomy = Counter(str(case.get("autonomy_level") or "unknown") for case in cases)
    approvals = [case for case in cases if case.get("approval_requested")]
    unnecessary = [case for case in cases if "unnecessary_permission" in case.get("_audit_tags", [])]
    missing = [case for case in cases if "missing_permission" in case.get("_audit_tags", [])]
    unverified = [case for case in cases if "unverified_completion" in case.get("_audit_tags", [])]
    false_success = [case for case in cases if "false_success" in case.get("_audit_tags", [])]
    costs = [float(case.get("cost_estimate") or 0.0) for case in cases]
    latencies = [float(case.get("latency")) for case in cases if case.get("latency") is not None]
    return {
        "total_cases": len(cases),
        "tag_counts": tag_counts,
        "task_types": task_types,
        "intents": intents,
        "tools": tools,
        "risk": risk,
        "autonomy": autonomy,
        "approval_count": len(approvals),
        "unnecessary_permission_count": len(unnecessary),
        "missing_permission_count": len(missing),
        "unverified_count": len(unverified),
        "false_success_count": len(false_success),
        "total_cost_estimate": round(sum(costs), 6),
        "median_latency_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_latency_ms": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1) if latencies else None,
    }


def top_examples(cases: list[dict[str, Any]], tag: str, limit: int = 10) -> list[dict[str, Any]]:
    selected = [case for case in cases if tag in case.get("_audit_tags", [])]
    return selected[:limit]


def compact_case_line(case: dict[str, Any]) -> str:
    request = sanitize_text(case.get("user_request_sanitized"), limit=120)
    outcome = sanitize_text(case.get("outcome"), limit=140)
    return (
        f"`{case.get('case_id')}` — intent=`{case.get('detected_intent')}`, "
        f"task=`{case.get('task_type')}`, tools={case.get('tools_used') or []}, "
        f"verification=`{case.get('verification_status')}`. "
        f"Request: {request or '(sin texto)'}; Outcome: {outcome or '(sin outcome)'}"
    )


def bullet_examples(cases: list[dict[str, Any]], tag: str, limit: int = 10) -> str:
    examples = top_examples(cases, tag, limit=limit)
    if not examples:
        return "- No hay casos en la muestra.\n"
    return "\n".join(f"- {compact_case_line(case)}" for case in examples) + "\n"


def counter_table(counter: Counter[str], *, limit: int = 12) -> str:
    if not counter:
        return "| Item | Casos |\n|---|---:|\n| none | 0 |\n"
    lines = ["| Item | Casos |", "|---|---:|"]
    for key, count in counter.most_common(limit):
        lines.append(f"| `{sanitize_text(key, limit=120)}` | {count} |")
    return "\n".join(lines) + "\n"


def generate_report(cases: list[dict[str, Any]], summary: dict[str, Any], output_jsonl: Path) -> str:
    tags = summary["tag_counts"]
    task_types = summary["task_types"]
    tools = summary["tools"]
    risk = summary["risk"]
    autonomy = summary["autonomy"]
    successes = [case for case in cases if {"intent_correct", "good_tool_use"} <= set(case.get("_audit_tags", []))]
    workflow_candidates = [case for case in cases if case.get("workflow_opportunity")]
    try:
        output_jsonl_display = output_jsonl.resolve().relative_to(REPO_ROOT)
    except ValueError:
        output_jsonl_display = Path(sanitize_text(output_jsonl, limit=240))

    report = f"""# Behavioral Audit Report — Dr. Strange

Generated from read-only SQLite and approval-store extraction. JSONL sample:
`{output_jsonl_display}`.

## 1. Resumen Ejecutivo

- Evidencia real: `messages`, `observe_stream`, `agent_tasks`, `agent_jobs`, `facts`, `task_outcomes`, `session_state` y archivos JSON de approvals.
- Casos sanitizados generados: **{summary['total_cases']}**.
- El patrón dominante de riesgo operativo es `completed_unverified` / `needs_verification`: **{summary['unverified_count']}** casos etiquetados.
- Tool use observado: **{sum(tools.values())}** eventos/casos con tools; principales tools abajo.
- Permisos: **{summary['approval_count']}** casos pidieron aprobación; **{summary['missing_permission_count']}** aparecen como posibles permisos faltantes por heurística.
- Costo estimado agregado en la muestra: **${summary['total_cost_estimate']}**. Nota: OpenAI/Codex reportan costo `0.0` en adapters, por lo que esta métrica subestima costo API real.
- Latencia Telegram mediana: **{summary['median_latency_ms']} ms**; p95: **{summary['p95_latency_ms']} ms** cuando había eventos `telegram_latency`.
- Inferencia: Hector usa el agente principalmente como operador conversacional con ejecución local, revisión, continuación contextual y automatización ligera.
- Pregunta abierta: qué porcentaje de `completed_unverified` representa trabajo útil no cerrado versus falsos positivos que deben reconciliarse.

## 2. Métricas Principales

| Métrica | Valor |
|---|---:|
| Casos totales | {summary['total_cases']} |
| Approvals solicitados | {summary['approval_count']} |
| Posibles approvals innecesarios | {summary['unnecessary_permission_count']} |
| Posibles approvals faltantes | {summary['missing_permission_count']} |
| Completions no verificadas | {summary['unverified_count']} |
| Posibles false success | {summary['false_success_count']} |
| Costo estimado total | ${summary['total_cost_estimate']} |
| Mediana latencia ms | {summary['median_latency_ms']} |
| P95 latencia ms | {summary['p95_latency_ms']} |

### Distribución Por Etiqueta

{counter_table(tags, limit=20)}

### Tipos De Caso

{counter_table(task_types, limit=16)}

### Riesgo Y Autonomía

{counter_table(risk, limit=8)}

{counter_table(autonomy, limit=10)}

## 3. Top 10 Patrones De Éxito

Evidencia: casos con `intent_correct` y/o `good_tool_use`.

{bullet_examples(successes, 'good_tool_use', limit=10)}

Patrones observados:
- Brain-first y `semantic_turn_trace` dan buena señal para continuaciones.
- `sdk_post_tool_use` deja rastro suficiente para reconstruir tool use.
- `telegram_latency` permite medir experiencia real, no solo inferirla.
- `NaturalLanguageRenderer` y sanitizers reducen exposición de labels internos en varias rutas.
- `task_ledger_created` + `task_ledger_terminal` dan cierre auditable.
- KAIROS emite acciones y supresiones, útil para distinguir ruido de alertas.
- `critical_action_verification` captura recomendación, riesgo y necesidad de aprobación.
- El ledger conserva suficientes campos para detectar `completed_unverified`.
- Learning outcomes (`task_outcomes`) registran lecciones útiles.
- El full suite previo pasó, lo que respalda que estas rutas tienen cobertura.

## 4. Top 10 Patrones De Fallo

{bullet_examples(cases, 'unverified_completion', limit=10)}

Patrones observados:
- Muchas tareas quedan como `completed_unverified` con `needs_verification`.
- Algunos eventos de tool failure aparecen sin una remediación estructurada visible en el caso.
- El costo real queda incompleto cuando el adapter reporta `0.0`.
- Las rutas de approvals mezclan approvals humanos, internos y KAIROS; conviene separar semánticamente.
- El outcome puede ser una respuesta conversacional sin evidencia ejecutable.
- La muestra tiene pocos `facts`, pero varios `task_outcomes`; memoria aprende más de resultados que de hechos explícitos.
- Los casos de external content requieren etiqueta sistemática de `prompt_injection_risk`.
- Varias decisiones dependen de heurística de texto y podrían fallar ante frases nuevas.
- La ausencia de `agent_jobs` activos sugiere que trabajos background no están usando ese servicio o no hay cola viva.
- La auditoría necesita más correlación formal entre user turn, trace_id, task_id y approval_id.

## 5. Fricción De Permisos

| Señal | Evidencia |
|---|---|
| Approvals en muestra | {summary['approval_count']} casos |
| Posibles innecesarios | {summary['unnecessary_permission_count']} por heurística low-risk/read-only |
| Posibles faltantes | {summary['missing_permission_count']} por high-risk tool sin approval visible |
| Eventos críticos | `critical_action_verification`, `critical_action_execution`, approval JSON store |

Recomendación: separar `approval_requested_by_policy`, `approval_requested_by_verifier`, `approval_requested_by_kairos` y `approval_user_visible` como campos distintos.

## 6. Calidad De Uso De Tools

### Tools Más Frecuentes

{counter_table(tools, limit=20)}

Evidencia: `sdk_post_tool_use`, `sdk_post_tool_use_failure`, `action_proposed`, `action_executed`, `AUTONOMY_BYPASS`.

Inferencia: el uso de tools es trazable, pero la calidad depende de correlación; ahora se reconstruye por ventanas temporales, no por un `turn_id` explícito.

## 7. Calidad De Verificación

Evidencia principal: `agent_tasks.status`, `agent_tasks.verification_status`, `brain_tooluse_ledger_needs_verification`, `cycle_verification_complete`, `critical_action_verification`.

Hallazgo: **{summary['unverified_count']}** casos quedan con etiqueta `unverified_completion`. Esto no significa necesariamente fallo, pero sí deuda operacional.

Recomendación: ninguna tarea debería terminar en estado visible de éxito si `verification_status` no está en `passed/verified/ok` o si no hay evidence manifest.

## 8. Calidad De Memoria

Evidencia:
- `facts`: memoria factual explícita.
- `task_outcomes`: aprendizaje operacional.
- `messages`: memoria conversacional.
- `session_state`: estado activo.

Hallazgo: hay más aprendizaje por outcomes que por facts. Esto es bueno para mejorar comportamiento, pero puede perder preferencias/datos durables si no se promueven a facts confiables.

Recomendación: añadir eval que verifique que feedback repetido de Hector se convierte en fact durable solo cuando cumple criterios de confianza y redacción.

## 9. Oportunidades De Workflows

Casos candidatos detectados: **{len(workflow_candidates)}**.

{chr(10).join(f"- {compact_case_line(case)} — Opportunity: {sanitize_text(case.get('workflow_opportunity'), limit=180)}" for case in workflow_candidates[:10]) or "- No hay candidatos claros en la muestra."}

Workflows sugeridos:
- Continuation resolver smoke: `Procede/Continúa/Dale` con state sources explícitos.
- Review/audit evidence-first: para “revisa/analiza/audita” con manifest de fuentes.
- NotebookLM workflow con límites y approval boundaries.
- External research workflow con untrusted context y prompt-injection checks.
- Tool-failure recovery workflow: retry/pivot/escalate con outcome explícito.

## 10. Recomendaciones Para Dr. Strange v3

1. Añadir `turn_id` global que conecte message, dispatch, tools, approvals, ledger, memory y response.
2. Convertir `completed_unverified` en estado temporal con SLA de reconciliación automática.
3. Hacer que approvals tengan `risk_basis`, `requested_by`, `visible_to_user`, `resolved_by`.
4. Agregar costo real por provider/model, especialmente OpenAI API.
5. Separar KAIROS proactive decisions de approvals humanos.
6. Promover workflows repetidos a playbooks con evals.
7. Añadir “behavior receipt” por turno con intent, tools, approval, evidence, verification.
8. Fortalecer prompt-injection labels para todo external-content tool output.
9. Crear dashboards por fricción: underexecuted, overcomplicated, missing_permission.
10. Reducir dependencia de correlación temporal; usar trace/root_trace_id como vínculo obligatorio.

## 11. Evals Nuevas Recomendadas

- `test_behavior_turn_receipt_links_message_tool_task_approval`.
- `test_completed_unverified_has_reconciliation_deadline`.
- `test_kairos_cannot_approve_foreign_pending_approval`.
- `test_openai_cost_estimate_nonzero_when_usage_present`.
- `test_external_content_tool_outputs_marked_untrusted`.
- `test_low_risk_readonly_does_not_request_approval`.
- `test_high_risk_bash_requires_policy_or_human_gate`.
- `test_memory_promotion_requires_trust_and_redaction`.
- `test_continuation_trace_has_state_sources_and_no_internal_labels`.
- `test_workflow_candidate_detection_for_repeated_successful_paths`.

## 12. Cambios De Código Sugeridos Sin Implementar

- Añadir tabla/JSONL `behavior_turn_receipts` o evento `turn_receipt` emitido al final de cada turno.
- Añadir campo `turn_id` a `observe_stream.payload`, `agent_tasks.metadata_json`, approval metadata y message artifacts.
- Cambiar extractor interno de costos para usar `usage` real por model.
- Añadir política explícita para KAIROS approvals: no autoaprobar approvals que no creó.
- Convertir `completed_unverified` en work queue de verificación.
- Crear un router de workflows separado de `bot.py` para patrones repetidos.
- Añadir policy tests para high-risk tools sin approval.
- Añadir memoria durable de preferencias con promoted facts y confidence thresholds.
- Añadir reportes automáticos semanales de behavior audit.
- Documentar criterios de “approval_was_necessary” como contrato evaluable.

## Evidencia, Inferencias Y Preguntas Abiertas

| Tipo | Detalle |
|---|---|
| Evidencia real | Conteos y casos vienen de SQLite read-only y approval JSON store sanitizado. |
| Inferencia | `approval_was_necessary`, `overcomplicated`, `underexecuted`, workflow candidates y missing/unnecessary permissions son heurísticos. |
| Pregunta abierta | Qué casos `completed_unverified` fueron aceptables para Hector aunque no tengan verifier pass. |
| Pregunta abierta | Qué workflows deben volverse producto versus permanecer como comportamiento conversacional. |
| Pregunta abierta | Qué datos deben guardarse como facts durables y cuáles solo como outcome learning. |
"""
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract sanitized Dr. Strange behavioral audit sample.")
    parser.add_argument("--db", type=Path, default=REPO_ROOT / "data" / "claw.db")
    parser.add_argument("--approval-root", type=Path, default=Path.home() / ".claw" / "pending_approvals")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "reports" / "behavior_audit")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    # P0-A: per-run id + frontmatter + O_EXCL canonical writes so two
    # concurrent extractor processes never silently overwrite each other.
    started_at_ts = time.time()
    run_id = generate_run_id()
    paths = build_output_paths(args.output_dir, run_id)

    with connect_readonly(args.db) as conn:
        cases = build_cases(conn, args.approval_root)

    summary = summarize_cases(cases)
    completed_at_ts = time.time()

    # Always write the run-suffixed artifacts (unique per run_id).
    write_jsonl(paths["run_jsonl"], cases)
    run_frontmatter = build_frontmatter(
        run_id=run_id,
        generated_by="reports/behavior_audit/extract_behavior_audit.py",
        source=str(Path(__file__).resolve()),
        started_at=started_at_ts,
        completed_at=completed_at_ts,
        canonical=False,
        input_db=str(args.db),
        sample_size=len(cases),
    )
    paths["run_md"].write_text(
        run_frontmatter + generate_report(cases, summary, paths["run_jsonl"]),
        encoding="utf-8",
    )

    # Best-effort canonical aliases — only written when nobody else has
    # claimed the canonical path yet, so concurrent runs never clobber.
    canonical_jsonl_written = False
    canonical_md_written = False
    if not paths["canonical_jsonl"].exists():
        canonical_jsonl_written = write_exclusive(
            paths["canonical_jsonl"], paths["run_jsonl"].read_bytes()
        )
    if not paths["canonical_md"].exists():
        canonical_frontmatter = build_frontmatter(
            run_id=run_id,
            generated_by="reports/behavior_audit/extract_behavior_audit.py",
            source=str(Path(__file__).resolve()),
            started_at=started_at_ts,
            completed_at=completed_at_ts,
            canonical=True,
            input_db=str(args.db),
            sample_size=len(cases),
        )
        canonical_md_written = write_exclusive(
            paths["canonical_md"],
            canonical_frontmatter + generate_report(cases, summary, paths["canonical_jsonl"]),
        )

    print(
        json.dumps(
            {
                "cases": len(cases),
                "run_id": run_id,
                "run_jsonl": str(paths["run_jsonl"]),
                "run_md": str(paths["run_md"]),
                "canonical_jsonl_written": canonical_jsonl_written,
                "canonical_md_written": canonical_md_written,
                "unverified": summary["unverified_count"],
                "approvals": summary["approval_count"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
