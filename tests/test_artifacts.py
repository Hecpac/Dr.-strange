from __future__ import annotations

import sqlite3
from pathlib import Path

from claw_v2.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    ArtifactStore,
    ExecutionArtifact,
    JobArtifact,
    PlanArtifact,
    VerificationArtifact,
)
from claw_v2.observe import ObserveStream


def test_typed_artifacts_generate_ids_and_event_payloads() -> None:
    artifact = PlanArtifact(
        summary="Ship durable jobs",
        trace_id="trace-1",
        payload={"steps": ["capture", "resume"]},
    )

    assert artifact.artifact_id.startswith("plan:")
    assert artifact.artifact_type == "plan"
    assert artifact.event_payload() == {
        "artifact_type": "plan",
        "summary": "Ship durable jobs",
        "steps": ["capture", "resume"],
    }


def test_artifact_store_persists_lineage_and_typed_roundtrip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "claw.db")
    plan = PlanArtifact(summary="Plan", trace_id="trace-1", root_trace_id="trace-1")
    verification = VerificationArtifact(
        summary="Verifier approved",
        trace_id="trace-1",
        root_trace_id="trace-1",
        parent_artifact_id=plan.artifact_id,
        payload={"recommendation": "approve"},
    )
    execution = ExecutionArtifact(
        summary="Executed",
        trace_id="trace-1",
        root_trace_id="trace-1",
        parent_artifact_id=verification.artifact_id,
        job_id="job-1",
        payload={"status": "executed"},
    )

    for artifact in (plan, verification, execution):
        store.record(artifact)

    assert store.get(plan.artifact_id) == plan
    assert [item.artifact_id for item in store.lineage(execution.artifact_id)] == [
        plan.artifact_id,
        verification.artifact_id,
        execution.artifact_id,
    ]
    assert [item.artifact_type for item in store.trace_artifacts("trace-1")] == [
        "plan",
        "verification",
        "execution",
    ]
    assert store.recent(artifact_type="verification")[0].payload["recommendation"] == "approve"


def test_observe_stream_can_emit_and_query_artifact_events(tmp_path: Path) -> None:
    observe = ObserveStream(tmp_path / "observe.db")
    artifact = JobArtifact(
        summary="Pipeline HEC-1 queued",
        trace_id="trace-1",
        root_trace_id="trace-1",
        span_id="span-1",
        job_id="job-1",
        payload={"state": "queued"},
    )

    artifact_id = observe.emit_artifact("job_queued", artifact, lane="worker", payload={"source": "test"})

    events = observe.trace_events("trace-1")
    assert events[0]["event_type"] == "job_queued"
    assert events[0]["artifact_id"] == artifact_id
    assert events[0]["payload"]["artifact_type"] == "job"
    assert events[0]["payload"]["state"] == "queued"
    assert events[0]["payload"]["source"] == "test"
    assert observe.recent_artifacts(limit=1)[0].artifact_id == artifact_id
    assert observe.trace_artifacts("trace-1")[0].job_id == "job-1"


def test_artifact_store_schema_is_created_on_existing_observe_db(tmp_path: Path) -> None:
    db_path = tmp_path / "claw.db"
    observe = ObserveStream(db_path)
    observe.emit("startup")

    conn = sqlite3.connect(db_path)
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='artifacts'"
    ).fetchone()

    assert table is not None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == ARTIFACT_SCHEMA_VERSION


def test_artifact_store_does_not_downgrade_future_schema_version(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version={ARTIFACT_SCHEMA_VERSION + 10}")
    conn.close()

    store = ArtifactStore(db_path)

    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == ARTIFACT_SCHEMA_VERSION + 10
