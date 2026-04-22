from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any


def traces_payload(observe: Any | None, *, path: str) -> tuple[int, dict]:
    if observe is None:
        return 503, {"error": "observe stream unavailable"}
    limit = _query_param_as_int(path, "limit", default=10)
    traces: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in observe.recent_events(limit=max(limit * 10, 50)):
        trace_id = event.get("trace_id")
        if not trace_id or trace_id in seen:
            continue
        seen.add(trace_id)
        traces.append(
            {
                "trace_id": trace_id,
                "timestamp": event.get("timestamp"),
                "last_event_type": event.get("event_type"),
                "lane": event.get("lane"),
                "provider": event.get("provider"),
                "model": event.get("model"),
                "artifact_id": event.get("artifact_id"),
                "job_id": event.get("job_id"),
            }
        )
        if len(traces) >= limit:
            break
    return 200, {"traces": traces}


def trace_replay_payload(observe: Any | None, *, trace_id: str) -> tuple[int, dict]:
    if not trace_id:
        return 400, {"error": "trace_id must be provided"}
    if observe is None:
        return 503, {"error": "observe stream unavailable"}
    events = observe.trace_events(trace_id)
    if not events:
        return 404, {"error": f"trace not found: {trace_id}"}
    replay = [
        {
            "timestamp": event["timestamp"],
            "event_type": event["event_type"],
            "lane": event["lane"],
            "provider": event["provider"],
            "model": event["model"],
            "span_id": event["span_id"],
            "parent_span_id": event["parent_span_id"],
            "artifact_id": event["artifact_id"],
            "job_id": event["job_id"],
            "payload": event["payload"],
        }
        for event in events
    ]
    return 200, {"trace_id": trace_id, "event_count": len(replay), "events": replay}


def jobs_payload(job_service: Any | None, *, path: str) -> tuple[int, dict]:
    if job_service is None:
        return 503, {"error": "job service unavailable"}
    state = _query_param(path, "state") or "active"
    include_terminal = state in {"all", "completed", "failed", "cancelled"}
    jobs = job_service.list_jobs(limit=_query_param_as_int(path, "limit", default=20), include_terminal=include_terminal)
    if state not in {"all", "active"}:
        jobs = [job for job in jobs if job.state == state]
    return 200, {"jobs": [_job_payload(job) for job in jobs]}


def job_detail_payload(job_service: Any | None, *, method: str, job_id: str) -> tuple[int, dict]:
    if job_service is None:
        return 503, {"error": "job service unavailable"}
    if method.upper() == "DELETE":
        try:
            job = job_service.cancel(job_id, reason="api")
        except KeyError:
            return 404, {"error": f"job not found: {job_id}"}
        return 200, {"job": _job_payload(job)}
    if method.upper() != "GET":
        return 405, {"error": "method not allowed", "allowed": ["GET", "DELETE"]}
    job = job_service.get(job_id)
    if job is None:
        return 404, {"error": f"job not found: {job_id}"}
    return 200, {"job": _job_payload(job), "steps": [asdict(step) for step in job_service.steps(job_id)]}


def approvals_payload(approvals: Any | None) -> tuple[int, dict]:
    if approvals is None:
        return 503, {"error": "approval service unavailable"}
    return 200, {"approvals": [_approval_payload(item) for item in approvals.list_pending()]}


def approval_detail_payload(approvals: Any | None, *, method: str, approval_id: str, body: bytes | None = None, action: str | None = None) -> tuple[int, dict]:
    if approvals is None:
        return 503, {"error": "approval service unavailable"}
    if action == "approve" and method.upper() == "POST":
        token = _decode_body(body).get("token")
        if not isinstance(token, str) or not token:
            return 400, {"error": "token must be provided"}
        return 200, {"approval_id": approval_id, "approved": approvals.approve(approval_id, token)}
    if action == "reject" and method.upper() == "POST":
        approvals.reject(approval_id)
        return 200, {"approval_id": approval_id, "status": "rejected"}
    if method.upper() != "GET":
        return 405, {"error": "method not allowed", "allowed": ["GET", "POST"]}
    try:
        return 200, {"approval": _approval_payload(approvals.read(approval_id))}
    except FileNotFoundError:
        return 404, {"error": f"approval not found: {approval_id}"}


def _job_payload(job: Any) -> dict:
    return {
        "id": job.job_id,
        "kind": job.kind,
        "state": job.state,
        "version": job.version,
        "updated_at": job.updated_at,
        "payload": job.payload,
    }


def _approval_payload(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key != "token_hash"}


def _decode_body(body: bytes | None) -> dict:
    try:
        decoded = json.loads((body or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _query_param(path: str, name: str) -> str | None:
    if "?" not in path:
        return None
    for chunk in path.split("?", 1)[1].split("&"):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key == name:
            return value
    return None


def _query_param_as_int(path: str, name: str, *, default: int) -> int:
    value = _query_param(path, name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default
