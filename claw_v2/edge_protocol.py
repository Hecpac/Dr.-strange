from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


PROTOCOL_VERSION = "a2a/1"
SCHEMA_VERSION = 1
ConnectivityLayer = Literal["tailscale", "wireguard", "cloudflare_tunnel", "local"]
EdgeTaskState = Literal[
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
    "degraded",
    "backpressured",
    "rejected",
]


@dataclass(slots=True)
class EdgeIdentity:
    edge_id: str
    endpoint: str
    capabilities: list[str]
    key_id: str
    connectivity_layer: ConnectivityLayer
    protocol_version: str = PROTOCOL_VERSION
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "EdgeIdentity":
        identity = cls(
            edge_id=str(data["edge_id"]),
            endpoint=str(data["endpoint"]),
            capabilities=[str(item) for item in data.get("capabilities", [])],
            key_id=str(data["key_id"]),
            connectivity_layer=str(data["connectivity_layer"]),  # type: ignore[arg-type]
            protocol_version=str(data.get("protocol_version") or PROTOCOL_VERSION),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
        )
        identity.validate()
        return identity

    def validate(self) -> None:
        if self.protocol_version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported edge protocol: {self.protocol_version}")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported edge schema: {self.schema_version}")
        if self.connectivity_layer not in {"tailscale", "wireguard", "cloudflare_tunnel", "local"}:
            raise ValueError(f"unapproved connectivity layer: {self.connectivity_layer}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EdgeHealth:
    ready: bool
    capabilities: dict[str, str] = field(default_factory=dict)
    degraded_reasons: dict[str, str] = field(default_factory=dict)
    queue_depth: int = 0
    capacity: int = 1
    running_tasks: int = 0
    retry_after_ms: int = 0
    reason: str = ""

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "EdgeHealth":
        return cls(
            ready=bool(data.get("ready")),
            capabilities={str(k): str(v) for k, v in dict(data.get("capabilities") or {}).items()},
            degraded_reasons={str(k): str(v) for k, v in dict(data.get("degraded_reasons") or {}).items()},
            queue_depth=int(data.get("queue_depth") or 0),
            capacity=max(1, int(data.get("capacity") or 1)),
            running_tasks=int(data.get("running_tasks") or 0),
            retry_after_ms=int(data.get("retry_after_ms") or 0),
            reason=str(data.get("reason") or ""),
        )

    @classmethod
    def unavailable(cls, reason: str) -> "EdgeHealth":
        return cls(ready=False, reason=reason)

    def admission_blocker(self, capability: str, deadline_ms: int) -> str | None:
        if not self.ready:
            return self.reason or "edge health is not ready"
        status = self.capabilities.get(capability, "unavailable")
        if status != "available":
            return self.degraded_reasons.get(capability) or f"{capability} is {status}"
        if self.queue_depth >= self.capacity:
            return "edge capacity exhausted"
        if deadline_ms <= 0:
            return "task deadline cannot be met"
        return None


@dataclass(slots=True)
class EdgeTaskRequest:
    task_id: str
    job_id: str
    capability: str
    action: str
    payload: dict[str, Any]
    deadline_ms: int
    idempotency_key: str
    trace_id: str = ""
    root_trace_id: str = ""
    span_id: str = ""
    artifact_id: str = ""
    callback_url: str | None = None

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["protocol_version"] = PROTOCOL_VERSION
        data["schema_version"] = SCHEMA_VERSION
        return data


@dataclass(slots=True)
class EdgeTaskStatus:
    task_id: str
    status: EdgeTaskState
    result: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    degraded_reason: str = ""
    retry_after_ms: int = 0

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "EdgeTaskStatus":
        return cls(
            task_id=str(data.get("task_id") or ""),
            status=str(data.get("status") or "failed"),  # type: ignore[arg-type]
            result=dict(data.get("result") or {}),
            artifact_refs=list(data.get("artifact_refs") or []),
            error=str(data.get("error") or ""),
            degraded_reason=str(data.get("degraded_reason") or ""),
            retry_after_ms=int(data.get("retry_after_ms") or 0),
        )


def canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    key_id: str,
    secret: str,
    now: float | None = None,
    nonce: str = "",
) -> dict[str, str]:
    timestamp = str(int(now if now is not None else time.time()))
    nonce = nonce or hashlib.sha256(f"{timestamp}:{path}:{body.hex()}".encode()).hexdigest()[:24]
    digest = hashlib.sha256(body).hexdigest()
    signature = hmac.new(secret.encode(), _signature_base(method, path, timestamp, nonce, digest), hashlib.sha256).hexdigest()
    return {
        "X-Claw-A2A-Key-Id": key_id,
        "X-Claw-A2A-Timestamp": timestamp,
        "X-Claw-A2A-Nonce": nonce,
        "X-Claw-A2A-Body-SHA256": digest,
        "X-Claw-A2A-Signature": signature,
    }


def verify_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    secrets: dict[str, str],
    now: float | None = None,
    max_skew_seconds: int = 300,
) -> tuple[bool, str]:
    key_id = headers.get("X-Claw-A2A-Key-Id", "")
    secret = secrets.get(key_id)
    if not secret:
        return False, "unknown key id"
    timestamp = headers.get("X-Claw-A2A-Timestamp", "")
    nonce = headers.get("X-Claw-A2A-Nonce", "")
    digest = headers.get("X-Claw-A2A-Body-SHA256", "")
    signature = headers.get("X-Claw-A2A-Signature", "")
    if not timestamp or not nonce or not digest or not signature:
        return False, "missing signature headers"
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False, "invalid timestamp"
    if abs(int(now if now is not None else time.time()) - signed_at) > max_skew_seconds:
        return False, "signature expired"
    if hashlib.sha256(body).hexdigest() != digest:
        return False, "body digest mismatch"
    expected = hmac.new(secret.encode(), _signature_base(method, path, timestamp, nonce, digest), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "signature mismatch"
    return True, ""


def _signature_base(method: str, path: str, timestamp: str, nonce: str, body_hash: str) -> bytes:
    return "\n".join([method.upper(), path, timestamp, nonce, body_hash]).encode("utf-8")
