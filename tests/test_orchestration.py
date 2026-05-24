from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from claw_v2.coordinator import CoordinatorService, WorkerTask
from claw_v2.orchestration import (
    ACK_ENVELOPE_SCHEMA,
    ARTIFACT_ENVELOPE_SCHEMA,
    OrchestrationStore,
    OrchestrationValidationError,
    OrchestrationVersionConflict,
)


class OrchestrationSchemaTests(unittest.TestCase):
    def test_envelope_schemas_are_strict(self) -> None:
        self.assertEqual(ARTIFACT_ENVELOPE_SCHEMA["additionalProperties"], False)
        self.assertEqual(ACK_ENVELOPE_SCHEMA["additionalProperties"], False)
        self.assertIn("payload_sha256", ARTIFACT_ENVELOPE_SCHEMA["required"])
        self.assertIn("expected_artifact_schema", ACK_ENVELOPE_SCHEMA["required"])


class OrchestrationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.store = OrchestrationStore(self.tmp / "claw.db")

    def test_run_phase_and_version_conflict(self) -> None:
        run = self.store.begin_run(task_id="task-1", objective="ship the thing")
        self.assertEqual(run.version, 1)

        phase = self.store.begin_phase(run.run_id, "planning", expected_version=1)
        self.assertEqual(phase.version, 2)
        self.assertEqual(phase.current_phase, "planning")

        with self.assertRaises(OrchestrationVersionConflict):
            self.store.begin_phase(run.run_id, "execution", expected_version=1)

    def test_artifact_requires_object_payload(self) -> None:
        run = self.store.begin_run(task_id="task-1", objective="ship")
        with self.assertRaises(OrchestrationValidationError):
            self.store.record_artifact(
                run.run_id,
                phase="planning",
                artifact_type="bad",
                payload=[],  # type: ignore[arg-type]
                producer_role="planner",
                consumer_role="executor",
            )

    def test_artifact_ack_and_audit_report(self) -> None:
        run = self.store.begin_run(task_id="task-1", objective="ship")
        artifact = self.store.record_artifact(
            run.run_id,
            phase="planning",
            artifact_type="plan",
            payload={"steps": ["inspect", "patch"]},
            producer_role="planner",
            consumer_role="executor",
        )
        self.assertEqual(artifact.schema_version, "orchestration_artifact.v1")
        self.assertEqual(len(artifact.payload_sha256), 64)

        ack = self.store.acknowledge_artifact(
            artifact.artifact_id,
            consumer_role="executor",
        )
        self.assertEqual(ack.status, "received")

        report = self.store.audit_report(run.run_id)
        self.assertEqual(report["missing_acks"], [])
        self.assertGreaterEqual(report["event_count"], 3)
        self.assertTrue(report["gaps"])

    def test_wrong_consumer_ack_is_rejected_and_still_missing(self) -> None:
        run = self.store.begin_run(task_id="task-1", objective="ship")
        artifact = self.store.record_artifact(
            run.run_id,
            phase="planning",
            artifact_type="plan",
            payload={"steps": ["inspect"]},
            producer_role="planner",
            consumer_role="executor",
        )

        ack = self.store.acknowledge_artifact(
            artifact.artifact_id,
            consumer_role="verifier",
        )

        self.assertEqual(ack.status, "rejected")
        report = self.store.audit_report(run.run_id)
        self.assertEqual(len(report["missing_acks"]), 1)
        self.assertEqual(report["missing_acks"][0]["artifact_id"], artifact.artifact_id)

    def test_checkpoint_updates_run_pointer(self) -> None:
        run = self.store.begin_run(task_id="task-1", objective="ship")
        checkpoint_id = self.store.checkpoint(
            run.run_id,
            phase="planning",
            reason="plan_validated",
            state={"phase": "planning"},
        )
        updated = self.store.get_run(run.run_id)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.checkpoint_id, checkpoint_id)


class CoordinatorOrchestrationIntegrationTests(unittest.TestCase):
    def test_coordinator_records_phase_artifacts_and_acks(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        router = MagicMock()
        router.ask.return_value = MagicMock(content="ok")
        observe = MagicMock()
        store = OrchestrationStore(tmp / "claw.db")
        coordinator = CoordinatorService(
            router=router,
            observe=observe,
            scratch_root=tmp / "scratch",
            max_workers=2,
            orchestration_store=store,
        )

        result = coordinator.run(
            "task-1",
            "objective",
            [WorkerTask(name="research", instruction="find")],
            implementation_tasks=[WorkerTask(name="implement", instruction="build", lane="worker")],
            verification_tasks=[WorkerTask(name="verify", instruction="check", lane="verifier")],
        )

        self.assertEqual(result.error, "")
        run = store.get_run("task-1")
        self.assertIsNotNone(run)
        assert run is not None
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.current_phase, "verification")

        report = store.audit_report("task-1")
        self.assertEqual(report["missing_acks"], [])
        event_types = [event["event_type"] for event in report["events"]]
        self.assertIn("run_started", event_types)
        self.assertIn("artifact_recorded", event_types)
        self.assertIn("artifact_acknowledged", event_types)
        self.assertIn("checkpoint_created", event_types)
        self.assertIn("run_completed", event_types)


if __name__ == "__main__":
    unittest.main()
