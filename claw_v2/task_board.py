"""Shared Task Board — swarm-pattern coordination for agents.

Agents publish tasks, claim them, and report results.
The board is file-backed (JSON per task) for persistence across restarts.
Thread-safe for concurrent access from coordinator workers.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(slots=True)
class BoardTask:
    """A task on the shared board."""

    id: str
    title: str
    instruction: str
    status: TaskStatus = TaskStatus.QUEUED
    priority: int = 0  # higher = more urgent
    created_by: str = ""
    assigned_to: str | None = None
    required_lane: str = "worker"
    tags: list[str] = field(default_factory=list)
    result: str = ""
    error: str = ""
    created_at: float = 0.0
    claimed_at: float = 0.0
    completed_at: float = 0.0
    # Wave 2.5: goal hierarchy. parent_task_id links a sub-task to the
    # parent that decomposed it; project_id and milestone_id link the task
    # to higher-level coordination objects so Kairos / brain can reason
    # about progress across multiple tasks toward one outcome.
    parent_task_id: str | None = None
    project_id: str | None = None
    milestone_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BoardTask:
        d["status"] = TaskStatus(d.get("status", "queued"))
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ABANDONED = "abandoned"


@dataclass(slots=True)
class Project:
    """A coordination object grouping multiple BoardTasks toward one outcome.

    Wave 2.5 of the autonomy plan. Projects let Kairos and the brain reason
    about progress across multiple tasks instead of operating on flat queue.
    """

    id: str
    title: str
    success_criteria: list[str] = field(default_factory=list)
    status: ProjectStatus = ProjectStatus.ACTIVE
    owner_session: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Project:
        d["status"] = ProjectStatus(d.get("status", "active"))
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class TaskBoard:
    """File-backed, thread-safe task board for agent swarm coordination.

    Tasks are persisted as individual JSON files under ``board_root``.
    Agents interact via publish / claim / complete / fail.
    """

    def __init__(self, board_root: Path | str = Path.home() / ".claw" / "board") -> None:
        self.root = Path(board_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── publish ──────────────────────────────────────────────

    def publish(
        self,
        title: str,
        instruction: str,
        *,
        created_by: str = "",
        priority: int = 0,
        required_lane: str = "worker",
        tags: list[str] | None = None,
        parent_task_id: str | None = None,
        project_id: str | None = None,
        milestone_id: str | None = None,
    ) -> BoardTask:
        """Add a new task to the board. Returns the created task."""
        task = BoardTask(
            id=uuid.uuid4().hex[:12],
            title=title,
            instruction=instruction,
            created_by=created_by,
            priority=priority,
            required_lane=required_lane,
            tags=tags or [],
            created_at=time.time(),
            parent_task_id=parent_task_id,
            project_id=project_id,
            milestone_id=milestone_id,
        )
        self._save(task)
        logger.info("TaskBoard: published %s by %s", task.id, created_by)
        return task

    # ── claim ────────────────────────────────────────────────

    def claim(
        self, agent_name: str, *, lane: str = "worker", tags: list[str] | None = None
    ) -> BoardTask | None:
        """Claim the highest-priority queued task matching lane/tags.

        Returns the claimed task or None if nothing is available.
        """
        with self._lock:
            candidates = [
                t
                for t in self._load_all()
                if t.status == TaskStatus.QUEUED
                and t.required_lane == lane
                and (tags is None or any(tag in t.tags for tag in tags))
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda t: (-t.priority, t.created_at))
            task = candidates[0]
            task.status = TaskStatus.CLAIMED
            task.assigned_to = agent_name
            task.claimed_at = time.time()
            self._save(task)
            logger.info("TaskBoard: %s claimed by %s", task.id, agent_name)
            return task

    # ── progress ─────────────────────────────────────────────

    def start(self, task_id: str) -> None:
        """Mark a claimed task as in-progress."""
        with self._lock:
            task = self._load(task_id)
            if task and task.status == TaskStatus.CLAIMED:
                task.status = TaskStatus.IN_PROGRESS
                self._save(task)

    def complete(self, task_id: str, result: str) -> None:
        """Mark a task as completed with its result."""
        with self._lock:
            task = self._load(task_id)
            if task and task.status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
                task.status = TaskStatus.COMPLETED
                task.result = result
                task.completed_at = time.time()
                self._save(task)
                logger.info("TaskBoard: %s completed", task_id)

    def fail(self, task_id: str, error: str) -> None:
        """Mark a task as failed."""
        with self._lock:
            task = self._load(task_id)
            if task and task.status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS):
                task.status = TaskStatus.FAILED
                task.error = error
                task.completed_at = time.time()
                self._save(task)
                logger.warning("TaskBoard: %s failed — %s", task_id, error)

    # ── queries ──────────────────────────────────────────────

    def pending(self, *, lane: str | None = None) -> list[BoardTask]:
        """Return all queued tasks, optionally filtered by lane."""
        tasks = [t for t in self._load_all() if t.status == TaskStatus.QUEUED]
        if lane:
            tasks = [t for t in tasks if t.required_lane == lane]
        return sorted(tasks, key=lambda t: (-t.priority, t.created_at))

    def active(self) -> list[BoardTask]:
        """Return all claimed or in-progress tasks."""
        return [
            t for t in self._load_all() if t.status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS)
        ]

    def summary(self) -> dict[str, int]:
        """Return counts per status."""
        all_tasks = self._load_all()
        counts: dict[str, int] = {}
        for t in all_tasks:
            counts[t.status.value] = counts.get(t.status.value, 0) + 1
        return counts

    def cleanup(self, max_age_seconds: float = 86400 * 7) -> int:
        """Remove completed/failed tasks older than max_age."""
        cutoff = time.time() - max_age_seconds
        removed = 0
        for task in self._load_all():
            if (
                task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                and task.completed_at < cutoff
            ):
                (self.root / f"{task.id}.json").unlink(missing_ok=True)
                removed += 1
        return removed

    # ── projects (Wave 2.5: goal hierarchy) ────────────────────

    def publish_project(
        self,
        title: str,
        *,
        success_criteria: list[str] | None = None,
        owner_session: str = "",
        notes: str = "",
    ) -> Project:
        """Create a new project; tasks can be linked via project_id."""
        now = time.time()
        project = Project(
            id=uuid.uuid4().hex[:12],
            title=title,
            success_criteria=success_criteria or [],
            owner_session=owner_session,
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        self._save_project(project)
        logger.info("TaskBoard: project %s '%s' created", project.id, title)
        return project

    def get_project(self, project_id: str) -> Project | None:
        path = self.projects_root / f"{project_id}.json"
        if not path.exists():
            return None
        try:
            return Project.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("TaskBoard: corrupt project file %s", path)
            return None

    def list_projects(self, *, status: ProjectStatus | None = None) -> list[Project]:
        projects: list[Project] = []
        for path in self.projects_root.glob("*.json"):
            try:
                projects.append(Project.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                logger.warning("TaskBoard: skipping corrupt project file %s", path)
        if status is not None:
            projects = [p for p in projects if p.status == status]
        return sorted(projects, key=lambda p: -p.updated_at)

    def tasks_for_project(self, project_id: str) -> list[BoardTask]:
        return sorted(
            (t for t in self._load_all() if t.project_id == project_id),
            key=lambda t: (-t.priority, t.created_at),
        )

    def project_status_summary(self, project_id: str) -> dict[str, int]:
        """Counts of task statuses for a given project. Useful for derivation
        of overall project status (all completed → project completed, any
        failed → project blocked, etc. — derivation lives in callers)."""
        tasks = self.tasks_for_project(project_id)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.status.value] = counts.get(task.status.value, 0) + 1
        counts["total"] = len(tasks)
        return counts

    def update_project_status(self, project_id: str, status: ProjectStatus) -> Project | None:
        with self._lock:
            project = self.get_project(project_id)
            if project is None:
                return None
            project.status = status
            project.updated_at = time.time()
            self._save_project(project)
            return project

    # ── persistence ──────────────────────────────────────────

    def _save(self, task: BoardTask) -> None:
        _atomic_write_text(
            self.root / f"{task.id}.json",
            json.dumps(task.to_dict(), indent=2, ensure_ascii=False),
        )

    def _save_project(self, project: Project) -> None:
        _atomic_write_text(
            self.projects_root / f"{project.id}.json",
            json.dumps(project.to_dict(), indent=2, ensure_ascii=False),
        )

    def _load(self, task_id: str) -> BoardTask | None:
        path = self.root / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            return BoardTask.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("TaskBoard: corrupt task file %s", path)
            return None

    def _load_all(self) -> list[BoardTask]:
        tasks = []
        for path in self.root.glob("*.json"):
            try:
                tasks.append(BoardTask.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                logger.warning("TaskBoard: skipping corrupt file %s", path)
        return tasks


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically and durably (F0.4).

    Mirrors ``coordinator._atomic_write_text`` / ``liveness.write_liveness``
    (unique dot-prefixed tmp file → ``os.write`` → ``fsync`` → ``os.replace`` →
    best-effort parent-dir fsync); intentionally duplicated rather than imported
    so this leaf store has no dependency on the coordinator. A reader never
    observes a partial file: a crash between temp-write and rename leaves the
    target absent or the previous complete file, never half-written. The dot
    prefix keeps the tmp out of the ``*.json`` listing glob.
    """
    data = text.encode("utf-8")
    tmp = path.parent / f".{path.name}.{secrets.token_hex(4)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    # Best-effort durability of the rename across power loss; a failure here
    # (e.g. a filesystem/sandbox that disallows fsync'ing a directory) must NOT
    # turn a successful, atomically in-place write into a spurious error.
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
