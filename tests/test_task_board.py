"""Tests for TaskBoard goal-hierarchy extensions (Wave 2.5).

The board already supported flat tasks. Wave 2.5 adds Project as a
coordination object grouping tasks toward one outcome, plus parent_task_id /
project_id / milestone_id on BoardTask so callers can reason about
multi-task progress.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


def _crash_replace_for(target: Path):
    """Return an ``os.replace`` stand-in that simulates power loss exactly at
    the atomic-commit rename for ``target`` (and only ``target``), delegating
    every other rename to the real implementation."""
    real_replace = os.replace

    def crash_replace(src, dst, *args, **kwargs):
        if Path(dst).name == target.name:
            raise OSError("simulated power loss at rename")
        return real_replace(src, dst, *args, **kwargs)

    return crash_replace


class TaskBoardAtomicWriteTests(unittest.TestCase):
    """F0.4: ``TaskBoard`` persistence must be atomic and crash-safe. A reader
    must see either the old complete JSON or the new complete JSON — never a
    truncated/partial file — even if the process dies mid-write."""

    def test_save_crash_at_rename_leaves_previous_complete_json(self) -> None:
        # TDD #1: a crash at the atomic-commit point of a task re-save must
        # leave the on-disk task as the OLD complete record, not the new one
        # (and never a half-written file). Non-atomic write_text overwrites the
        # target in place and "commits" the new content even though the commit
        # was supposed to fail — that is the failure this test pins down.
        with tempfile.TemporaryDirectory() as tmpdir:
            board = TaskBoard(board_root=Path(tmpdir))
            task = board.publish("v1 title", "v1 instruction")
            target = Path(tmpdir) / f"{task.id}.json"
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["title"], "v1 title")

            task.title = "v2 title"
            with patch("os.replace", side_effect=_crash_replace_for(target)):
                with contextlib.suppress(OSError):
                    board._save(task)  # noqa: SLF001 — exercising the persistence primitive

            data = json.loads(target.read_text(encoding="utf-8"))  # must not raise
            self.assertEqual(data["title"], "v1 title")

    def test_save_project_crash_at_rename_leaves_previous_complete_json(self) -> None:
        # TDD #2: same old-or-new invariant for project records.
        with tempfile.TemporaryDirectory() as tmpdir:
            board = TaskBoard(board_root=Path(tmpdir))
            project = board.publish_project("v1 project")
            target = Path(tmpdir) / "projects" / f"{project.id}.json"
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["title"], "v1 project")

            project.title = "v2 project"
            with patch("os.replace", side_effect=_crash_replace_for(target)):
                with contextlib.suppress(OSError):
                    board._save_project(project)  # noqa: SLF001

            data = json.loads(target.read_text(encoding="utf-8"))  # must not raise
            self.assertEqual(data["title"], "v1 project")

    def test_reader_never_sees_corrupt_json_after_repeated_failed_saves(self) -> None:
        # TDD #4: read-after-failure. Across repeated interrupted saves a reader
        # always parses valid JSON that is one of the known-complete versions —
        # never a truncated/corrupt file.
        with tempfile.TemporaryDirectory() as tmpdir:
            board = TaskBoard(board_root=Path(tmpdir))
            task = board.publish("title 0", "instruction")
            target = Path(tmpdir) / f"{task.id}.json"
            for n in range(1, 6):
                task.title = f"title {n}"
                with patch("os.replace", side_effect=_crash_replace_for(target)):
                    with contextlib.suppress(OSError):
                        board._save(task)  # noqa: SLF001
                data = json.loads(target.read_text(encoding="utf-8"))  # must never raise
                self.assertEqual(data["title"], "title 0")  # old complete version survives

    def test_successful_save_writes_complete_json_and_leaves_no_tmp(self) -> None:
        # Preservation guard: the happy path still produces the new complete
        # record, leaves no leftover temp file, and the temp file never matches
        # the ``*.json`` listing glob (so _load_all never sees a partial).
        with tempfile.TemporaryDirectory() as tmpdir:
            board = TaskBoard(board_root=Path(tmpdir))
            task = board.publish("done", "instruction")
            target = Path(tmpdir) / f"{task.id}.json"
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["title"], "done")
            siblings = list(Path(tmpdir).glob("*"))
            self.assertEqual(
                [p.name for p in siblings if p.name != "projects"], [f"{task.id}.json"]
            )
            self.assertEqual(len(board._load_all()), 1)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
