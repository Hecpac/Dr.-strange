from __future__ import annotations

import json as json_lib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from claw_v2.edge_client import CoreEdgeClient, EdgeHttpResponse, HttpxEdgeTransport
from claw_v2.edge_protocol import EdgeTaskRequest, canonical_json, verify_headers
from claw_v2.jobs import JobService


class FakeTransport:
    def __init__(self) -> None:
        self.gets: list[tuple[str, dict[str, str]]] = []
        self.posts: list[tuple[str, dict, bytes, dict[str, str]]] = []
        self.health = EdgeHttpResponse(
            200,
            {
                "ready": True,
                "capabilities": {"computer_use": "available"},
                "queue_depth": 0,
                "capacity": 2,
            },
        )
        self.identity = EdgeHttpResponse(
            200,
            {
                "edge_id": "mac-edge",
                "endpoint": "https://mac.tailnet.ts.net",
                "capabilities": ["computer_use"],
                "key_id": "edge",
                "connectivity_layer": "tailscale",
            },
        )
        self.post_responses = [EdgeHttpResponse(200, {"task_id": "task-1", "status": "queued"})]

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> EdgeHttpResponse:
        self.gets.append((url, headers))
        if url.endswith("/.well-known/claw-edge.json"):
            return self.identity
        if url.endswith("/a2a/v1/health"):
            return self.health
        return EdgeHttpResponse(200, {"task_id": "task-1", "status": "completed", "result": {"ok": True}})

    def post(
        self,
        url: str,
        *,
        json: dict | None = None,
        content: bytes | None = None,
        headers: dict[str, str],
        timeout: float,
    ) -> EdgeHttpResponse:
        payload = json if json is not None else json_lib.loads((content or b"{}").decode("utf-8"))
        self.posts.append((url, payload, content or b"", headers))
        return self.post_responses.pop(0)


class FlakyPostTransport(FakeTransport):
    def post(
        self,
        url: str,
        *,
        json: dict | None = None,
        content: bytes | None = None,
        headers: dict[str, str],
        timeout: float,
    ) -> EdgeHttpResponse:
        payload = json if json is not None else json_lib.loads((content or b"{}").decode("utf-8"))
        self.posts.append((url, payload, content or b"", headers))
        if len(self.posts) == 1:
            raise TimeoutError("network partition")
        return EdgeHttpResponse(200, {"task_id": "task-1", "status": "queued"})


def _request(**overrides) -> EdgeTaskRequest:
    values = {
        "task_id": "task-1",
        "job_id": "job-1",
        "capability": "computer_use",
        "action": "click",
        "payload": {"target": "Save"},
        "deadline_ms": 30_000,
        "idempotency_key": "job-1:task-1",
        "trace_id": "trace-1",
        "callback_url": "https://core/a2a/v1/callbacks/task-1",
    }
    values.update(overrides)
    return EdgeTaskRequest(**values)


def _client(transport: FakeTransport, **overrides) -> CoreEdgeClient:
    values = {
        "endpoint": "https://mac.tailnet.ts.net",
        "key_id": "core",
        "secret": "secret",
        "transport": transport,
        "clock": lambda: 1000.0,
        "sleep": lambda _: None,
        "retry_delays": (0.01, 0.02),
    }
    values.update(overrides)
    return CoreEdgeClient(**values)


def test_fetch_identity_validates_connectivity_layer() -> None:
    transport = FakeTransport()
    identity = _client(transport).fetch_identity()

    assert identity.edge_id == "mac-edge"
    assert identity.connectivity_layer == "tailscale"


def test_health_failure_enters_degraded_without_posting_task() -> None:
    transport = FakeTransport()
    transport.health = EdgeHttpResponse(503, {"ready": False, "reason": "mac asleep"})

    result = _client(transport).submit_task(_request())

    assert result.status == "degraded"
    assert result.degraded_reason == "edge health HTTP 503"
    assert transport.posts == []


def test_backpressure_blocks_task_admission_without_posting_task() -> None:
    transport = FakeTransport()
    transport.health = EdgeHttpResponse(
        200,
        {
            "ready": True,
            "capabilities": {"computer_use": "available"},
            "queue_depth": 2,
            "capacity": 2,
            "retry_after_ms": 4000,
        },
    )

    result = _client(transport).submit_task(_request())

    assert result.status == "backpressured"
    assert result.retry_after_ms == 4000
    assert transport.posts == []


def test_submit_task_signs_payload_and_records_job_step(tmp_path: Path) -> None:
    transport = FakeTransport()
    jobs = JobService(tmp_path / "claw.db")
    jobs.enqueue(kind="edge", job_id="job-1")

    result = _client(transport, jobs=jobs).submit_task(_request())

    assert result.status == "queued"
    assert len(transport.posts) == 1
    url, payload, body, headers = transport.posts[0]
    assert url == "https://mac.tailnet.ts.net/a2a/v1/tasks"
    assert payload["callback_url"] == "https://core/a2a/v1/callbacks/task-1"
    assert body == canonical_json(payload)
    ok, reason = verify_headers(
        method="POST",
        path="/a2a/v1/tasks",
        body=body,
        headers=headers,
        secrets={"core": "secret"},
        now=1000,
    )
    assert ok, reason
    steps = jobs.steps("job-1")
    assert len(steps) == 1
    assert steps[0].step_class == "edge"
    assert steps[0].idempotency_key == "job-1:task-1"


def test_submit_task_retries_503_then_uses_existing_idempotency_key() -> None:
    transport = FakeTransport()
    transport.post_responses = [
        EdgeHttpResponse(503, {"error": "busy"}),
        EdgeHttpResponse(200, {"task_id": "task-1", "status": "queued"}),
    ]

    result = _client(transport).submit_task(_request())

    assert result.status == "queued"
    assert len(transport.posts) == 2
    assert transport.posts[0][1]["idempotency_key"] == transport.posts[1][1]["idempotency_key"]
    assert transport.posts[0][2] == transport.posts[1][2]


def test_submit_task_retries_transport_errors() -> None:
    transport = FlakyPostTransport()

    result = _client(transport).submit_task(_request())

    assert result.status == "queued"
    assert len(transport.posts) == 2


def test_task_status_fetches_final_edge_state() -> None:
    transport = FakeTransport()

    result = _client(transport).task_status("task-1")

    assert result.status == "completed"
    assert result.result == {"ok": True}


def test_httpx_transport_sends_canonical_content_bytes() -> None:
    received: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            received["body"] = self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    body = canonical_json({"z": 1, "a": {"b": 2}})
    try:
        response = HttpxEdgeTransport().post(
            f"http://127.0.0.1:{server.server_port}/a2a/v1/tasks",
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=2.0,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)

    assert response.status_code == 200
    assert received["body"] == body
