from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


TRUTHY = {"1", "true", "yes", "on"}
SENSITIVE_KEY_PARTS = ("api_key", "token", "secret", "password", "authorization")


class TelemetrySink(Protocol):
    def emit_event(
        self,
        event_type: str,
        *,
        lane: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        trace_id: str | None = None,
        root_trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        job_id: str | None = None,
        artifact_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        ...

    def status(self) -> dict[str, Any]:
        ...


@dataclass(slots=True)
class NoopTelemetrySink:
    reason: str = "disabled"

    def emit_event(self, event_type: str, **kwargs: Any) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {"enabled": False, "sink": "noop", "reason": self.reason}


@dataclass(slots=True)
class InMemoryTelemetrySink:
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit_event(self, event_type: str, **kwargs: Any) -> None:
        payload = kwargs.pop("payload", None)
        self.events.append({"event_type": event_type, **kwargs, "payload": redact_payload(payload or {})})

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "sink": "memory", "events": len(self.events)}


class OpenTelemetrySink:
    def __init__(self, *, service_name: str = "claw-v3", endpoint: str | None = None) -> None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        self.service_name = service_name
        self.endpoint = endpoint
        self._provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        self._provider.add_span_processor(BatchSpanProcessor(exporter))
        self._tracer = self._provider.get_tracer("claw_v2.observe")

    def emit_event(self, event_type: str, **kwargs: Any) -> None:
        attributes = otel_attributes(event_type, kwargs)
        with self._tracer.start_as_current_span(f"claw.{event_type}") as span:
            for key, value in attributes.items():
                span.set_attribute(key, value)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "sink": "opentelemetry",
            "service_name": self.service_name,
            "endpoint": self.endpoint,
        }

    def shutdown(self) -> None:
        self._provider.shutdown()


def build_telemetry_sink_from_env(env: dict[str, str] | None = None) -> TelemetrySink:
    env = env or os.environ
    enabled = env.get("CLAW_OTEL_ENABLED", "").strip().lower() in TRUTHY
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or env.get("CLAW_OTEL_EXPORTER_OTLP_ENDPOINT")
    if not enabled and not endpoint:
        return NoopTelemetrySink()
    service_name = env.get("OTEL_SERVICE_NAME") or env.get("CLAW_OTEL_SERVICE_NAME") or "claw-v3"
    try:
        return OpenTelemetrySink(service_name=service_name, endpoint=endpoint)
    except Exception as exc:
        return NoopTelemetrySink(reason=f"unavailable:{exc.__class__.__name__}")


def otel_attributes(event_type: str, values: dict[str, Any]) -> dict[str, Any]:
    payload = redact_payload(values.get("payload") or {})
    attrs: dict[str, Any] = {
        "claw.event_type": event_type,
        "claw.payload_json": json.dumps(payload, sort_keys=True, default=str)[:2000],
    }
    for key in ("lane", "provider", "model", "trace_id", "root_trace_id", "span_id", "parent_span_id", "job_id", "artifact_id"):
        value = values.get(key)
        if value is not None:
            attrs[f"claw.{key}"] = str(value)
    for key, value in payload.items():
        attr = _attribute_value(value)
        if attr is not None:
            attrs[f"claw.payload.{key}"] = attr
    return attrs


def redact_payload(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): redact_payload(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(item, key=key) for item in value[:20]]
    if isinstance(value, tuple):
        return [redact_payload(item, key=key) for item in value[:20]]
    return value


def _attribute_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (bool, int, float, str)):
        return value if not isinstance(value, str) else value[:500]
    return None


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_PARTS)
