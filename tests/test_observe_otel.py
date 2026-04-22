from __future__ import annotations

from pathlib import Path
from typing import Any

from claw_v2.observe import ObserveStream
from claw_v2.otel import InMemoryTelemetrySink, NoopTelemetrySink, build_telemetry_sink_from_env, redact_payload


def test_observe_stream_dual_writes_sqlite_and_telemetry(tmp_path: Path) -> None:
    telemetry = InMemoryTelemetrySink()
    observe = ObserveStream(tmp_path / "claw.db", telemetry=telemetry)

    observe.emit(
        "llm_response",
        lane="brain",
        provider="anthropic",
        model="claude",
        trace_id="trace-1",
        payload={"cost_estimate": 0.2, "api_key": "sk-secret"},
    )

    event = observe.recent_events(limit=1)[0]
    telemetry_event = telemetry.events[0]
    assert event["event_type"] == "llm_response"
    assert event["payload"]["api_key"] == "sk-secret"
    assert telemetry_event["event_type"] == "llm_response"
    assert telemetry_event["payload"]["api_key"] == "[redacted]"


def test_observe_stream_fails_open_when_telemetry_sink_raises(tmp_path: Path) -> None:
    observe = ObserveStream(tmp_path / "claw.db", telemetry=_FailingTelemetry())

    observe.emit("startup", payload={"ok": True})

    assert observe.recent_events(limit=1)[0]["event_type"] == "startup"
    assert "RuntimeError" in observe.telemetry_status()["last_error"]


def test_build_telemetry_sink_from_env_defaults_to_noop() -> None:
    sink = build_telemetry_sink_from_env({})

    assert isinstance(sink, NoopTelemetrySink)
    assert sink.status()["enabled"] is False


def test_redact_payload_recurses_through_sensitive_keys() -> None:
    payload = {"token": "abc", "nested": {"password": "pw", "safe": "value"}}

    assert redact_payload(payload) == {
        "token": "[redacted]",
        "nested": {"password": "[redacted]", "safe": "value"},
    }


class _FailingTelemetry:
    def emit_event(self, event_type: str, **kwargs: Any) -> None:
        raise RuntimeError("collector down")

    def status(self) -> dict[str, Any]:
        return {"enabled": True, "sink": "failing"}
