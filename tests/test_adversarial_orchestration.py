from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from claw_v2.coordinator import CoordinatorService, WorkerTask
from claw_v2.orchestration import (
    OrchestrationGateError,
    OrchestrationStore,
    OrchestrationValidationError,
)


def test_missing_ack_blocks_coordinator_before_next_phase(tmp_path) -> None:
    observe = MagicMock()
    store = OrchestrationStore(tmp_path / "claw.db", observe=observe)
    router = MagicMock()
    router.ask.return_value = MagicMock(content="research result")
    coordinator = CoordinatorService(
        router=router,
        observe=observe,
        scratch_root=tmp_path / "scratch",
        max_workers=1,
        orchestration_store=store,
    )
    coordinator._orchestration_ack = MagicMock(return_value=None)

    result = coordinator.run(
        "task-missing-ack",
        "coordinate safely",
        [WorkerTask(name="research", instruction="collect facts")],
        implementation_tasks=[
            WorkerTask(name="implement", instruction="write patch", lane="worker")
        ],
    )

    assert result.error
    run = store.get_run("task-missing-ack")
    assert run is not None
    assert run.status in {"alarm", "blocked"}

    report = store.audit_report("task-missing-ack")
    assert len(report["missing_acks"]) == 1
    recorded_artifacts = [
        event for event in report["events"] if event["event_type"] == "artifact_recorded"
    ]
    assert len(recorded_artifacts) == 1
    assert report["missing_acks"][0]["artifact_id"] == recorded_artifacts[0]["artifact_id"]
    assert report["missing_acks"][0]["phase"] == "research"
    assert report["missing_acks"][0]["consumer_role"] == "coordinator_synthesis"
    assert any(event["event_type"] == "orchestration_alarm" for event in report["events"])
    assert "synthesis" not in [
        event["phase"] for event in report["events"] if event["event_type"] == "phase_started"
    ]


def test_corrupted_payload_is_rejected_without_mutating_checkpoint(tmp_path) -> None:
    store = OrchestrationStore(tmp_path / "claw.db")
    run = store.begin_run(task_id="task-corrupt", objective="reject hallucinated artifacts")
    checkpoint_id = store.checkpoint(
        run.run_id,
        phase="execution",
        reason="last_known_good",
        state={"phase": "execution", "status": "validated"},
    )
    before = store.get_run(run.run_id)
    assert before is not None

    hallucinated_worker = MagicMock(
        return_value={
            "status": "success",
            "artifact_ref": str(tmp_path / "does-not-exist.json"),
        }
    )

    with pytest.raises(OrchestrationValidationError, match="missing_artifact_ref"):
        store.record_artifact(
            run.run_id,
            phase="execution",
            artifact_type="tool_execution",
            payload=hallucinated_worker(),
            producer_role="tool_worker",
            consumer_role="coordinator",
        )

    hallucinated_worker.assert_called_once()
    after = store.get_run(run.run_id)
    assert after is not None
    assert after.version == before.version
    assert after.checkpoint_id == checkpoint_id
    report = store.audit_report(run.run_id)
    assert not [event for event in report["events"] if event["event_type"] == "artifact_recorded"]


def test_repeated_needs_improvement_triggers_orchestration_alarm(tmp_path) -> None:
    store = OrchestrationStore(tmp_path / "claw.db")
    run = store.begin_run(task_id="task-loop", objective="stop infinite refinement")
    reviewer = MagicMock(return_value={"status": "needs_improvement", "notes": "try again"})
    max_phase_attempts = 3

    for _ in range(max_phase_attempts):
        store.begin_phase(
            run.run_id,
            "verification",
            max_phase_attempts=max_phase_attempts,
        )
        artifact = store.record_artifact(
            run.run_id,
            phase="verification",
            artifact_type="verification",
            payload=reviewer(),
            producer_role="review_worker",
            consumer_role="coordinator",
        )
        store.acknowledge_artifact(artifact.artifact_id, consumer_role="coordinator")
        store.finish_phase(
            run.run_id,
            "verification",
            status="blocked",
            payload={"review_status": "needs_improvement"},
        )

    with pytest.raises(OrchestrationGateError, match="max_phase_attempts"):
        store.begin_phase(
            run.run_id,
            "verification",
            max_phase_attempts=max_phase_attempts,
        )

    assert reviewer.call_count == max_phase_attempts
    alarmed = store.get_run(run.run_id)
    assert alarmed is not None
    assert alarmed.status == "alarm"
    report = store.audit_report(run.run_id)
    alarm_events = [
        event for event in report["events"] if event["event_type"] == "orchestration_alarm"
    ]
    assert len(alarm_events) == 1
    assert alarm_events[0]["payload"]["reason"] == "max_phase_attempts_exceeded"


def test_final_success_is_rejected_without_verification_evidence(tmp_path) -> None:
    store = OrchestrationStore(tmp_path / "claw.db")
    run = store.begin_run(task_id="task-lying-agent", objective="reject false success")
    lying_agent = MagicMock(return_value={"status": "success", "summary": "done"})

    final_output = store.record_artifact(
        run.run_id,
        phase="final",
        artifact_type="final_output",
        payload=lying_agent(),
        producer_role="worker",
        consumer_role="coordinator",
    )
    store.acknowledge_artifact(final_output.artifact_id, consumer_role="coordinator")

    with pytest.raises(OrchestrationGateError, match="final gate rejected success"):
        store.complete_run(
            run.run_id,
            status="succeeded",
            reason="claimed_success",
            required_artifact_types=("verification",),
        )

    lying_agent.assert_called_once()
    terminal = store.get_run(run.run_id)
    assert terminal is not None
    assert terminal.status == "alarm"
    report = store.audit_report(run.run_id)
    assert report["run"]["status"] == "alarm"
    assert not [
        event
        for event in report["events"]
        if event["event_type"] == "run_completed" and event["payload"].get("status") == "succeeded"
    ]
    alarm_events = [
        event for event in report["events"] if event["event_type"] == "orchestration_alarm"
    ]
    assert len(alarm_events) == 1
    assert alarm_events[0]["payload"]["missing_artifact_types"] == ["verification"]
