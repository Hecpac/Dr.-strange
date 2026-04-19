# Checkpointing Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Claw a DB-level rollback primitive — hot-backup snapshots of `claw.db` at pre-critical-action and consecutive-failure triggers, restorable on next process start, with a ring of N=10 snapshots and Telegram/CLI controls.

**Architecture:** A new `CheckpointService` in `claw_v2/checkpoint.py` creates snapshots via `sqlite3.Connection.backup()` into `<db_path>.parent/snapshots/ckpt_<8hex>.db`. Metadata lives in a new `checkpoints` table inside the same `claw.db`. Restores are deferred: `schedule_restore()` sets a `pending_restore` flag, and a module-level `apply_pending_restore_if_any()` called from `MemoryStore.__init__` (before schema migrations) copies the snapshot file over `claw.db`. `BrainService.execute_critical_action` takes a pre-action snapshot on all three execute branches; `BrainService._emit_verification_outcome` counts recent consecutive failures and triggers `auto_rollback_proposed` (autonomous → schedule restore; assisted → operator acts via `/rollback`).

**Tech Stack:** Python 3.12, SQLite stdlib (`sqlite3.Connection.backup` for hot copy, WAL mode already enabled), `secrets.token_hex` for ckpt_id, `shutil.copy` for restore apply, `unittest.TestCase` (project convention).

**Spec:** `docs/superpowers/specs/2026-04-19-checkpointing-design.md`.

**Non-goals (explicit scope fences):**
- No workspace filesystem snapshots (filesystem mutations recoverable via git; phase 2+).
- No automatic restart after `schedule_restore`; operator/daemon restart applies.
- No snapshots of sibling DBs (`buddy.db`, `observe.db` event store).
- No diff/merge, retention by time/size, encryption, or compression.

---

### Task 1: `checkpoints` table schema + migration

**Files:**
- Modify: `claw_v2/memory.py` (SCHEMA block, `_migrate` method)
- Test: `tests/test_memory_core.py` (new `CheckpointsTableSchemaTests` class)

**Rationale:** Metadata about snapshots must live in the DB itself so `create()` and rotation are transactional. Mirror the existing `outcome_embeddings` migration pattern.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memory_core.py`:

```python
class CheckpointsTableSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryStore(Path(tempfile.mkdtemp()) / "test.db")

    def test_checkpoints_table_exists(self) -> None:
        row = self.store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_checkpoints_columns(self) -> None:
        cols = {r[1] for r in self.store._conn.execute(
            "PRAGMA table_info(checkpoints)").fetchall()}
        expected = {"id", "ckpt_id", "created_at", "trigger_reason",
                    "session_id", "consecutive_failures", "file_path",
                    "pending_restore", "restored_at"}
        self.assertEqual(cols, expected)

    def test_checkpoints_indices_exist(self) -> None:
        indices = {r[1] for r in self.store._conn.execute(
            "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='checkpoints'"
        ).fetchall()}
        self.assertIn("idx_checkpoints_created_at", indices)
        self.assertIn("idx_checkpoints_pending_restore", indices)

    def test_migration_idempotent(self) -> None:
        MemoryStore(self.store.db_path)
        MemoryStore(self.store.db_path)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py::CheckpointsTableSchemaTests -v`
Expected: FAIL — table does not exist.

- [ ] **Step 3: Add the table to `SCHEMA`**

In `claw_v2/memory.py`, append to the `SCHEMA` string (after the last existing CREATE TABLE block, before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ckpt_id TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    trigger_reason TEXT NOT NULL,
    session_id TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    file_path TEXT NOT NULL,
    pending_restore INTEGER NOT NULL DEFAULT 0,
    restored_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_created_at ON checkpoints(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_checkpoints_pending_restore
    ON checkpoints(pending_restore) WHERE pending_restore = 1;
```

Also append to `_migrate` (after the existing `outcome_embeddings` migration block, before the final `backfill_outcome_embeddings()` call):

```python
cursor = self._conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
)
if cursor.fetchone() is None:
    try:
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS checkpoints ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ckpt_id TEXT NOT NULL UNIQUE, "
            "created_at TEXT DEFAULT CURRENT_TIMESTAMP, "
            "trigger_reason TEXT NOT NULL, "
            "session_id TEXT, "
            "consecutive_failures INTEGER NOT NULL DEFAULT 0, "
            "file_path TEXT NOT NULL, "
            "pending_restore INTEGER NOT NULL DEFAULT 0, "
            "restored_at TEXT); "
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_created_at "
            "ON checkpoints(created_at DESC); "
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_pending_restore "
            "ON checkpoints(pending_restore) WHERE pending_restore = 1;"
        )
        self._conn.commit()
    except sqlite3.OperationalError:
        pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py::CheckpointsTableSchemaTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full memory suite**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py tests/test_memory_scoped.py -q`
Expected: all prior tests still PASS plus +4 new.

- [ ] **Step 6: Commit**

```bash
git add claw_v2/memory.py tests/test_memory_core.py
git commit -m "feat(memory): add checkpoints table for snapshot metadata"
```

---

### Task 2: `CheckpointService.create()` — snapshot creation (no rotation yet)

**Files:**
- Create: `claw_v2/checkpoint.py`
- Test: `tests/test_checkpoint.py` (new file)

**Rationale:** Get a single snapshot end-to-end: file on disk, row in DB, atomic under lock. Rotation comes in Task 3 as an additive step. Splitting lets each test target one responsibility.

- [ ] **Step 1: Write the failing test**

Create `tests/test_checkpoint.py`:

```python
"""Tests for CheckpointService — DB snapshot primitive."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claw_v2.checkpoint import CheckpointService
from claw_v2.memory import MemoryStore


class CheckpointCreateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.snapshots_dir = tmp / "snapshots"
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=self.snapshots_dir,
        )

    def test_create_returns_ckpt_id_with_expected_format(self) -> None:
        ckpt_id = self.service.create(trigger_reason="test")
        self.assertTrue(ckpt_id.startswith("ckpt_"))
        self.assertEqual(len(ckpt_id), 13)
        self.assertTrue(all(c in "0123456789abcdef" for c in ckpt_id[5:]))

    def test_create_writes_snapshot_file_to_disk(self) -> None:
        ckpt_id = self.service.create(trigger_reason="test")
        expected = self.snapshots_dir / f"{ckpt_id}.db"
        self.assertTrue(expected.exists())
        self.assertGreater(expected.stat().st_size, 0)

    def test_create_inserts_metadata_row(self) -> None:
        self.store.store_fact("before_snapshot", "value", source="test")
        ckpt_id = self.service.create(
            trigger_reason="pre-action",
            session_id="s1",
            consecutive_failures=2,
        )
        row = self.store._conn.execute(
            "SELECT ckpt_id, trigger_reason, session_id, consecutive_failures, "
            "file_path, pending_restore, restored_at "
            "FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["ckpt_id"], ckpt_id)
        self.assertEqual(row["trigger_reason"], "pre-action")
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["consecutive_failures"], 2)
        self.assertEqual(row["pending_restore"], 0)
        self.assertIsNone(row["restored_at"])

    def test_snapshot_file_contains_source_data(self) -> None:
        self.store.store_fact("my_key", "my_value", source="test")
        ckpt_id = self.service.create(trigger_reason="test")
        snap_path = self.snapshots_dir / f"{ckpt_id}.db"
        snap_conn = sqlite3.connect(snap_path)
        try:
            row = snap_conn.execute(
                "SELECT value FROM facts WHERE key = 'my_key'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "my_value")
        finally:
            snap_conn.close()

    def test_backup_invoked_with_expected_args(self) -> None:
        captured = {}
        original_backup = sqlite3.Connection.backup

        def spy_backup(self, target, **kwargs):
            captured["pages"] = kwargs.get("pages")
            captured["sleep"] = kwargs.get("sleep")
            return original_backup(self, target, **kwargs)

        with patch.object(sqlite3.Connection, "backup", spy_backup):
            self.service.create(trigger_reason="test")
        self.assertEqual(captured["pages"], 100)
        self.assertEqual(captured["sleep"], 0.001)

    def test_create_failure_cleans_up_file(self) -> None:
        def failing_backup(self, target, **kwargs):
            raise sqlite3.OperationalError("simulated failure")
        with patch.object(sqlite3.Connection, "backup", failing_backup):
            with self.assertRaises(sqlite3.OperationalError):
                self.service.create(trigger_reason="test")
        # snapshot dir should be empty — no leaked partial files
        self.assertEqual(list(self.snapshots_dir.glob("ckpt_*.db")), [])
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 0)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointCreateTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'claw_v2.checkpoint'`.

