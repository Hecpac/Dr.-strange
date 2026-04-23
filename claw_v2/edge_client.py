from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from claw_v2.edge_protocol import (
    EdgeHealth,
    EdgeIdentity,
    EdgeTaskRequest,
    EdgeTaskStatus,
    canonical_json,
    sign_headers,
)


@dataclass(slots=True)
class EdgeHttpResponse:
    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)
    text: str = ""


class EdgeTransport(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> EdgeHttpResponse:
        ...

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str],
        timeout: float,
    ) -> EdgeHttpResponse:
        ...


class HttpxEdgeTransport:
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> EdgeHttpResponse:
        import httpx

        response = httpx.get(url, headers=headers, timeout=timeout)
        return EdgeHttpResponse(response.status_code, _json_payload(response), response.text)

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str],
        timeout: float,
    ) -> EdgeHttpResponse:
        import httpx

        response = httpx.post(url, json=json, content=content, headers=headers, timeout=timeout)
        return EdgeHttpResponse(response.status_code, _json_payload(response), response.text)


class CoreEdgeClient:
    def __init__(
        self,
        *,
        endpoint: str,
        key_id: str,
        secret: str,
        transport: EdgeTransport | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: tuple[float, ...] = (1.0, 3.0, 10.0),
        observe: Any | None = None,
        jobs: Any | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.key_id = key_id
        self.secret = secret
        self.transport = transport or HttpxEdgeTransport()
        self.clock = clock
        self.sleep = sleep
        self.retry_delays = retry_delays
        self.observe = observe
        self.jobs = jobs

    def fetch_identity(self) -> EdgeIdentity:
        path = "/.well-known/claw-edge.json"
        response = self.transport.get(self._url(path), headers=self._headers("GET", path, b""), timeout=2.0)
        if response.status_code >= 400:
            raise RuntimeError(f"edge identity failed: HTTP {response.status_code}")
        return EdgeIdentity.from_mapping(response.payload)

    def health(self) -> EdgeHealth:
        path = "/a2a/v1/health"
        try:
            response = self.transport.get(self._url(path), headers=self._headers("GET", path, b""), timeout=2.0)
            if response.status_code >= 400:
                return EdgeHealth.unavailable(f"edge health HTTP {response.status_code}")
            return EdgeHealth.from_mapping(response.payload)
        except Exception as exc:
            return EdgeHealth.unavailable(f"edge health failed: {exc}")

    def submit_task(self, request: EdgeTaskRequest) -> EdgeTaskStatus:
        health = self.health()
        blocker = health.admission_blocker(request.capability, request.deadline_ms)
        if blocker:
            status = "backpressured" if health.ready and health.queue_depth >= health.capacity else "degraded"
            result = EdgeTaskStatus(
                task_id=request.task_id,
                status=status,  # type: ignore[arg-type]
                degraded_reason=blocker,
                retry_after_ms=health.retry_after_ms,
            )
            self._record_job_step(request, result)
            self._emit("edge_task_degraded", request, result)
            return result

        payload = request.to_payload()
        last_error = ""
        for attempt, delay in enumerate((0.0, *self.retry_delays), start=1):
            if delay:
                self.sleep(delay)
            try:
                response = self._post_task(payload)
            except Exception as exc:
                last_error = f"edge task failed: {exc}"
                continue
            if response.status_code in {429, 503}:
                last_error = f"edge HTTP {response.status_code}"
                if response.status_code == 429:
                    result = EdgeTaskStatus(
                        task_id=request.task_id,
                        status="backpressured",
                        degraded_reason=last_error,
                        retry_after_ms=int(response.payload.get("retry_after_ms") or 0),
                    )
                    self._record_job_step(request, result)
                    self._emit("edge_task_backpressured", request, result)
                    return result
                continue
            if response.status_code >= 400:
                result = EdgeTaskStatus(request.task_id, "failed", error=f"edge HTTP {response.status_code}")
                self._record_job_step(request, result)
                self._emit("edge_task_failed", request, result)
                return result
            result = EdgeTaskStatus.from_mapping(response.payload)
            self._record_job_step(request, result, attempt=attempt)
            self._emit("edge_task_submitted", request, result)
            return result

        result = EdgeTaskStatus(request.task_id, "degraded", degraded_reason=last_error or "edge retry budget exhausted")
        self._record_job_step(request, result)
        self._emit("edge_task_degraded", request, result)
        return result

    def task_status(self, task_id: str) -> EdgeTaskStatus:
        path = f"/a2a/v1/tasks/{task_id}"
        response = self.transport.get(self._url(path), headers=self._headers("GET", path, b""), timeout=5.0)
        if response.status_code >= 400:
            return EdgeTaskStatus(task_id, "failed", error=f"edge HTTP {response.status_code}")
        return EdgeTaskStatus.from_mapping(response.payload)

    def _post_task(self, payload: dict[str, Any]) -> EdgeHttpResponse:
        path = "/a2a/v1/tasks"
        body = canonical_json(payload)
        return self.transport.post(self._url(path), content=body, headers=self._headers("POST", path, body), timeout=10.0)

    def _headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        headers = sign_headers(
            method=method,
            path=path,
            body=body,
            key_id=self.key_id,
            secret=self.secret,
            now=self.clock(),
        )
        headers["Content-Type"] = "application/json"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.endpoint}{path}"

    def _record_job_step(self, request: EdgeTaskRequest, result: EdgeTaskStatus, *, attempt: int = 1) -> None:
        if self.jobs is None or not hasattr(self.jobs, "record_step"):
            return
        step_state = "completed" if result.status in {"queued", "running", "completed"} else "skipped"
        try:
            self.jobs.record_step(
                request.job_id,
                "edge_task",
                state=step_state,
                step_class="edge",
                payload={"task_id": request.task_id, "status": result.status, "attempt": attempt},
                side_effect_ref=request.task_id,
                idempotency_key=request.idempotency_key,
            )
        except Exception:
            return

    def _emit(self, event_type: str, request: EdgeTaskRequest, result: EdgeTaskStatus) -> None:
        if self.observe is None or not hasattr(self.observe, "emit"):
            return
        self.observe.emit(
            event_type,
            trace_id=request.trace_id or None,
            root_trace_id=request.root_trace_id or None,
            span_id=request.span_id or None,
            job_id=request.job_id,
            artifact_id=request.artifact_id or None,
            payload={"task_id": request.task_id, "capability": request.capability, "status": result.status},
        )


def _json_payload(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
