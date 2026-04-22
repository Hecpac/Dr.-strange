from __future__ import annotations

import pytest

from claw_v2.edge_protocol import (
    EdgeHealth,
    EdgeIdentity,
    EdgeTaskRequest,
    canonical_json,
    sign_headers,
    verify_headers,
)


def test_a2a_hmac_signature_round_trips_and_detects_tampering() -> None:
    body = canonical_json({"task": "click", "id": "t1"})
    headers = sign_headers(
        method="POST",
        path="/a2a/v1/tasks",
        body=body,
        key_id="core",
        secret="secret",
        now=1000,
        nonce="n1",
    )

    ok, reason = verify_headers(
        method="POST",
        path="/a2a/v1/tasks",
        body=body,
        headers=headers,
        secrets={"core": "secret"},
        now=1000,
    )
    assert ok
    assert reason == ""

    ok, reason = verify_headers(
        method="POST",
        path="/a2a/v1/tasks",
        body=canonical_json({"task": "other"}),
        headers=headers,
        secrets={"core": "secret"},
        now=1000,
    )
    assert not ok
    assert reason == "body digest mismatch"


def test_a2a_hmac_rejects_expired_signature() -> None:
    body = canonical_json({"task": "click"})
    headers = sign_headers(method="POST", path="/a2a/v1/tasks", body=body, key_id="core", secret="secret", now=1000)

    ok, reason = verify_headers(
        method="POST",
        path="/a2a/v1/tasks",
        body=body,
        headers=headers,
        secrets={"core": "secret"},
        now=2000,
    )

    assert not ok
    assert reason == "signature expired"


def test_edge_identity_requires_approved_connectivity_layer() -> None:
    identity = EdgeIdentity.from_mapping(
        {
            "edge_id": "mac-edge",
            "endpoint": "https://mac.tailnet.ts.net",
            "capabilities": ["computer_use"],
            "key_id": "edge",
            "connectivity_layer": "tailscale",
        }
    )

    assert identity.connectivity_layer == "tailscale"

    with pytest.raises(ValueError, match="unapproved connectivity layer"):
        EdgeIdentity.from_mapping(
            {
                "edge_id": "mac-edge",
                "endpoint": "https://public-dyndns.example.com",
                "capabilities": [],
                "key_id": "edge",
                "connectivity_layer": "dynamic_dns",
            }
        )


def test_task_payload_carries_callback_and_versions() -> None:
    request = EdgeTaskRequest(
        task_id="edge-task-1",
        job_id="job-1",
        capability="computer_use",
        action="click",
        payload={"target": "Save"},
        deadline_ms=30_000,
        idempotency_key="job-1:click",
        callback_url="https://core/a2a/v1/callbacks/edge-task-1",
    )

    payload = request.to_payload()

    assert payload["protocol_version"] == "a2a/1"
    assert payload["schema_version"] == 1
    assert payload["callback_url"] == "https://core/a2a/v1/callbacks/edge-task-1"


def test_edge_health_blocks_dispatch_when_not_ready_or_over_capacity() -> None:
    assert EdgeHealth.unavailable("mac asleep").admission_blocker("computer_use", 1000) == "mac asleep"

    health = EdgeHealth(
        ready=True,
        capabilities={"computer_use": "available"},
        queue_depth=2,
        capacity=2,
        retry_after_ms=5000,
    )

    assert health.admission_blocker("computer_use", 1000) == "edge capacity exhausted"


def test_edge_health_reports_capability_degraded_reason() -> None:
    health = EdgeHealth(
        ready=True,
        capabilities={"computer_use": "degraded"},
        degraded_reasons={"computer_use": "screen locked"},
    )

    assert health.admission_blocker("computer_use", 1000) == "screen locked"