- [ ] **Step 3: Implement `claw_v2/checkpoint.py`**

Create file:

```python
"""DB-only checkpointing primitive for Claw — Phase 1.

See docs/superpowers/specs/2026-04-19-checkpointing-design.md.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claw_v2.memory import MemoryStore

logger = logging.getLogger(__name__)


class CheckpointService:
    def __init__(
        self,
        *,
        memory: "MemoryStore",
        snapshots_dir: Path,
        ring_size: int = 10,
    ) -> None:
        self.memory = memory
        self.snapshots_dir = Path(snapshots_dir)
        self.ring_size = ring_size
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        trigger_reason: str,
        session_id: str | None = None,
        consecutive_failures: int = 0,
    ) -> str:
        ckpt_id = f"ckpt_{secrets.token_hex(4)}"
        file_path = self.snapshots_dir / f"{ckpt_id}.db"
        target_conn: sqlite3.Connection | None = None
        try:
            with self.memory._lock:
                target_conn = sqlite3.connect(file_path)
                self.memory._conn.backup(target_conn, pages=100, sleep=0.001)
                target_conn.close()
                target_conn = None
                self.memory._conn.execute(
                    "INSERT INTO checkpoints "
                    "(ckpt_id, trigger_reason, session_id, consecutive_failures, file_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ckpt_id, trigger_reason, session_id, consecutive_failures, str(file_path)),
                )
                self.memory._conn.commit()
        except Exception:
            if target_conn is not None:
                try:
                    target_conn.close()
                except Exception:
                    pass
            file_path.unlink(missing_ok=True)
            raise
        return ckpt_id
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointCreateTests -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/checkpoint.py tests/test_checkpoint.py
git commit -m "feat(checkpoint): CheckpointService.create with atomic snapshot"
```

---

### Task 3: Ring buffer rotation

**Files:**
- Modify: `claw_v2/checkpoint.py` (extend `create()` to rotate)
- Test: `tests/test_checkpoint.py` (new `CheckpointRotationTests` class)

**Rationale:** Without rotation, `create()` would leak files forever. Keep rotation as its own task so the test proves exactly 10 remain and the oldest is purged from both disk and DB.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_checkpoint.py`:

```python
class CheckpointRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.snapshots_dir = tmp / "snapshots"
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=self.snapshots_dir, ring_size=10,
        )

    def test_ring_keeps_exactly_N_snapshots(self) -> None:
        created: list[str] = []
        for i in range(11):
            created.append(self.service.create(trigger_reason=f"ckpt-{i}"))
        # Exactly 10 files on disk
        files = sorted(self.snapshots_dir.glob("ckpt_*.db"))
        self.assertEqual(len(files), 10)
        # Exactly 10 rows in DB
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 10)

    def test_oldest_snapshot_is_purged_first(self) -> None:
        created: list[str] = []
        for i in range(11):
            created.append(self.service.create(trigger_reason=f"ckpt-{i}"))
        first_ckpt_id = created[0]
        # First ckpt_id no longer in DB
        row = self.store._conn.execute(
            "SELECT ckpt_id FROM checkpoints WHERE ckpt_id = ?",
            (first_ckpt_id,),
        ).fetchone()
        self.assertIsNone(row)
        # First snapshot file gone from disk
        first_path = self.snapshots_dir / f"{first_ckpt_id}.db"
        self.assertFalse(first_path.exists())
        # Most recent 10 all present
        for ckpt_id in created[1:]:
            self.assertTrue((self.snapshots_dir / f"{ckpt_id}.db").exists())

    def test_custom_ring_size_respected(self) -> None:
        svc = CheckpointService(
            memory=self.store,
            snapshots_dir=Path(tempfile.mkdtemp()),
            ring_size=3,
        )
        for i in range(5):
            svc.create(trigger_reason=f"t-{i}")
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 3)

    def test_rotation_failure_does_not_rollback_new_snapshot(self) -> None:
        # Prime with 10 snapshots, then force the rotation UPDATE to fail on the 11th.
        for i in range(10):
            self.service.create(trigger_reason=f"base-{i}")
        # Capture a row to delete later so Path.unlink can fail.
        new_ckpt: str | None = None
        original_unlink = Path.unlink
        def fail_once(self, *args, **kwargs):
            # Fail only on the first unlink during rotation.
            nonlocal new_ckpt
            if new_ckpt is not None and "ckpt_" in str(self):
                raise OSError("simulated rotation failure")
            return original_unlink(self, *args, **kwargs)
        # We want the NEW snapshot (the 11th) to exist even if rotation of the old one fails.
        with patch.object(Path, "unlink", fail_once):
            new_ckpt = self.service.create(trigger_reason="new")
        # The new ckpt must still exist.
        row = self.store._conn.execute(
            "SELECT ckpt_id FROM checkpoints WHERE ckpt_id = ?", (new_ckpt,),
        ).fetchone()
        self.assertIsNotNone(row)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointRotationTests -v`
Expected: FAIL — ring grows unbounded, test_ring_keeps_exactly_N_snapshots sees 11 files.

- [ ] **Step 3: Add rotation to `CheckpointService.create()`**

In `claw_v2/checkpoint.py`, inside `create()`, after `self.memory._conn.commit()` (still inside the `with self.memory._lock:` block), append:

```python
try:
    rows_to_purge = self.memory._conn.execute(
        "SELECT ckpt_id, file_path FROM checkpoints "
        "ORDER BY created_at ASC, id ASC"
    ).fetchall()
    excess = len(rows_to_purge) - self.ring_size
    if excess > 0:
        for row in rows_to_purge[:excess]:
            try:
                Path(row["file_path"]).unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Checkpoint rotation: failed to unlink %s",
                    row["file_path"], exc_info=True,
                )
            try:
                self.memory._conn.execute(
                    "DELETE FROM checkpoints WHERE ckpt_id = ?",
                    (row["ckpt_id"],),
                )
            except sqlite3.Error:
                logger.warning(
                    "Checkpoint rotation: failed to delete row %s",
                    row["ckpt_id"], exc_info=True,
                )
        self.memory._conn.commit()
except Exception:
    logger.warning("Checkpoint rotation encountered an error", exc_info=True)
```

Wrapping the entire rotation block in a broad try/except is deliberate: rotation failures must NOT roll back the new snapshot (better to keep 11 temporarily than to fail creation).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointRotationTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/checkpoint.py tests/test_checkpoint.py
git commit -m "feat(checkpoint): rotate ring to N=10 snapshots on create"
```

