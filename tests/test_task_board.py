"""Tests for TaskBoard goal-hierarchy extensions (Wave 2.5).

The board already supported flat tasks. Wave 2.5 adds Project as a
coordination object grouping tasks toward one outcome, plus parent_task_id /
project_id / milestone_id on BoardTask so callers can reason about
multi-task progress.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from claw_v2.task_board import (
    ProjectStatus,
    TaskBoard,
)


class TaskBoardProjectTests(unittest.TestCase):
    def _board(self, tmpdir: Path) -> TaskBoard:
        return TaskBoard(board_root=tmpdir)

    def test_publish_project_and_get_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            project = board.publish_project(
                "Land $10K MRR by Q3",
                success_criteria=["3 paying customers", "MRR >= $10K"],
                owner_session="hector-1",
                notes="north star",
            )

            loaded = board.get_project(project.id)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.title, "Land $10K MRR by Q3")
            self.assertEqual(loaded.status, ProjectStatus.ACTIVE)
            self.assertEqual(loaded.success_criteria, ["3 paying customers", "MRR >= $10K"])
            self.assertEqual(loaded.owner_session, "hector-1")

    def test_get_project_returns_none_for_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            self.assertIsNone(board.get_project("not-real"))

    def test_publish_task_with_project_link_persists_relationship(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            project = board.publish_project("Landing site refresh")

            task = board.publish(
                "Draft hero copy",
                "Write 3 hero variants for the new landing.",
                project_id=project.id,
                milestone_id="m-design-week",
            )

            self.assertEqual(task.project_id, project.id)
            self.assertEqual(task.milestone_id, "m-design-week")
            tasks = board.tasks_for_project(project.id)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].id, task.id)

    def test_parent_task_id_links_subtasks_to_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            parent = board.publish("plan release", "decompose into 3 steps")
            child = board.publish(
                "step 1: cut branch",
                "git checkout -b release/v2",
                parent_task_id=parent.id,
            )
            self.assertEqual(child.parent_task_id, parent.id)

    def test_project_status_summary_counts_task_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            project = board.publish_project("multi-task project")

            t1 = board.publish("a", "do a", project_id=project.id)
            t2 = board.publish("b", "do b", project_id=project.id)
            _t3 = board.publish("c", "do c", project_id=project.id)

            board.claim("alma", lane="worker")
            board.complete(t1.id, "done")
            board.claim("hex", lane="worker")
            board.fail(t2.id, "boom")

            summary = board.project_status_summary(project.id)
            self.assertEqual(summary["total"], 3)
            self.assertEqual(summary.get("completed"), 1)
            self.assertEqual(summary.get("failed"), 1)
            self.assertEqual(summary.get("queued"), 1)

    def test_list_projects_filters_by_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            p1 = board.publish_project("active one")
            p2 = board.publish_project("done one")
            board.update_project_status(p2.id, ProjectStatus.COMPLETED)

            active = board.list_projects(status=ProjectStatus.ACTIVE)
            completed = board.list_projects(status=ProjectStatus.COMPLETED)
            self.assertEqual({p.id for p in active}, {p1.id})
            self.assertEqual({p.id for p in completed}, {p2.id})

    def test_update_project_status_persists_and_returns_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            p = board.publish_project("p1")
            updated = board.update_project_status(p.id, ProjectStatus.BLOCKED)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, ProjectStatus.BLOCKED)

            reloaded = board.get_project(p.id)
            assert reloaded is not None
            self.assertEqual(reloaded.status, ProjectStatus.BLOCKED)

    def test_tasks_glob_does_not_pick_up_project_files(self) -> None:
        # Regression guard: _load_all() globs *.json in self.root; projects
        # live in self.root/projects/, so they must NOT show up as tasks.
        with tempfile.TemporaryDirectory() as tmpdir:
            board = self._board(Path(tmpdir))
            board.publish_project("just a project, no tasks")
            board.publish("real task", "instruction")
            tasks = board._load_all()  # noqa: SLF001 — covering a regression invariant
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].title, "real task")


if __name__ == "__main__":
    unittest.main()
