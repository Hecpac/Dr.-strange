from __future__ import annotations

import unittest

from claw_v2.artifacts import (
    ExecutionArtifact,
    PlanArtifact,
    append_lifecycle_artifacts,
    new_artifact_id,
    planned_phases_for_mode,
)


class ArtifactTests(unittest.TestCase):
    def test_append_lifecycle_artifacts_indexes_latest_by_kind_and_preserves_events(self) -> None:
        plan = PlanArtifact(
            artifact_id=new_artifact_id("plan"),
            task_id="task-1",
            session_id="s1",
            objective="fix login",
            mode="coding",
            planned_phases=planned_phases_for_mode("coding"),
        )
        execution = ExecutionArtifact(
            artifact_id=new_artifact_id("execution"),
            task_id="task-1",
            session_id="s1",
            status="running",
            runtime="coordinator",
            provider="codex",
            model="gpt-5.5",
        )

        payload = append_lifecycle_artifacts({}, plan, execution)

        lifecycle = payload["lifecycle"]
        self.assertEqual(lifecycle["plan"]["objective"], "fix login")
        self.assertEqual(lifecycle["execution"]["provider"], "codex")
        self.assertEqual(lifecycle["plan"]["planned_phases"], ["research", "synthesis", "implementation", "verification"])
        self.assertEqual([event["kind"] for event in lifecycle["events"]], ["plan", "execution"])
        self.assertEqual(len(lifecycle["artifact_ids"]), 2)


if __name__ == "__main__":
    unittest.main()