---

### Task 4: `list()` and `latest()` read helpers

**Files:**
- Modify: `claw_v2/checkpoint.py` (add methods)
- Test: `tests/test_checkpoint.py` (new `CheckpointListTests` class)

- [ ] **Step 1: Write the failing test**

Append:

```python
class CheckpointListTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=tmp / "snapshots",
        )

    def test_list_empty(self) -> None:
        self.assertEqual(self.service.list(), [])

    def test_latest_returns_none_when_empty(self) -> None:
        self.assertIsNone(self.service.latest())

    def test_list_ordered_desc_by_created_at(self) -> None:
        ids = [self.service.create(trigger_reason=f"t-{i}") for i in range(3)]
        rows = self.service.list()
        self.assertEqual([r["ckpt_id"] for r in rows], list(reversed(ids)))

    def test_latest_returns_newest(self) -> None:
        ids = [self.service.create(trigger_reason=f"t-{i}") for i in range(3)]
        self.assertEqual(self.service.latest()["ckpt_id"], ids[-1])

    def test_list_exposes_expected_fields(self) -> None:
        self.service.create(
            trigger_reason="t", session_id="s1", consecutive_failures=2,
        )
        rows = self.service.list()
        self.assertEqual(len(rows), 1)
        keys = set(rows[0].keys())
        for expected in ("ckpt_id", "created_at", "trigger_reason",
                         "session_id", "consecutive_failures",
                         "file_path", "pending_restore", "restored_at"):
            self.assertIn(expected, keys)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointListTests -v`
Expected: FAIL — `AttributeError: 'CheckpointService' object has no attribute 'list'`.

- [ ] **Step 3: Add methods to `CheckpointService`**

In `claw_v2/checkpoint.py`, add after `create()`:

```python
def list(self) -> list[dict]:
    rows = self.memory._conn.execute(
        "SELECT ckpt_id, created_at, trigger_reason, session_id, "
        "consecutive_failures, file_path, pending_restore, restored_at "
        "FROM checkpoints "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [dict(r) for r in rows]

def latest(self) -> dict | None:
    rows = self.list()
    return rows[0] if rows else None
```

