"""F3b.2 HeyGen read-only/status-only provider.

This adapter is intentionally narrower than ``HeygenDeliveryService``:
it can inspect quota, video status, and recent videos, but it cannot
generate, delete, download, publish, or deliver anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

BASE_URL = "https://api.heygen.com"
KEYCHAIN_SERVICE = "heygen_api_key"
PHASE = "F3b.2"
APPROVAL_TTL_SECONDS = 600
READ_ONLY_MODE = "read_only_live"
READ_ONLY_TOOL = "HeyGenDeliver"
WHITELIST_ERROR = "F3b.2: endpoint not in read-only whitelist"

SENSITIVE_FIELD_RE = re.compile(r"(?i)(secret|token|key|password|signature|signed)")

_READ_ONLY_WHITELIST: dict[str, frozenset[str]] = {
    "/v3/users/me": frozenset(),
}

_LEGACY_READ_ONLY_WHITELIST: dict[str, frozenset[str]] = {
    "/v1/user/remaining_quota": frozenset(),
    "/v1/video_status.get": frozenset({"video_id"}),
    "/v1/video.list": frozenset({"limit", "offset"}),
}

ENDPOINT_ALIASES: dict[str, str] = {
    "quota": "/v3/users/me",
    "remaining_quota": "/v3/users/me",
    "video_status": "/v1/video_status.get",
    "status": "/v1/video_status.get",
    "video_list": "/v1/video.list",
    "list": "/v1/video.list",
}

REDACTED_FIELDS = (
    "X-Api-Key",
    "video_url",
    "gif_url",
    "thumbnail_url",
    "callback_url",
    "webhook_url",
    "approval_token",
)


@dataclass(slots=True)
class ProviderObservation:
    endpoint: str
    params: dict[str, Any]
    status: str
    reason: str
    status_code: int | None = None
    latency_ms: int | None = None
    response_summary: dict[str, Any] | None = None
    evidence_uri: str | None = None
    timestamp_iso: str | None = None
    correlation_id: str | None = None
    retry_after: str | None = None
    preflight_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "phase": PHASE,
            "endpoint": self.endpoint,
            "params": dict(self.params),
            "status": self.status,
            "reason": self.reason,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "response_summary": dict(self.response_summary or {}),
            "evidence_uri": self.evidence_uri,
            "timestamp_iso": self.timestamp_iso,
            "correlation_id": self.correlation_id,
            "preflight_passed": self.preflight_passed,
        }
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


@dataclass(slots=True)
class _PreflightResult:
    ok: bool
    status: str
    reason: str
    api_key: str = ""
    approval_fingerprint: str | None = None


class HeyGenReadOnlyRateLimiter:
    """Small rolling-window limiter for F3b.2 live GET calls."""

    def __init__(
        self,
        *,
        limit: int = 3,
        window_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock or time.time
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        now = float(self.clock())
        cutoff = now - self.window_seconds
        self._timestamps = [ts for ts in self._timestamps if ts > cutoff]
        if len(self._timestamps) >= self.limit:
            return False
        self._timestamps.append(now)
        return True

    def reset(self) -> None:
        self._timestamps.clear()


_GLOBAL_RATE_LIMITER = HeyGenReadOnlyRateLimiter()


def read_f3b2_heygen_api_key() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def _sha256_12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _timestamp_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _single_query_values(query: str) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _redact_params(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if SENSITIVE_FIELD_RE.search(str(key)):
            out[str(key)] = "REDACTED"
        else:
            out[str(key)] = value
    return out


def _json_loads(raw: bytes) -> dict[str, Any] | list[Any]:
    decoded = raw.decode("utf-8", errors="replace")
    parsed = json.loads(decoded)
    if not isinstance(parsed, (dict, list)):
        raise ValueError("json root must be object or list")
    return parsed


def _headers_get(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        value = None
    return str(value) if value is not None else None


def _data_object(payload: dict[str, Any] | list[Any]) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list)):
        return payload["data"]
    return payload


def _first_present(source: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in source:
            return source[name]
    return None


def _summarize_response(
    *,
    path: str,
    params: Mapping[str, Any],
    payload: dict[str, Any] | list[Any],
) -> dict[str, Any]:
    data = _data_object(payload)
    if path in {"/v3/users/me", "/v1/user/remaining_quota"}:
        source = data if isinstance(data, Mapping) else {}
        remaining = _first_present(
            source,
            (
                "remaining_quota",
                "remaining_credits",
                "credits",
                "quota",
                "remaining_quota_seconds",
            ),
        )
        summary = {"remaining_quota": remaining}
        if path == "/v3/users/me":
            summary["endpoint_version"] = "v3"
        return summary

    if path == "/v1/video_status.get":
        source = data if isinstance(data, Mapping) else {}
        duration = _first_present(source, ("duration", "duration_seconds"))
        summary: dict[str, Any] = {
            "video_id": source.get("video_id") or params.get("video_id"),
            "status": source.get("status"),
            "duration_seconds": duration,
            "video_url_present": bool(source.get("video_url")),
            "gif_url_present": bool(source.get("gif_url")),
            "thumbnail_url_present": bool(source.get("thumbnail_url")),
        }
        error = source.get("error")
        if error:
            summary["error_present"] = True
        return summary

    if path == "/v1/video.list":
        if isinstance(data, Mapping):
            videos = data.get("videos") or data.get("items") or []
        else:
            videos = data
        if not isinstance(videos, list):
            videos = []
        selected: list[dict[str, Any]] = []
        for item in videos[: _safe_int(params.get("limit"), 5)]:
            if not isinstance(item, Mapping):
                continue
            selected.append(
                {
                    "video_id": item.get("video_id") or item.get("id"),
                    "status": item.get("status"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                    "video_url_present": bool(item.get("video_url")),
                    "thumbnail_url_present": bool(item.get("thumbnail_url")),
                }
            )
        return {"count": len(videos), "videos": selected}

    return {}


def _status_for_http_result(
    *,
    path: str,
    status_code: int,
    summary: Mapping[str, Any],
) -> tuple[str, str]:
    if status_code == 200:
        if path == "/v1/video_status.get":
            provider_status = str(summary.get("status") or "").lower()
            if provider_status in {"processing", "pending", "waiting", "queued"}:
                return "pending_verification", "video_processing"
            if provider_status == "failed":
                return "failed", "provider_video_failed"
        return "succeeded", "ok"
    if status_code in {401, 403}:
        return "failed", "auth_rejected"
    if status_code == 429:
        return "pending_verification", "remote_rate_limit"
    if status_code >= 500:
        return "pending_verification", "provider_5xx"
    if status_code == 404:
        return "failed", "resource_not_found"
    if status_code == 400:
        return "failed", "bad_request"
    if 400 <= status_code < 500:
        return "failed", "provider_4xx"
    return "pending_verification", "provider_unexpected_status"


class HeyGenReadOnlyAdapter:
    def __init__(
        self,
        *,
        workspace_root: Path | str | None = None,
        evidence_root: Path | str | None = None,
        db_path: Path | str | None = None,
        observe: Any | None = None,
        approval_store: Any | None = None,
        key_reader: Callable[[], str] = read_f3b2_heygen_api_key,
        dns_resolver: Callable[[str], Any] = socket.gethostbyname,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        rate_limiter: HeyGenReadOnlyRateLimiter | None = None,
        clock: Callable[[], float] | None = None,
        allow_legacy_v1: bool = False,
        runtime_db: Any | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root or Path.cwd()).resolve()
        self.evidence_root = Path(
            evidence_root or self.workspace_root / "artifacts" / "verification" / "f3b2"
        )
        self.db_path = Path(db_path or os.getenv("DB_PATH", "data/claw.db"))
        self.observe = observe
        self.approval_store = approval_store
        # F1.1a1: when provided (production, via the tool registry), the lazily
        # built CapabilityGrantStore shares this single RuntimeDb connection
        # instead of opening its own claw.db connection (second-writer bug).
        self.runtime_db = runtime_db
        self.key_reader = key_reader
        self.dns_resolver = dns_resolver
        self.urlopen = urlopen
        self.rate_limiter = rate_limiter or _GLOBAL_RATE_LIMITER
        self.clock = clock or time.time
        self.allow_legacy_v1 = allow_legacy_v1

    def read_only_call(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        method: str = "GET",
    ) -> ProviderObservation:
        request_method, path, normalized_params = self._normalize_request(
            endpoint=endpoint,
            params=params or {},
            method=method,
        )
        whitelist = self._active_whitelist()
        if request_method != "GET" or path not in whitelist:
            raise ValueError(WHITELIST_ERROR)
        self._validate_params(path, normalized_params, whitelist)

        display_endpoint = f"GET {path}"
        safe_params = _redact_params(normalized_params)
        correlation_id = f"f3b2-{uuid.uuid4().hex}"
        timestamp_iso = _timestamp_iso()
        preflight = self._preflight()
        if not preflight.ok:
            return ProviderObservation(
                endpoint=display_endpoint,
                params=safe_params,
                status=preflight.status,
                reason=preflight.reason,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                preflight_passed=False,
            )

        url = self._build_url(path, normalized_params)
        request = urllib.request.Request(
            url,
            headers={"X-Api-Key": preflight.api_key},
            method="GET",
        )
        started = float(self.clock())
        try:
            with self.urlopen(request, timeout=10) as response:
                raw = response.read()
                status_code = int(
                    getattr(response, "status", 0)
                    or getattr(response, "code", 0)
                    or response.getcode()
                )
                payload = _json_loads(raw)
                latency_ms = self._latency_ms(started)
        except urllib.error.HTTPError as exc:
            latency_ms = self._latency_ms(started)
            status_code = int(exc.code)
            retry_after = _headers_get(exc.headers, "Retry-After")
            status, reason = _status_for_http_result(
                path=path,
                status_code=status_code,
                summary={},
            )
            summary = {"http_error": status_code}
            evidence_uri = self._write_evidence(
                endpoint=display_endpoint,
                params=safe_params,
                status=status,
                reason=reason,
                status_code=status_code,
                latency_ms=latency_ms,
                response_summary=summary,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                approval_fingerprint=preflight.approval_fingerprint,
            )
            return ProviderObservation(
                endpoint=display_endpoint,
                params=safe_params,
                status=status,
                reason=reason,
                status_code=status_code,
                latency_ms=latency_ms,
                response_summary=summary,
                evidence_uri=evidence_uri,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                retry_after=retry_after,
                preflight_passed=True,
            )
        except (TimeoutError, socket.timeout, urllib.error.URLError, OSError):
            latency_ms = self._latency_ms(started)
            summary = {"network_error": True}
            evidence_uri = self._write_evidence(
                endpoint=display_endpoint,
                params=safe_params,
                status="pending_verification",
                reason="network_error",
                status_code=None,
                latency_ms=latency_ms,
                response_summary=summary,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                approval_fingerprint=preflight.approval_fingerprint,
            )
            return ProviderObservation(
                endpoint=display_endpoint,
                params=safe_params,
                status="pending_verification",
                reason="network_error",
                status_code=None,
                latency_ms=latency_ms,
                response_summary=summary,
                evidence_uri=evidence_uri,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                preflight_passed=True,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            latency_ms = self._latency_ms(started)
            summary = {"parseable": False}
            evidence_uri = self._write_evidence(
                endpoint=display_endpoint,
                params=safe_params,
                status="failed",
                reason="payload_not_parseable",
                status_code=None,
                latency_ms=latency_ms,
                response_summary=summary,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                approval_fingerprint=preflight.approval_fingerprint,
            )
            return ProviderObservation(
                endpoint=display_endpoint,
                params=safe_params,
                status="failed",
                reason="payload_not_parseable",
                status_code=None,
                latency_ms=latency_ms,
                response_summary=summary,
                evidence_uri=evidence_uri,
                timestamp_iso=timestamp_iso,
                correlation_id=correlation_id,
                preflight_passed=True,
            )

        summary = _summarize_response(
            path=path,
            params=normalized_params,
            payload=payload,
        )
        status, reason = _status_for_http_result(
            path=path,
            status_code=status_code,
            summary=summary,
        )
        evidence_uri = self._write_evidence(
            endpoint=display_endpoint,
            params=safe_params,
            status=status,
            reason=reason,
            status_code=status_code,
            latency_ms=latency_ms,
            response_summary=summary,
            timestamp_iso=timestamp_iso,
            correlation_id=correlation_id,
            approval_fingerprint=preflight.approval_fingerprint,
        )
        return ProviderObservation(
            endpoint=display_endpoint,
            params=safe_params,
            status=status,
            reason=reason,
            status_code=status_code,
            latency_ms=latency_ms,
            response_summary=summary,
            evidence_uri=evidence_uri,
            timestamp_iso=timestamp_iso,
            correlation_id=correlation_id,
            preflight_passed=True,
        )

    def _normalize_request(
        self,
        *,
        endpoint: str,
        params: Mapping[str, Any],
        method: str,
    ) -> tuple[str, str, dict[str, Any]]:
        raw = str(endpoint or "").strip()
        request_method = str(method or "GET").upper()
        if " " in raw:
            maybe_method, maybe_endpoint = raw.split(None, 1)
            if maybe_method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                request_method = maybe_method.upper()
                raw = maybe_endpoint.strip()
        raw = ENDPOINT_ALIASES.get(raw, raw)
        parsed = urllib.parse.urlsplit(raw)
        path = ENDPOINT_ALIASES.get(parsed.path, parsed.path)
        normalized = _single_query_values(parsed.query)
        normalized.update(dict(params))
        if path == "/v1/video.list":
            limit = max(1, min(_safe_int(normalized.get("limit"), 5), 20))
            normalized["limit"] = limit
            normalized["offset"] = _safe_int(normalized.get("offset"), 0)
        return request_method, path, normalized

    def _active_whitelist(self) -> dict[str, frozenset[str]]:
        if not self.allow_legacy_v1:
            return dict(_READ_ONLY_WHITELIST)
        return {**_READ_ONLY_WHITELIST, **_LEGACY_READ_ONLY_WHITELIST}

    def _validate_params(
        self,
        path: str,
        params: Mapping[str, Any],
        whitelist: Mapping[str, frozenset[str]],
    ) -> None:
        allowed = whitelist[path]
        if set(params) - set(allowed):
            raise ValueError(WHITELIST_ERROR)
        if path == "/v1/video_status.get" and not str(params.get("video_id") or "").strip():
            raise ValueError("video_id is required for F3b.2 video status")
        if path in {"/v3/users/me", "/v1/user/remaining_quota"} and params:
            raise ValueError(WHITELIST_ERROR)

    def _build_url(self, path: str, params: Mapping[str, Any]) -> str:
        query = urllib.parse.urlencode(params)
        return f"{BASE_URL}{path}" + (f"?{query}" if query else "")

    def _preflight(self) -> _PreflightResult:
        api_key = ""
        try:
            api_key = str(self.key_reader() or "").strip()
        except Exception:
            api_key = ""
        if not api_key:
            return _PreflightResult(False, "blocked", "credential_missing")

        approval_fingerprint = self._active_approval_fingerprint()
        if approval_fingerprint is None:
            self._emit_approval_required()
            return _PreflightResult(False, "blocked", "approval_missing")

        try:
            self.dns_resolver("api.heygen.com")
        except Exception:
            return _PreflightResult(False, "blocked", "network_unavailable")

        if not self.rate_limiter.allow():
            return _PreflightResult(
                False,
                "pending_verification",
                "local_rate_limit",
                approval_fingerprint=approval_fingerprint,
            )
        return _PreflightResult(
            True,
            "succeeded",
            "ok",
            api_key=api_key,
            approval_fingerprint=approval_fingerprint,
        )

    def _active_approval_fingerprint(self) -> str | None:
        store = self.approval_store
        if store is None:
            from claw_v2.capability_grants import CapabilityGrantStore

            store = CapabilityGrantStore(
                self.db_path, observe=self.observe, runtime_db=self.runtime_db
            )
        now = float(self.clock())
        try:
            grants = store.find_grants_for(kind="tool", target=READ_ONLY_TOOL, now=now)
        except TypeError:
            grants = store.find_grants_for(kind="tool", target=READ_ONLY_TOOL)
        except AttributeError:
            grants = []
        for grant in grants:
            metadata = getattr(grant, "metadata", None) or {}
            if not isinstance(metadata, Mapping):
                metadata = {}
            if metadata.get("mode") != READ_ONLY_MODE:
                continue
            fingerprint = metadata.get("approval_token_fingerprint")
            if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{12}", fingerprint):
                return fingerprint
            token = metadata.get("approval_token") or metadata.get("token")
            source = str(token or getattr(grant, "grant_id", ""))
            if source:
                return _sha256_12(source)
        return None

    def _emit_approval_required(self) -> None:
        if self.observe is None:
            return
        payload = {
            "tool": READ_ONLY_TOOL,
            "mode": READ_ONLY_MODE,
            "ttl_seconds": APPROVAL_TTL_SECONDS,
            "status": "blocked",
            "reason": "approval_missing",
        }
        try:
            self.observe.emit("tier3_approval_required", payload=payload)
        except Exception:
            return

    def _latency_ms(self, started: float) -> int:
        elapsed = max(0.0, float(self.clock()) - started)
        return int(round(elapsed * 1000))

    def _write_evidence(
        self,
        *,
        endpoint: str,
        params: Mapping[str, Any],
        status: str,
        reason: str,
        status_code: int | None,
        latency_ms: int,
        response_summary: Mapping[str, Any],
        timestamp_iso: str,
        correlation_id: str,
        approval_fingerprint: str | None,
    ) -> str:
        filename = f"{_timestamp_for_filename()}_{correlation_id}.json"
        path = self.evidence_root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            evidence_uri = str(path.relative_to(self.workspace_root))
        except ValueError:
            evidence_uri = str(path)
        payload = {
            "phase": PHASE,
            "endpoint": endpoint,
            "params": dict(params),
            "status": status,
            "reason": reason,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "response_summary": self._safe_summary(response_summary),
            "redacted_fields": list(REDACTED_FIELDS),
            "timestamp_iso": timestamp_iso,
            "correlation_id": correlation_id,
            "approval_token_fingerprint": approval_fingerprint,
        }
        self._assert_no_forbidden_values(payload)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return evidence_uri

    def _safe_summary(self, value: Mapping[str, Any]) -> dict[str, Any]:
        def scrub(item: Any) -> Any:
            if isinstance(item, Mapping):
                out: dict[str, Any] = {}
                for key, nested in item.items():
                    key_str = str(key)
                    if key_str in {"video_url", "gif_url", "thumbnail_url"}:
                        out[f"{key_str}_present"] = bool(nested)
                        continue
                    if key_str in {"callback_url", "webhook_url"}:
                        out[f"{key_str}_present"] = bool(nested)
                        continue
                    if SENSITIVE_FIELD_RE.search(key_str):
                        out[key_str] = "REDACTED"
                        continue
                    out[key_str] = scrub(nested)
                return out
            if isinstance(item, list):
                return [scrub(nested) for nested in item]
            if isinstance(item, str):
                return "REDACTED" if SENSITIVE_FIELD_RE.search(item) else item
            return item

        scrubbed = scrub(dict(value))
        return scrubbed if isinstance(scrubbed, dict) else {}

    def _assert_no_forbidden_values(self, payload: Mapping[str, Any]) -> None:
        serialized = json.dumps(payload, sort_keys=True, default=str)
        try:
            api_key = str(self.key_reader() or "").strip()
        except Exception:
            api_key = ""
        if api_key and api_key in serialized:
            raise RuntimeError("F3b.2 evidence redaction failed")
