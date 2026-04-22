from __future__ import annotations

from pathlib import Path
from typing import Any

from claw_v2.artifacts import ArtifactRecord, ArtifactStore
from claw_v2.observe_store import SQLiteObserveStore
from claw_v2.otel import TelemetrySink, build_telemetry_sink_from_env


class ObserveStream:
    def __init__(self, db_path: Path | str, *, telemetry: TelemetrySink | None = None) -> None:
        self._store = SQLiteObserveStore(db_path)
        self.db_path = self._store.db_path
        self._conn = self._store._conn
        self._lock = self._store._lock
        self.artifacts = ArtifactStore(self.db_path)
        self.telemetry = telemetry or build_telemetry_sink_from_env()
        self._telemetry_error: str | None = None

    def emit(
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
        self._store.emit(
            event_type,
            lane=lane,
            provider=provider,
            model=model,
            trace_id=trace_id,
            root_trace_id=root_trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            job_id=job_id,
            artifact_id=artifact_id,
            payload=payload,
        )
        self._emit_telemetry(
            event_type,
            lane=lane,
            provider=provider,
            model=model,
            trace_id=trace_id,
            root_trace_id=root_trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            job_id=job_id,
            artifact_id=artifact_id,
            payload=payload,
        )

    def record_artifact(self, artifact: ArtifactRecord) -> str:
        return self.artifacts.record(artifact)

    def emit_artifact(
        self,
        event_type: str,
        artifact: ArtifactRecord,
        *,
        lane: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        payload: dict | None = None,
    ) -> str:
        artifact_id = self.record_artifact(artifact)
        self.emit(
            event_type,
            lane=lane,
            provider=provider,
            model=model,
            trace_id=artifact.trace_id,
            root_trace_id=artifact.root_trace_id,
            span_id=artifact.span_id,
            parent_span_id=artifact.parent_span_id,
            job_id=artifact.job_id,
            artifact_id=artifact_id,
            payload={**artifact.event_payload(), **(payload or {})},
        )
        return artifact_id

    def recent_artifacts(self, *, limit: int = 20, artifact_type: str | None = None) -> list[ArtifactRecord]:
        return self.artifacts.recent(limit=limit, artifact_type=artifact_type)

    def trace_artifacts(self, trace_id: str) -> list[ArtifactRecord]:
        return self.artifacts.trace_artifacts(trace_id)

    def artifact_lineage(self, artifact_id: str) -> list[ArtifactRecord]:
        return self.artifacts.lineage(artifact_id)

    def cache_summary(self, hours: int = 24) -> dict:
        return self._store.cache_summary(hours=hours)

    def total_cost_today(self) -> float:
        return self._store.total_cost_today()

    def cost_per_agent_today(self) -> dict[str, float]:
        return self._store.cost_per_agent_today()

    def spending_today(self) -> dict:
        return self._store.spending_today()

    def recent_events(self, limit: int = 20) -> list[dict]:
        return self._store.recent_events(limit=limit)

    def trace_events(self, trace_id: str, *, limit: int | None = None) -> list[dict]:
        return self._store.trace_events(trace_id, limit=limit)

    def telemetry_status(self) -> dict[str, Any]:
        status = self.telemetry.status()
        if self._telemetry_error:
            status = {**status, "last_error": self._telemetry_error}
        return status

    def _emit_telemetry(self, event_type: str, **kwargs: Any) -> None:
        try:
            self.telemetry.emit_event(event_type, **kwargs)
        except Exception as exc:
            self._telemetry_error = f"{exc.__class__.__name__}: {str(exc)[:200]}"
