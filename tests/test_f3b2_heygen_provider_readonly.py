"""F3b.2 — HeyGenDeliver read-only live provider, fully mocked.

All tests are zero-network. They exercise the real F3b.2 adapter with
fake keychain, fake approval grants, fake DNS, and fake HTTP responses.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from claw_v2.heygen_readonly import (
    HeyGenReadOnlyAdapter,
    HeyGenReadOnlyRateLimiter,
    WHITELIST_ERROR,
    _sha256_12,
)


API_KEY = "HEYGEN-SECRET-KEY-123456"
APPROVAL_TOKEN = "approval-token-secret-abc123"
VIDEO_ID = "bf41989e378048a4bda1cf89f5cadc92"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("Network call attempted from F3b.2 test - forbidden")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(urllib.request, "urlopen", _boom)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass(slots=True)
class _Grant:
    grant_id: str = "grant-f3b2"
    metadata: dict[str, Any] | None = None


class _GrantStore:
    def __init__(self, grants: list[_Grant] | None = None) -> None:
        self.grants = (
            grants
            if grants is not None
            else [_Grant(metadata={"mode": "read_only_live", "approval_token": APPROVAL_TOKEN})]
        )

    def find_grants_for(self, *, kind: str, target: str, now: float | None = None):
        assert kind == "tool"
        assert target == "HeyGenDeliver"
        return list(self.grants)


class _Observe:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, *, payload: dict | None = None, **_kw) -> None:
        self.events.append((event_type, dict(payload or {})))


class _Response:
    def __init__(self, status: int, payload: dict, headers: dict | None = None) -> None:
        self.status = status
        self.code = status
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def getcode(self) -> int:
        return self.status


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.heygen.com/test",
        code=code,
        msg="error",
        hdrs=headers or {},
        fp=io.BytesIO(b'{"error":"nope"}'),
    )


def _adapter(
    tmp_path: Path,
    *,
    urlopen=None,
    key_reader=None,
    grants: list[_Grant] | None = None,
    dns_resolver=None,
    rate_limiter: HeyGenReadOnlyRateLimiter | None = None,
    observe: _Observe | None = None,
    allow_legacy_v1: bool = False,
) -> HeyGenReadOnlyAdapter:
    return HeyGenReadOnlyAdapter(
        workspace_root=tmp_path,
        approval_store=_GrantStore(grants),
        key_reader=key_reader or (lambda: API_KEY),
        dns_resolver=dns_resolver or (lambda host: "93.184.216.34"),
        urlopen=urlopen or (lambda *_a, **_kw: _Response(200, {"data": {"remaining_quota": 7}})),
        rate_limiter=rate_limiter or HeyGenReadOnlyRateLimiter(limit=100, clock=_Clock()),
        clock=_Clock(),
        observe=observe,
        allow_legacy_v1=allow_legacy_v1,
    )


def _artifact_json(tmp_path: Path, evidence_uri: str | None) -> dict:
    assert evidence_uri
    path = tmp_path / evidence_uri
    assert path.exists()
    return json.loads(path.read_text(encoding="utf-8"))


def test_endpoint_whitelist_rejects_post(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(tmp_path, urlopen=lambda *_a, **_kw: calls.append(True))

    with pytest.raises(ValueError, match=WHITELIST_ERROR):
        adapter.read_only_call("/v2/video/generate", method="POST")

    assert calls == []


def test_endpoint_whitelist_rejects_delete(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(tmp_path, urlopen=lambda *_a, **_kw: calls.append(True))

    with pytest.raises(ValueError, match=WHITELIST_ERROR):
        adapter.read_only_call("DELETE /v1/video/abc")

    assert calls == []


def test_endpoint_whitelist_rejects_unknown_get(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(tmp_path, urlopen=lambda *_a, **_kw: calls.append(True))

    with pytest.raises(ValueError, match=WHITELIST_ERROR):
        adapter.read_only_call("/v1/user/profile")

    assert calls == []


def test_v1_quota_endpoint_rejected_by_default(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(tmp_path, urlopen=lambda *_a, **_kw: calls.append(True))

    with pytest.raises(ValueError, match=WHITELIST_ERROR):
        adapter.read_only_call("/v1/user/remaining_quota")

    assert calls == []


def test_video_status_legacy_endpoint_rejected_by_default(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(tmp_path, urlopen=lambda *_a, **_kw: calls.append(True))

    with pytest.raises(ValueError, match=WHITELIST_ERROR):
        adapter.read_only_call("video_status", {"video_id": VIDEO_ID})

    assert calls == []


def test_preflight_blocks_when_credential_missing(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(
        tmp_path,
        key_reader=lambda: "",
        urlopen=lambda *_a, **_kw: calls.append(True),
    )

    result = adapter.read_only_call("quota")

    assert result.status == "blocked"
    assert result.reason == "credential_missing"
    assert result.evidence_uri is None
    assert calls == []


def test_preflight_blocks_when_approval_missing(tmp_path):
    observe = _Observe()
    calls: list[bool] = []
    adapter = _adapter(
        tmp_path,
        grants=[],
        urlopen=lambda *_a, **_kw: calls.append(True),
        observe=observe,
    )

    result = adapter.read_only_call("quota")

    assert result.status == "blocked"
    assert result.reason == "approval_missing"
    assert result.evidence_uri is None
    assert calls == []
    assert observe.events
    assert observe.events[0][0] == "tier3_approval_required"
    assert observe.events[0][1]["ttl_seconds"] == 600


def test_preflight_blocks_when_network_unavailable(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(
        tmp_path,
        dns_resolver=lambda host: (_ for _ in ()).throw(socket.gaierror("no dns")),
        urlopen=lambda *_a, **_kw: calls.append(True),
    )

    result = adapter.read_only_call("quota")

    assert result.status == "blocked"
    assert result.reason == "network_unavailable"
    assert result.evidence_uri is None
    assert calls == []


def test_preflight_rate_limit_returns_pending(tmp_path):
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Response(200, {"data": {"remaining_quota": 9}})

    limiter = HeyGenReadOnlyRateLimiter(limit=3, window_seconds=60, clock=_Clock())
    adapter = _adapter(tmp_path, urlopen=fake_urlopen, rate_limiter=limiter)

    results = [adapter.read_only_call("quota") for _ in range(4)]

    assert [r.status for r in results[:3]] == ["succeeded", "succeeded", "succeeded"]
    assert results[3].status == "pending_verification"
    assert results[3].reason == "local_rate_limit"
    assert results[3].evidence_uri is None
    assert len(calls) == 3


def test_quota_endpoint_success_returns_succeeded(tmp_path):
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _Response(200, {"data": {"remaining_quota": 17}})

    adapter = _adapter(
        tmp_path,
        urlopen=fake_urlopen,
    )

    result = adapter.read_only_call("quota")

    assert result.status == "succeeded"
    assert calls == ["https://api.heygen.com/v3/users/me"]
    assert result.endpoint == "GET /v3/users/me"
    assert result.response_summary == {"remaining_quota": 17, "endpoint_version": "v3"}
    artifact = _artifact_json(tmp_path, result.evidence_uri)
    assert artifact["status_code"] == 200
    assert artifact["response_summary"]["remaining_quota"] == 17
    assert artifact["endpoint"] == "GET /v3/users/me"


def test_video_status_completed_returns_succeeded(tmp_path):
    adapter = _adapter(
        tmp_path,
        allow_legacy_v1=True,
        urlopen=lambda *_a, **_kw: _Response(
            200,
            {
                "data": {
                    "video_id": VIDEO_ID,
                    "status": "completed",
                    "duration": 66.7,
                    "video_url": "https://signed.example/video.mp4?token=secret",
                }
            },
        ),
    )

    result = adapter.read_only_call("video_status", {"video_id": VIDEO_ID})

    assert result.status == "succeeded"
    assert result.response_summary["status"] == "completed"
    assert result.response_summary["video_url_present"] is True


def test_video_status_processing_returns_pending(tmp_path):
    adapter = _adapter(
        tmp_path,
        allow_legacy_v1=True,
        urlopen=lambda *_a, **_kw: _Response(
            200,
            {"data": {"video_id": VIDEO_ID, "status": "processing"}},
        ),
    )

    result = adapter.read_only_call("video_status", {"video_id": VIDEO_ID})

    assert result.status == "pending_verification"
    assert result.reason == "video_processing"


def test_401_returns_failed_auth_rejected(tmp_path):
    adapter = _adapter(
        tmp_path,
        urlopen=lambda *_a, **_kw: (_ for _ in ()).throw(_http_error(401)),
    )

    result = adapter.read_only_call("quota")

    assert result.status == "failed"
    assert result.reason == "auth_rejected"
    assert result.status_code == 401


def test_429_returns_pending_remote_rate_limit(tmp_path):
    adapter = _adapter(
        tmp_path,
        urlopen=lambda *_a, **_kw: (_ for _ in ()).throw(_http_error(429, {"Retry-After": "30"})),
    )

    result = adapter.read_only_call("quota")

    assert result.status == "pending_verification"
    assert result.reason == "remote_rate_limit"
    assert result.retry_after == "30"


def test_500_returns_pending_provider_5xx(tmp_path):
    adapter = _adapter(
        tmp_path,
        urlopen=lambda *_a, **_kw: (_ for _ in ()).throw(_http_error(500)),
    )

    result = adapter.read_only_call("quota")

    assert result.status == "pending_verification"
    assert result.reason == "provider_5xx"


def test_timeout_returns_pending_network_error(tmp_path):
    adapter = _adapter(
        tmp_path,
        urlopen=lambda *_a, **_kw: (_ for _ in ()).throw(TimeoutError("slow")),
    )

    result = adapter.read_only_call("quota")

    assert result.status == "pending_verification"
    assert result.reason == "network_error"
    assert result.evidence_uri


def test_evidence_artifact_redacts_api_key(tmp_path):
    adapter = _adapter(
        tmp_path,
        urlopen=lambda *_a, **_kw: _Response(200, {"data": {"remaining_quota": 22}}),
    )

    result = adapter.read_only_call("quota")
    artifact = _artifact_json(tmp_path, result.evidence_uri)
    serialized = json.dumps(artifact)

    assert API_KEY not in serialized
    assert "X-Api-Key" in artifact["redacted_fields"]


def test_evidence_artifact_redacts_video_url(tmp_path):
    signed_url = "https://cdn.heygen.example/video.mp4?token=secret-signed-url"
    adapter = _adapter(
        tmp_path,
        allow_legacy_v1=True,
        urlopen=lambda *_a, **_kw: _Response(
            200,
            {
                "data": {
                    "video_id": VIDEO_ID,
                    "status": "completed",
                    "video_url": signed_url,
                    "thumbnail_url": "https://cdn.heygen.example/t.jpg?signature=abc",
                }
            },
        ),
    )

    result = adapter.read_only_call("video_status", {"video_id": VIDEO_ID})
    artifact = _artifact_json(tmp_path, result.evidence_uri)
    serialized = json.dumps(artifact)

    assert signed_url not in serialized
    assert artifact["response_summary"]["video_url_present"] is True
    assert artifact["response_summary"]["thumbnail_url_present"] is True


def test_approval_token_only_fingerprinted(tmp_path):
    adapter = _adapter(
        tmp_path,
        urlopen=lambda *_a, **_kw: _Response(200, {"data": {"remaining_quota": 3}}),
    )

    result = adapter.read_only_call("quota")
    artifact = _artifact_json(tmp_path, result.evidence_uri)
    serialized = json.dumps(artifact)

    assert APPROVAL_TOKEN not in serialized
    assert artifact["approval_token_fingerprint"] == _sha256_12(APPROVAL_TOKEN)


def test_correlation_id_unique_per_call(tmp_path):
    limiter = HeyGenReadOnlyRateLimiter(limit=100, window_seconds=60, clock=_Clock())
    adapter = _adapter(
        tmp_path,
        rate_limiter=limiter,
        urlopen=lambda *_a, **_kw: _Response(200, {"data": {"remaining_quota": 5}}),
    )

    ids = {adapter.read_only_call("quota").correlation_id for _ in range(10)}

    assert len(ids) == 10


def test_post_method_blocked_even_with_approval(tmp_path):
    calls: list[bool] = []
    adapter = _adapter(tmp_path, urlopen=lambda *_a, **_kw: calls.append(True))

    with pytest.raises(ValueError, match=WHITELIST_ERROR):
        adapter.read_only_call("POST /v2/video/generate")

    assert calls == []