- [ ] **Step 4: Run and confirm passing**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointListTests -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/checkpoint.py tests/test_checkpoint.py
git commit -m "feat(checkpoint): list and latest read helpers"
```

---

### Task 5: `schedule_restore()`

**Files:**
- Modify: `claw_v2/checkpoint.py` (add method)
- Test: `tests/test_checkpoint.py` (new `CheckpointScheduleRestoreTests` class)

**Rationale:** Marks a target checkpoint as pending_restore. One row can hold the flag at a time; scheduling a new target clears any previous one.

- [ ] **Step 1: Write the failing test**

Append:

```python
class CheckpointScheduleRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = MemoryStore(tmp / "test.db")
        self.snapshots_dir = tmp / "snapshots"
        self.service = CheckpointService(
            memory=self.store, snapshots_dir=self.snapshots_dir,
        )

    def test_schedule_restore_sets_flag(self) -> None:
        ckpt_id = self.service.create(trigger_reason="t")
        self.service.schedule_restore(ckpt_id)
        row = self.store._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 1)

    def test_schedule_restore_clears_previous_pending(self) -> None:
        a = self.service.create(trigger_reason="a")
        b = self.service.create(trigger_reason="b")
        self.service.schedule_restore(a)
        self.service.schedule_restore(b)
        count = self.store._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints WHERE pending_restore = 1"
        ).fetchone()["c"]
        self.assertEqual(count, 1)
        row = self.store._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?", (a,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 0)

    def test_schedule_restore_unknown_id_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.service.schedule_restore("ckpt_deadbeef")

    def test_schedule_restore_missing_file_raises(self) -> None:
        ckpt_id = self.service.create(trigger_reason="t")
        (self.snapshots_dir / f"{ckpt_id}.db").unlink()
        with self.assertRaises(FileNotFoundError):
            self.service.schedule_restore(ckpt_id)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointScheduleRestoreTests -v`
Expected: FAIL — `AttributeError: 'CheckpointService' object has no attribute 'schedule_restore'`.

- [ ] **Step 3: Implement**

Add to `claw_v2/checkpoint.py`:

```python
def schedule_restore(self, ckpt_id: str) -> None:
    with self.memory._lock:
        row = self.memory._conn.execute(
            "SELECT file_path FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(ckpt_id)
        if not Path(row["file_path"]).exists():
            raise FileNotFoundError(row["file_path"])
        self.memory._conn.execute(
            "UPDATE checkpoints SET pending_restore = 0 WHERE pending_restore = 1"
        )
        self.memory._conn.execute(
            "UPDATE checkpoints SET pending_restore = 1 WHERE ckpt_id = ?",
            (ckpt_id,),
        )
        self.memory._conn.commit()
```

- [ ] **Step 4: Run and confirm passing**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointScheduleRestoreTests -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/checkpoint.py tests/test_checkpoint.py
git commit -m "feat(checkpoint): schedule_restore marks pending flag"
```

---

### Task 6: `apply_pending_restore_if_any` + wiring in `MemoryStore.__init__`

**Files:**
- Modify: `claw_v2/checkpoint.py` (add module-level function)
- Modify: `claw_v2/memory.py` (call from `__init__` before schema+migrate)
- Test: `tests/test_memory_core.py` (new `ApplyPendingRestoreOnInitTests` class)

**Rationale:** The module-level function operates on a path, not a live `CheckpointService`. It must run BEFORE the persistent `MemoryStore` connection opens, to avoid overwriting a file that is being held open.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memory_core.py`:

```python
import shutil

from claw_v2.checkpoint import CheckpointService, apply_pending_restore_if_any


class ApplyPendingRestoreOnInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "test.db"
        self.snapshots_dir = self.tmp / "snapshots"

    def test_returns_none_when_no_pending_restore(self) -> None:
        MemoryStore(self.db_path)  # creates table
        result = apply_pending_restore_if_any(self.db_path)
        self.assertIsNone(result)

    def test_returns_none_when_db_does_not_exist(self) -> None:
        self.assertIsNone(apply_pending_restore_if_any(self.tmp / "missing.db"))

    def test_applies_snapshot_and_marks_restored_at(self) -> None:
        # Seed store, take snapshot, mutate, schedule restore.
        store = MemoryStore(self.db_path)
        store.store_fact("seed", "A", source="test")
        service = CheckpointService(memory=store, snapshots_dir=self.snapshots_dir)
        ckpt_id = service.create(trigger_reason="seed")
        store.store_fact("seed", "B", source="test")   # mutation post-snapshot
        service.schedule_restore(ckpt_id)
        store._conn.close()

        result = apply_pending_restore_if_any(self.db_path)
        self.assertEqual(result, ckpt_id)

        # Reopen and confirm the pre-snapshot state is present AND post-snapshot mutation is gone.
        store2 = MemoryStore(self.db_path)
        facts = store2.search_facts("seed")
        values = [f["value"] for f in facts]
        self.assertIn("A", values)
        self.assertNotIn("B", values)
        # restored_at is populated; pending_restore cleared.
        row = store2._conn.execute(
            "SELECT pending_restore, restored_at FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 0)
        self.assertIsNotNone(row["restored_at"])

    def test_memorystore_init_invokes_apply(self) -> None:
        # End-to-end: schedule then reopen MemoryStore — no manual apply call.
        store = MemoryStore(self.db_path)
        store.store_fact("key", "before", source="test")
        service = CheckpointService(memory=store, snapshots_dir=self.snapshots_dir)
        ckpt_id = service.create(trigger_reason="t")
        store.store_fact("key", "after", source="test")
        service.schedule_restore(ckpt_id)
        store._conn.close()

        store2 = MemoryStore(self.db_path)
        values = [f["value"] for f in store2.search_facts("key")]
        self.assertIn("before", values)
        self.assertNotIn("after", values)
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py::ApplyPendingRestoreOnInitTests -v`
Expected: FAIL — `apply_pending_restore_if_any` does not exist.

- [ ] **Step 3: Implement the module-level function**

Add to `claw_v2/checkpoint.py`:

```python
import shutil


def apply_pending_restore_if_any(db_path: Path) -> str | None:
    """Called from MemoryStore.__init__ BEFORE any schema migrations.

    If a pending_restore flag is set and the referenced snapshot file exists,
    copy the snapshot over db_path and mark restored_at. Returns the applied
    ckpt_id or None.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    try:
        probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        probe.row_factory = sqlite3.Row
        table_exists = probe.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='checkpoints'"
        ).fetchone() is not None
        if not table_exists:
            return None
        row = probe.execute(
            "SELECT ckpt_id, file_path FROM checkpoints "
            "WHERE pending_restore = 1 LIMIT 1"
        ).fetchone()
    finally:
        probe.close()
    if row is None:
        return None
    snapshot_path = Path(row["file_path"])
    ckpt_id = row["ckpt_id"]
    if not snapshot_path.exists():
        logger.warning(
            "Pending restore points to missing snapshot %s; clearing flag.",
            snapshot_path,
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE checkpoints SET pending_restore = 0 WHERE ckpt_id = ?",
                (ckpt_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return None
    shutil.copy(snapshot_path, db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE checkpoints "
            "SET pending_restore = 0, restored_at = CURRENT_TIMESTAMP "
            "WHERE ckpt_id = ?",
            (ckpt_id,),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Applied checkpoint %s from %s", ckpt_id, snapshot_path)
    return ckpt_id
```

- [ ] **Step 4: Wire into `MemoryStore.__init__`**

In `claw_v2/memory.py`, find `MemoryStore.__init__`. At the very top of `__init__`, BEFORE the line `self._conn = sqlite3.connect(...)`, add:

```python
# Apply any pending checkpoint restore before opening the persistent connection.
from claw_v2.checkpoint import apply_pending_restore_if_any as _apply_pending_restore
try:
    _apply_pending_restore(self.db_path)
except Exception:
    logger.debug("Pending restore check failed", exc_info=True)
```

The local import avoids a circular import at module load time (`checkpoint.py` imports `MemoryStore` only under `TYPE_CHECKING`).

- [ ] **Step 5: Run and confirm passing**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py::ApplyPendingRestoreOnInitTests tests/test_checkpoint.py -v`
Expected: PASS — all new and prior checkpoint tests.

- [ ] **Step 6: Regression sweep**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py tests/test_memory_scoped.py -q`
Expected: all prior memory tests still green.

- [ ] **Step 7: Commit**

```bash
git add claw_v2/checkpoint.py claw_v2/memory.py tests/test_memory_core.py
git commit -m "feat(memory): apply pending checkpoint restore on store open"
```

---

### Task 7: Wire `CheckpointService` into `main.py`

**Files:**
- Modify: `claw_v2/main.py` (instantiate service, pass to BrainService)
- Modify: `claw_v2/brain.py` (accept optional `checkpoint` field on `BrainService`)
- Modify: `claw_v2/types.py` (add `checkpoint_id` to `CriticalActionExecution`)

**Rationale:** Lock in the wire-up before adding Brain integration (Tasks 8-9). Having the types and injection in place lets later tasks only touch behavior.

- [ ] **Step 1: Add the `checkpoint_id` field to `CriticalActionExecution`**

In `claw_v2/types.py`, locate the `CriticalActionExecution` dataclass. Append one new field AFTER the existing `approval_status`:

```python
@dataclass(slots=True)
class CriticalActionExecution:
    action: str
    status: CriticalActionStatus
    executed: bool
    verification: CriticalActionVerification
    result: Any = None
    reason: str | None = None
    approval_status: str | None = None
    checkpoint_id: str | None = None  # NEW
```

- [ ] **Step 2: Add optional `checkpoint` field to `BrainService`**

In `claw_v2/brain.py`, locate the `BrainService` dataclass fields (around lines 60-72). Add after the `learning` and `observe` fields:

```python
from claw_v2.checkpoint import CheckpointService  # top-level import

# inside BrainService:
checkpoint: CheckpointService | None = None
```

- [ ] **Step 3: Instantiate `CheckpointService` in `main.py`**

In `claw_v2/main.py`, locate where `BrainService` is instantiated. Before that site, add:

```python
from claw_v2.checkpoint import CheckpointService

checkpoint = CheckpointService(
    memory=memory,
    snapshots_dir=config.db_path.parent / "snapshots",
)
```

And pass `checkpoint=checkpoint` to the `BrainService(...)` constructor keyword arguments.

- [ ] **Step 4: Run the full brain suite for regression**

Run: `.venv/bin/python -m pytest tests/test_brain_core.py tests/test_brain_verify.py -q`
Expected: all PASS. Existing tests construct `BrainService` without `checkpoint` — the default `None` makes that backward-compatible.

- [ ] **Step 5: Smoke test — import `main` module**

Run: `.venv/bin/python -c "import claw_v2.main"`
Expected: no ImportError or AttributeError. If this fails, the fix is localized to main.py's construction site.

- [ ] **Step 6: Commit**

```bash
git add claw_v2/types.py claw_v2/brain.py claw_v2/main.py
git commit -m "feat(brain): wire CheckpointService and checkpoint_id field"
```

---

### Task 8: Pre-critical-action checkpoint

**Files:**
- Modify: `claw_v2/brain.py` (`execute_critical_action`, three execute branches; `_emit_execution_event`)
- Test: `tests/test_brain_verify.py` (new `PreCriticalActionCheckpointTests` class)

**Rationale:** All three execute branches share the same "about to mutate state" moment. Capture the checkpoint exactly once per branch; propagate `ckpt_id` into both the returned `CriticalActionExecution` and the `critical_action_execution` observe event.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_brain_verify.py`:

```python
class PreCriticalActionCheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        from claw_v2.checkpoint import CheckpointService
        from claw_v2.learning import LearningLoop
        tmp = Path(tempfile.mkdtemp())
        self.memory = MemoryStore(tmp / "claw.db")
        self.observe = ObserveStream(tmp / "obs.db")
        self.learning = LearningLoop(memory=self.memory)
        self.checkpoint = CheckpointService(
            memory=self.memory, snapshots_dir=tmp / "snapshots",
        )

    def _brain(self) -> "BrainService":
        from claw_v2.brain import BrainService
        return BrainService(
            router=MagicMock(),
            memory=self.memory,
            system_prompt="You are Claw.",
            learning=self.learning,
            observe=self.observe,
            checkpoint=self.checkpoint,
        )

    def test_executed_autonomously_takes_pre_snapshot(self) -> None:
        brain = self._brain()
        called_executor = {"count": 0}
        def executor():
            called_executor["count"] += 1
            return "ok"
        # Fake verification with should_proceed + low risk for autonomous path.
        with patch.object(
            brain, "verify_critical_action",
            return_value=_fake_verification(should_proceed=True, risk="low"),
        ):
            result = brain.execute_critical_action(
                action="rm -rf /tmp/foo",
                plan="p", diff="d", test_output="t",
                executor=executor, autonomy_mode="autonomous",
            )
        self.assertEqual(called_executor["count"], 1)
        self.assertIsNotNone(result.checkpoint_id)
        self.assertTrue(result.checkpoint_id.startswith("ckpt_"))
        # Exactly one snapshot row in DB
        count = self.memory._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_blocked_path_does_not_create_checkpoint(self) -> None:
        brain = self._brain()
        def executor():
            self.fail("executor must not run when blocked")
        with patch.object(
            brain, "verify_critical_action",
            return_value=_fake_verification(should_proceed=False, risk="high"),
        ):
            result = brain.execute_critical_action(
                action="x", plan="p", diff="d", test_output="t",
                executor=executor, autonomy_mode="assisted",
            )
        self.assertIsNone(result.checkpoint_id)
        count = self.memory._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints"
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_checkpoint_id_in_observe_event(self) -> None:
        brain = self._brain()
        with patch.object(
            brain, "verify_critical_action",
            return_value=_fake_verification(should_proceed=True, risk="low"),
        ):
            brain.execute_critical_action(
                action="install pytest", plan="p", diff="d", test_output="t",
                executor=lambda: None, autonomy_mode="autonomous",
            )
        events = [e for e in self.observe.recent_events(limit=20)
                  if e["event_type"] == "critical_action_execution"]
        self.assertEqual(len(events), 1)
        self.assertIn("checkpoint_id", events[0]["payload"])
        self.assertTrue(events[0]["payload"]["checkpoint_id"].startswith("ckpt_"))

    def test_checkpoint_failure_does_not_block_execution(self) -> None:
        brain = self._brain()
        with patch.object(
            self.checkpoint, "create",
            side_effect=RuntimeError("simulated"),
        ), patch.object(
            brain, "verify_critical_action",
            return_value=_fake_verification(should_proceed=True, risk="low"),
        ):
            result = brain.execute_critical_action(
                action="x", plan="p", diff="d", test_output="t",
                executor=lambda: "ok", autonomy_mode="autonomous",
            )
        self.assertEqual(result.result, "ok")
        self.assertIsNone(result.checkpoint_id)
```

Also add a helper `_fake_verification` at the top of `test_brain_verify.py` if not present. Check first with:

```
grep -n "_fake_verification" tests/test_brain_verify.py
```

If absent, append this helper just above the new class:

```python
def _fake_verification(*, should_proceed: bool, risk: str) -> "CriticalActionVerification":
    from claw_v2.types import (
        CriticalActionVerification, LLMResponse,
    )
    return CriticalActionVerification(
        recommendation="approve" if should_proceed else "deny",
        risk_level=risk,
        summary="ok" if should_proceed else "blocked",
        should_proceed=should_proceed,
        requires_human_approval=not should_proceed,
        confidence=0.9,
        response=LLMResponse(
            content="ok", lane="verify", provider="mock", model="mock",
            confidence=0.9, cost_estimate=0.0,
        ),
    )
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_brain_verify.py::PreCriticalActionCheckpointTests -v`
Expected: FAIL — no snapshot taken, `checkpoint_id` always None.

- [ ] **Step 3: Add a helper `_maybe_pre_snapshot` on `BrainService`**

In `claw_v2/brain.py`, add a small private helper near the other private helpers (e.g., next to `_emit_verification_outcome`):

```python
def _maybe_pre_snapshot(self, *, action: str, session_id: str | None = None) -> str | None:
    if self.checkpoint is None:
        return None
    try:
        return self.checkpoint.create(
            trigger_reason=f"pre-critical-action:{action[:80]}",
            session_id=session_id,
        )
    except Exception:
        logger.warning("Pre-action checkpoint failed", exc_info=True)
        return None
```

- [ ] **Step 4: Capture `ckpt_id` in each of the three execute branches**

In `claw_v2/brain.py`, inside `execute_critical_action`, modify each of the three execute branches:

**Branch `executed_autonomously`** (around line 628), change:

```python
if autonomy_mode == "autonomous" and verification.should_proceed and verification.risk_level in {"low", "medium"}:
    result = executor()
    self._emit_execution_event(...)
    return CriticalActionExecution(
        action=action, status="executed_autonomously",
        executed=True, verification=verification, result=result,
        approval_status=approval_status,
    )
```

to:

```python
if autonomy_mode == "autonomous" and verification.should_proceed and verification.risk_level in {"low", "medium"}:
    ckpt_id = self._maybe_pre_snapshot(action=action)
    result = executor()
    self._emit_execution_event(
        action=action, verification=verification,
        status="executed_autonomously", approval_status=approval_status,
        checkpoint_id=ckpt_id,
    )
    return CriticalActionExecution(
        action=action, status="executed_autonomously",
        executed=True, verification=verification, result=result,
        approval_status=approval_status, checkpoint_id=ckpt_id,
    )
```

**Branch `executed`** (around line 645): same transformation — snapshot BEFORE `executor()`, pass `checkpoint_id` to event and return value.

**Branch `executed_with_approval`** (around line 662): same transformation.

For all three non-execute branches (`aborted_by_pre_check`, `awaiting_approval`, `blocked`), do NOT add snapshot code; they already do not run `executor()`.

- [ ] **Step 5: Thread `checkpoint_id` through `_emit_execution_event`**

In `claw_v2/brain.py`, extend `_emit_execution_event` to accept and include `checkpoint_id`:

```python
def _emit_execution_event(
    self,
    *,
    action: str,
    verification: CriticalActionVerification,
    status: str,
    approval_status: str | None,
    checkpoint_id: str | None = None,  # NEW
) -> None:
    if self.observe is None or verification.response is None:
        return
    self.observe.emit(
        "critical_action_execution",
        lane=verification.response.lane,
        provider=verification.response.provider,
        model=verification.response.model,
        payload={
            "action": action,
            "status": status,
            "approval_status": approval_status,
            "recommendation": verification.recommendation,
            "risk_level": verification.risk_level,
            "requires_human_approval": verification.requires_human_approval,
            "should_proceed": verification.should_proceed,
            "approval_id": verification.approval_id,
            "checkpoint_id": checkpoint_id,  # NEW
        },
    )
    # Remainder of _emit_execution_event unchanged (existing _emit_verification_outcome call).
```

Update the three non-execute callers of `_emit_execution_event` inside `execute_critical_action` (`aborted_by_pre_check`, `awaiting_approval`, `blocked`) to pass `checkpoint_id=None` explicitly. This keeps the signature uniform even if no snapshot was taken in those branches.

- [ ] **Step 6: Run and confirm passing**

Run: `.venv/bin/python -m pytest tests/test_brain_verify.py::PreCriticalActionCheckpointTests -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Regression sweep**

Run: `.venv/bin/python -m pytest tests/test_brain_core.py tests/test_brain_verify.py tests/test_memory_core.py tests/test_memory_scoped.py tests/test_checkpoint.py -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add claw_v2/brain.py tests/test_brain_verify.py
git commit -m "feat(brain): pre-critical-action checkpoint with ckpt_id on execution"
```

---

### Task 9: Consecutive failure trigger — automatic rollback proposal

**Files:**
- Modify: `claw_v2/brain.py` (extend `_emit_verification_outcome`; add `_count_recent_consecutive_failures`)
- Modify: `claw_v2/memory.py` (add helper `recent_outcomes` with `within_minutes` filter if not present)
- Test: `tests/test_brain_verify.py` (new `ConsecutiveFailuresTriggerTests`, `AssistedModeDoesNotAutoRestoreTests`, `NoCheckpointAvailableTests`, `OldFailuresIgnoredTests` classes)

**Rationale:** After a cycle completes and the post-mortem is recorded, check whether 3+ contiguous failures have accumulated in the last 30 minutes. If so, emit `auto_rollback_proposed` and (in autonomous mode only) schedule a restore.

- [ ] **Step 1: Add a `MemoryStore.recent_outcomes_within` helper**

In `claw_v2/memory.py`, add right after `recent_failures`:

```python
def recent_outcomes_within(
    self,
    *,
    within_minutes: int,
    task_type: str | None = None,
    session_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Outcomes in the last `within_minutes`, newest first."""
    clauses = ["created_at >= datetime('now', ?)"]
    params: list[object] = [f"-{int(within_minutes)} minutes"]
    if task_type is not None:
        clauses.append("task_type = ?")
        params.append(task_type)
    if session_id is not None:
        clauses.append("task_id = ?")
        params.append(session_id)
    params.append(limit)
    sql = (
        "SELECT task_type, task_id, description, approach, outcome, lesson, "
        "error_snippet, retries, created_at, feedback "
        "FROM task_outcomes WHERE " + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?"
    )
    rows = self._conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_brain_verify.py`:

```python
class ConsecutiveFailuresTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        from claw_v2.checkpoint import CheckpointService
        from claw_v2.learning import LearningLoop
        tmp = Path(tempfile.mkdtemp())
        self.memory = MemoryStore(tmp / "claw.db")
        self.observe = ObserveStream(tmp / "obs.db")
        self.learning = LearningLoop(memory=self.memory)
        self.checkpoint = CheckpointService(
            memory=self.memory, snapshots_dir=tmp / "snapshots",
        )
        # Pre-seed: autonomous mode on the session; one usable checkpoint.
        self.memory.update_session_state(
            "sess-X", autonomy_mode="autonomous", mode="coding",
        )
        self.seed_ckpt_id = self.checkpoint.create(trigger_reason="seed")

    def _brain(self) -> "BrainService":
        from claw_v2.brain import BrainService
        return BrainService(
            router=MagicMock(),
            memory=self.memory,
            system_prompt="You are Claw.",
            learning=self.learning,
            observe=self.observe,
            checkpoint=self.checkpoint,
        )

    def _push_failure(self, brain, i: int) -> None:
        brain._emit_verification_outcome(
            session_id="sess-X", task_type="self_heal",
            goal=f"goal-{i}", action_summary=f"action-{i}",
            verification_status="failed", error_snippet="boom",
        )

    def test_three_consecutive_failures_trigger_autonomous_rollback(self) -> None:
        brain = self._brain()
        for i in range(3):
            self._push_failure(brain, i)
        events = self.observe.recent_events(limit=20)
        kinds = [e["event_type"] for e in events]
        self.assertIn("auto_rollback_proposed", kinds)
        # schedule_restore was called -> pending_restore flag set on seed_ckpt
        row = self.memory._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?",
            (self.seed_ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 1)


class AssistedModeDoesNotAutoRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        from claw_v2.checkpoint import CheckpointService
        from claw_v2.learning import LearningLoop
        tmp = Path(tempfile.mkdtemp())
        self.memory = MemoryStore(tmp / "claw.db")
        self.observe = ObserveStream(tmp / "obs.db")
        self.learning = LearningLoop(memory=self.memory)
        self.checkpoint = CheckpointService(
            memory=self.memory, snapshots_dir=tmp / "snapshots",
        )
        self.memory.update_session_state(
            "sess-A", autonomy_mode="assisted", mode="coding",
        )
        self.seed_ckpt_id = self.checkpoint.create(trigger_reason="seed")

    def test_assisted_emits_event_but_does_not_schedule(self) -> None:
        from claw_v2.brain import BrainService
        brain = BrainService(
            router=MagicMock(), memory=self.memory,
            system_prompt="p", learning=self.learning,
            observe=self.observe, checkpoint=self.checkpoint,
        )
        for i in range(3):
            brain._emit_verification_outcome(
                session_id="sess-A", task_type="self_heal",
                goal=f"g-{i}", action_summary="a",
                verification_status="failed", error_snippet="x",
            )
        kinds = [e["event_type"] for e in self.observe.recent_events(limit=20)]
        self.assertIn("auto_rollback_proposed", kinds)
        row = self.memory._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?",
            (self.seed_ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 0)


class NoCheckpointAvailableTests(unittest.TestCase):
    def test_three_failures_without_checkpoint_emit_unavailable(self) -> None:
        from claw_v2.brain import BrainService
        from claw_v2.checkpoint import CheckpointService
        from claw_v2.learning import LearningLoop
        tmp = Path(tempfile.mkdtemp())
        memory = MemoryStore(tmp / "claw.db")
        observe = ObserveStream(tmp / "obs.db")
        learning = LearningLoop(memory=memory)
        checkpoint = CheckpointService(
            memory=memory, snapshots_dir=tmp / "snapshots",
        )
        memory.update_session_state("sess-Z", autonomy_mode="autonomous")
        brain = BrainService(
            router=MagicMock(), memory=memory, system_prompt="p",
            learning=learning, observe=observe, checkpoint=checkpoint,
        )
        for i in range(3):
            brain._emit_verification_outcome(
                session_id="sess-Z", task_type="self_heal",
                goal=f"g-{i}", action_summary="a",
                verification_status="failed", error_snippet="x",
            )
        kinds = [e["event_type"] for e in observe.recent_events(limit=20)]
        self.assertIn("auto_rollback_unavailable", kinds)
        self.assertNotIn("auto_rollback_proposed", kinds)


class OldFailuresIgnoredTests(unittest.TestCase):
    def test_failures_older_than_window_do_not_trigger(self) -> None:
        from claw_v2.brain import BrainService
        from claw_v2.checkpoint import CheckpointService
        from claw_v2.learning import LearningLoop
        tmp = Path(tempfile.mkdtemp())
        memory = MemoryStore(tmp / "claw.db")
        observe = ObserveStream(tmp / "obs.db")
        learning = LearningLoop(memory=memory)
        checkpoint = CheckpointService(
            memory=memory, snapshots_dir=tmp / "snapshots",
        )
        checkpoint.create(trigger_reason="seed")
        memory.update_session_state("sess-Y", autonomy_mode="autonomous")
        # Manually insert 3 stale failures older than 30 minutes.
        for i in range(3):
            memory._conn.execute(
                "INSERT INTO task_outcomes "
                "(task_type, task_id, description, approach, outcome, lesson, "
                "error_snippet, retries, created_at) "
                "VALUES ('self_heal', 'sess-Y', 'd', 'a', 'failure', 'l', 'x', 0, "
                "datetime('now', '-60 minutes'))"
            )
        memory._conn.commit()
        brain = BrainService(
            router=MagicMock(), memory=memory, system_prompt="p",
            learning=learning, observe=observe, checkpoint=checkpoint,
        )
        # Emit a fresh OK outcome — no auto_rollback should fire.
        brain._emit_verification_outcome(
            session_id="sess-Y", task_type="self_heal",
            goal="g", action_summary="a",
            verification_status="ok", error_snippet=None,
        )
        kinds = [e["event_type"] for e in observe.recent_events(limit=20)]
        self.assertNotIn("auto_rollback_proposed", kinds)
        self.assertNotIn("auto_rollback_unavailable", kinds)
```

- [ ] **Step 3: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_brain_verify.py::ConsecutiveFailuresTriggerTests tests/test_brain_verify.py::AssistedModeDoesNotAutoRestoreTests tests/test_brain_verify.py::NoCheckpointAvailableTests tests/test_brain_verify.py::OldFailuresIgnoredTests -v`
Expected: FAIL — `auto_rollback_proposed` never fires; `recent_outcomes_within` may not exist yet.

- [ ] **Step 4: Add `_count_recent_consecutive_failures` helper**

In `claw_v2/brain.py`, add a module-level helper near the bottom of the file (below `_format_verifier_evidence`):

```python
def _count_recent_consecutive_failures(
    memory: "MemoryStore",
    *,
    task_type: str | None,
    session_id: str | None,
    within_minutes: int = 30,
) -> int:
    rows = memory.recent_outcomes_within(
        within_minutes=within_minutes,
        task_type=task_type,
        session_id=session_id,
        limit=20,
    )
    count = 0
    for row in rows:
        if row["outcome"] == "failure":
            count += 1
        else:
            break
    return count
```

- [ ] **Step 5: Extend `_emit_verification_outcome` with the trigger logic**

In `claw_v2/brain.py`, modify `_emit_verification_outcome` so it adds an auto-rollback decision block AFTER the existing `record_cycle_outcome` call and its except handler:

```python
def _emit_verification_outcome(
    self,
    *,
    session_id: str,
    task_type: str,
    goal: str,
    action_summary: str,
    verification_status: str,
    error_snippet: str | None,
) -> None:
    if self.observe is not None:
        self.observe.emit(
            "cycle_verification_complete",
            payload={
                "session_id": session_id,
                "task_type": task_type,
                "verification_status": verification_status,
                "had_error": bool(error_snippet),
            },
        )
    if self.learning is None:
        return
    try:
        self.learning.record_cycle_outcome(
            session_id=session_id,
            task_type=task_type,
            goal=goal,
            action_summary=action_summary,
            verification_status=verification_status,
            error_snippet=error_snippet,
        )
    except Exception:
        logger.warning("Auto post-mortem recording failed", exc_info=True)

    # Auto-rollback decision block
    if self.checkpoint is None:
        return
    try:
        consecutive = _count_recent_consecutive_failures(
            self.memory,
            task_type=task_type,
            session_id=session_id,
            within_minutes=30,
        )
    except Exception:
        logger.debug("Failure count probe failed", exc_info=True)
        return
    if consecutive < 3:
        return
    latest = self.checkpoint.latest()
    autonomy_mode = (
        self.memory.get_session_state(session_id).get("autonomy_mode", "assisted")
        if session_id else "assisted"
    )
    if latest is None:
        if self.observe is not None:
            self.observe.emit(
                "auto_rollback_unavailable",
                payload={
                    "session_id": session_id,
                    "consecutive_failures": consecutive,
                    "autonomy_mode": autonomy_mode,
                },
            )
        return
    if self.observe is not None:
        self.observe.emit(
            "auto_rollback_proposed",
            payload={
                "ckpt_id": latest["ckpt_id"],
                "consecutive_failures": consecutive,
                "session_id": session_id,
                "autonomy_mode": autonomy_mode,
            },
        )
    if autonomy_mode == "autonomous":
        try:
            self.checkpoint.schedule_restore(latest["ckpt_id"])
        except Exception:
            logger.warning("schedule_restore failed", exc_info=True)
```

- [ ] **Step 6: Run and confirm passing**

Run: `.venv/bin/python -m pytest tests/test_brain_verify.py::ConsecutiveFailuresTriggerTests tests/test_brain_verify.py::AssistedModeDoesNotAutoRestoreTests tests/test_brain_verify.py::NoCheckpointAvailableTests tests/test_brain_verify.py::OldFailuresIgnoredTests -v`
Expected: PASS (4 tests).

- [ ] **Step 7: Regression sweep**

Run: `.venv/bin/python -m pytest tests/test_brain_core.py tests/test_brain_verify.py tests/test_memory_core.py tests/test_memory_scoped.py tests/test_checkpoint.py -q`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add claw_v2/brain.py claw_v2/memory.py tests/test_brain_verify.py
git commit -m "feat(brain): consecutive-failure trigger for auto rollback"
```

---

### Task 10: Telegram `/rollback` and `/checkpoints` commands

**Files:**
- Modify: `claw_v2/bot_commands.py` (new handlers)
- Test: `tests/test_bot.py` (new `RollbackCommandTests` class)

**Rationale:** Manual rollback path for operator control. `/rollback last` uses `latest()`; `/rollback <id>` uses the provided ckpt_id. `/checkpoints list` shows available snapshots.

- [ ] **Step 1: Inspect bot_commands.py command registration**

Run:

```
grep -n "def handle_\|register\|/task_\|/help" /Users/hector/Projects/Dr.-strange/claw_v2/bot_commands.py | head -30
```

Note the signature style used by existing handlers (e.g., `/task_pending`, `/task_approve`). Mirror that style in the new handlers.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_bot.py`:

```python
class RollbackCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        from claw_v2.checkpoint import CheckpointService
        tmp = Path(tempfile.mkdtemp())
        self.memory = MemoryStore(tmp / "claw.db")
        self.checkpoint = CheckpointService(
            memory=self.memory, snapshots_dir=tmp / "snapshots",
        )

    def test_rollback_last_schedules_latest(self) -> None:
        from claw_v2.bot_commands import handle_rollback
        ckpt_id = self.checkpoint.create(trigger_reason="t")
        reply = handle_rollback(args=["last"], checkpoint=self.checkpoint)
        self.assertIn(ckpt_id, reply)
        self.assertIn("/restart", reply)
        row = self.memory._conn.execute(
            "SELECT pending_restore FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 1)

    def test_rollback_by_id_schedules_exact(self) -> None:
        from claw_v2.bot_commands import handle_rollback
        a = self.checkpoint.create(trigger_reason="a")
        b = self.checkpoint.create(trigger_reason="b")
        reply = handle_rollback(args=[a], checkpoint=self.checkpoint)
        self.assertIn(a, reply)
        row = self.memory._conn.execute(
            "SELECT ckpt_id FROM checkpoints WHERE pending_restore = 1"
        ).fetchone()
        self.assertEqual(row["ckpt_id"], a)

    def test_rollback_unknown_id_returns_error(self) -> None:
        from claw_v2.bot_commands import handle_rollback
        reply = handle_rollback(args=["ckpt_nonexistent"], checkpoint=self.checkpoint)
        self.assertIn("no encontrado", reply.lower())
        count = self.memory._conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoints WHERE pending_restore = 1"
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_rollback_without_checkpoints_returns_error(self) -> None:
        from claw_v2.bot_commands import handle_rollback
        reply = handle_rollback(args=["last"], checkpoint=self.checkpoint)
        self.assertIn("no hay checkpoints", reply.lower())

    def test_checkpoints_list_renders_rows(self) -> None:
        from claw_v2.bot_commands import handle_checkpoints_list
        a = self.checkpoint.create(trigger_reason="pre-action")
        b = self.checkpoint.create(trigger_reason="manual")
        reply = handle_checkpoints_list(checkpoint=self.checkpoint)
        self.assertIn(a, reply)
        self.assertIn(b, reply)
        self.assertIn("pre-action", reply)

    def test_checkpoints_list_empty(self) -> None:
        from claw_v2.bot_commands import handle_checkpoints_list
        reply = handle_checkpoints_list(checkpoint=self.checkpoint)
        self.assertIn("sin checkpoints", reply.lower())
```

Add imports at the top of the file if needed (`tempfile`, `Path`, `MemoryStore`). Do not duplicate.

- [ ] **Step 3: Run and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_bot.py::RollbackCommandTests -v`
Expected: FAIL — `handle_rollback` and `handle_checkpoints_list` do not exist.

- [ ] **Step 4: Implement the handlers**

In `claw_v2/bot_commands.py`, add:

```python
def handle_rollback(*, args: list[str], checkpoint: "CheckpointService") -> str:
    """Handler for /rollback <ckpt_id|last>."""
    target = (args[0] if args else "").strip()
    if not target:
        return (
            "Uso: /rollback <ckpt_id|last>\n"
            "Usa /checkpoints list para ver IDs disponibles."
        )
    if target == "last":
        row = checkpoint.latest()
        if row is None:
            return "No hay checkpoints disponibles. Crea uno primero."
        ckpt_id = row["ckpt_id"]
    else:
        ckpt_id = target
    try:
        checkpoint.schedule_restore(ckpt_id)
    except KeyError:
        return f"Checkpoint {ckpt_id} no encontrado."
    except FileNotFoundError:
        return f"Checkpoint {ckpt_id} tiene su archivo snapshot ausente del disco."
    return (
        f"Checkpoint {ckpt_id} marcado para rollback. "
        f"Ejecuta /restart para aplicar."
    )


def handle_checkpoints_list(*, checkpoint: "CheckpointService") -> str:
    """Handler for /checkpoints list."""
    rows = checkpoint.list()
    if not rows:
        return "Sin checkpoints registrados."
    lines = ["Checkpoints disponibles (más reciente primero):"]
    for r in rows:
        lines.append(
            f"· {r['ckpt_id']} — {r['created_at']} — {r['trigger_reason'][:60]}"
        )
    return "\n".join(lines)
```

Add `from claw_v2.checkpoint import CheckpointService` under `TYPE_CHECKING` at the top of the file.

- [ ] **Step 5: Run and confirm passing**

Run: `.venv/bin/python -m pytest tests/test_bot.py::RollbackCommandTests -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add claw_v2/bot_commands.py tests/test_bot.py
git commit -m "feat(bot): /rollback and /checkpoints list commands"
```

---

### Task 11: End-to-end integration test

**Files:**
- Test: `tests/test_checkpoint.py` (new `CheckpointEndToEndTests` class; no production code changes)

**Rationale:** Prove the full cycle: seed data → snapshot → mutation → schedule restore → reopen MemoryStore → seed data present, mutation gone.

- [ ] **Step 1: Write the integration test**

Append to `tests/test_checkpoint.py`:

```python
class CheckpointEndToEndTests(unittest.TestCase):
    def test_restore_reverts_to_prior_state(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        db_path = tmp / "claw.db"
        snapshots_dir = tmp / "snapshots"

        # Phase 1 — seed, snapshot, mutate, schedule.
        store = MemoryStore(db_path)
        store.store_fact("k", "before", source="test")
        service = CheckpointService(memory=store, snapshots_dir=snapshots_dir)
        ckpt_id = service.create(trigger_reason="seed")
        store.store_fact("k", "after", source="test")
        service.schedule_restore(ckpt_id)
        store._conn.close()

        # Phase 2 — reopen (simulates restart). Apply happens in __init__.
        store2 = MemoryStore(db_path)
        values = [f["value"] for f in store2.search_facts("k")]
        self.assertIn("before", values)
        self.assertNotIn("after", values)

        row = store2._conn.execute(
            "SELECT pending_restore, restored_at FROM checkpoints WHERE ckpt_id = ?",
            (ckpt_id,),
        ).fetchone()
        self.assertEqual(row["pending_restore"], 0)
        self.assertIsNotNone(row["restored_at"])
```

- [ ] **Step 2: Run — expect pass on first try**

Run: `.venv/bin/python -m pytest tests/test_checkpoint.py::CheckpointEndToEndTests -v`
Expected: PASS. If it fails, a previous task is wired incorrectly.

- [ ] **Step 3: Run the full affected suite**

Run: `.venv/bin/python -m pytest tests/test_memory_core.py tests/test_memory_scoped.py tests/test_brain_core.py tests/test_brain_verify.py tests/test_checkpoint.py tests/test_bot.py -q`
Expected: all PASS. Report the count.

- [ ] **Step 4: Commit**

```bash
git add tests/test_checkpoint.py
git commit -m "test: end-to-end checkpoint restore reverts state on reopen"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| 2.1 Storage (snapshot files via backup) | Task 2 |
| 2.2 Metadata schema + indices | Task 1 |
| 2.3 Retention (N=10 ring) | Task 3 |
| 3. Public API (`create`, `schedule_restore`, `list`, `latest`, `apply_pending_restore_if_any`) | Tasks 2, 4, 5, 6 |
| 4.1 Create flow | Task 2 + 3 |
| 4.2 Schedule restore flow | Task 5 |
| 4.3 Apply pending restore flow | Task 6 |
| 5.1 Trigger A (pre-critical-action) | Task 8 |
| 5.2 Trigger D (consecutive-failures → auto-rollback) | Task 9 |
| 5.3 Telegram `/rollback` and `/checkpoints` | Task 10 |
| 6. Testing strategy (per layer) | Tasks 2, 3, 4, 5, 6, 8, 9, 10, 11 |
| 7. Risks (atomicity under concurrent writes, disk usage, schema restore) | Task 2 (failure cleanup), Task 6 (migration idempotency) — risks 5 (race) and 1 (backup blocking) covered via locking, risk 4 (never restart) is operator-facing in Task 10 reply text |
| 8. Success criteria (end-to-end loop) | Task 11 |

No gaps.

**2. Placeholder scan**

No "TBD", no "implement later", no "similar to Task N". Every code step contains complete code. Every test is concrete.

**3. Type consistency**

- `ckpt_id` is `str` throughout; format `ckpt_<8hex>`, length 13.
- `CheckpointService.create(*, trigger_reason, session_id=None, consecutive_failures=0) -> str` — same signature in Tasks 2, 3, 5, 6, 8, 9, 10, 11.
- `CheckpointService.schedule_restore(ckpt_id: str) -> None` — consistent in Tasks 5, 9, 10.
- `CheckpointService.list() -> list[dict]`, `CheckpointService.latest() -> dict | None` — consistent in Tasks 4, 9, 10.
- `apply_pending_restore_if_any(db_path: Path) -> str | None` — module-level function, Task 6.
- `BrainService.checkpoint: CheckpointService | None = None` — added Task 7, used Tasks 8, 9.
- `CriticalActionExecution.checkpoint_id: str | None = None` — added Task 7, used Task 8 (write + assertion).
- `_count_recent_consecutive_failures(memory, *, task_type, session_id, within_minutes=30) -> int` — defined Task 9, used Task 9.
- `MemoryStore.recent_outcomes_within(*, within_minutes, task_type=None, session_id=None, limit=20) -> list[dict]` — defined Task 9, used Task 9.
- Observe event names: `auto_rollback_proposed`, `auto_rollback_unavailable`, `critical_action_execution` (extended with `checkpoint_id` key) — Tasks 8, 9.

All signatures match across tasks.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-19-checkpointing.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
