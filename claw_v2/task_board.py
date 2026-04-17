"""Shared Task Board — swarm-pattern coordination for agents.

Agents publish tasks, claim them, and report results.
The board is file-backed (JSON per task) for persistence across restarts.
Thread-safe for concurrent access from coordinator workers.
"""

from __future__ import annotations

import json
import logging
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

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BoardTask:
        d["status"] = TaskStatus(d.get("status", "queued"))
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})


class TaskBoard:
    """File-backed, thread-safe task board for agent swarm coordination.

    Tasks are persisted as individual JSON files under ``board_root``.
    Agents interact via publish / claim / complete / fail.
    """

    def __init__(self, board_root: Path | str = Path.home() / ".claw" / "board") -> None:
        self.root = Path(board_root)
        self.root.mkdir(parents=True, exist_ok=True)
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
        )
        self._save(task)
        logger.info("TaskBoard: published %s by %s", task.id, created_by)
        return task

    # ── claim ────────────────────────────────────────────────

    def claim(self, agent_name: str, *, lane: str = "worker", tags: list[str] | None = None) -> BoardTask | None:
        """Claim the highest-priority queued task matching lane/tags.

        Returns the claimed task or None if nothing is available.
        """
        with self._lock:
            candidates = [
                t for t in self._load_all()
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
        return [t for t in self._load_all() if t.status in (TaskStatus.CLAIMED, TaskStatus.IN_PROGRESS)]

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
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED) and task.completed_at < cutoff:
                (self.root / f"{task.id}.json").unlink(missing_ok=True)
                removed += 1
        return removed

    # ── persistence ──────────────────────────────────────────

    def _save(self, task: BoardTask) -> None:
        (self.root / f"{task.id}.json").write_text(
            json.dumps(task.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
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
