# Checkpointing for Claw — Design Spec (Phase 1)

**Status:** Approved (brainstorming complete, 2026-04-19)
**Scope:** Phase 1 — DB-only snapshots with pre-action + consecutive-failure triggers, ring buffer of N=10, restart-applied restores.
**Out of scope (Phase 2+):** Workspace filesystem snapshots, automatic restart, diff/merge, other DBs (`buddy.db`, `observe.db`).

---

## 1. Problem Statement

Claw can enter corrupted internal state — e.g., a self-heal loop writes bad rows to `task_outcomes`, `session_state` acquires an inconsistent `pending_action`, or `facts` accumulate contradictory entries during a failed cycle. Today there is no rollback primitive: the only recovery is manual SQL surgery.

Goal: enable Claw (and the operator via Telegram/CLI) to **revert the `claw.db` state to a prior, known-good point** when a self-heal cycle demonstrably fails repeatedly, or preemptively before a high-risk action.

Explicit non-goal for Phase 1: reverting **filesystem** mutations Claw has made to `workspace_root`. Those are git-versioned and better handled with `git reset` / `git stash` in a later phase.

---

## 2. Architecture

New module `claw_v2/checkpoint.py` with a `CheckpointService` class. Instantiated once in `main.py` during setup, receives a reference to the `MemoryStore` (for its connection and lock) and a `snapshots_dir` path.

### 2.1 Storage

- Snapshot files: `<db_path>.parent / "snapshots" / ckpt_<8hex>.db`.
- Generated via `sqlite3.Connection.backup(target_conn, pages=100, sleep=0.001)` — cooperative hot backup, atomic, does not block readers on the WAL-mode source.
- Metadata: new table `checkpoints` in the **same** `claw.db`. Storing metadata in the same DB (rather than a sidecar JSON file) simplifies transactionality of `create()` and rotation.

### 2.2 Metadata schema

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

Column notes:
- `ckpt_id`: 13-char string `ckpt_<8 hex chars>`, generated via `secrets.token_hex(4)`. Globally unique, never reused.
- `file_path`: absolute path to snapshot. Stored explicitly rather than recomputed — simplifies tests with custom paths.
- `pending_restore`: boolean flag; at most one row with value 1 at any time (enforced in code, not via SQLite CHECK constraint).
- `restored_at`: timestamp when the restore was applied (set by `apply_pending_restore_if_any`).

Migration follows the existing `memory.py` pattern: a new `_MIGRATION_ADD_CHECKPOINTS_TABLE` constant, applied inside `_migrate()` via the `sqlite_master` existence check + `try/except sqlite3.OperationalError` guard.

### 2.3 Retention

Ring buffer of **N = 10** snapshots. On `create()`, after insert, if `SELECT COUNT(*) FROM checkpoints > 10`, delete the oldest rows (by `created_at ASC`) and `unlink` their files. Failures in rotation are logged but do not roll back the new snapshot (better to keep 11 temporarily than to fail creation).

---

## 3. Public API

```python
class CheckpointService:
    def __init__(
        self,
        memory: MemoryStore,
        snapshots_dir: Path,
        *,
        ring_size: int = 10,
    ) -> None: ...

    def create(
        self,
        *,
        trigger_reason: str,
        session_id: str | None = None,
        consecutive_failures: int = 0,
    ) -> str:
        """Create a new snapshot. Returns ckpt_id. Rotates ring as needed."""

    def schedule_restore(self, ckpt_id: str) -> None:
        """Mark a checkpoint as pending_restore. Applied on next MemoryStore open.
        Raises FileNotFoundError if the snapshot file is missing on disk."""

    def list(self) -> list[dict]:
        """Return all metadata rows, newest first."""

    def latest(self) -> dict | None:
        """Return newest metadata row or None."""


def apply_pending_restore_if_any(db_path: Path) -> str | None:
    """Module-level helper called from MemoryStore.__init__ BEFORE any schema
    migrations. If a pending_restore is set, copy the snapshot file over
    claw.db and mark restored_at. Returns the applied ckpt_id or None."""
```

Only one pending_restore at a time: `schedule_restore` first clears any existing `pending_restore = 1` row (sets it back to 0) before marking its target.

---

## 4. Flows

### 4.1 Create

```
1. hex = secrets.token_hex(4)
2. ckpt_id = f"ckpt_{hex}"
3. file_path = snapshots_dir / f"{ckpt_id}.db"
4. snapshots_dir.mkdir(parents=True, exist_ok=True)
5. with memory._lock:
6.   target_conn = sqlite3.connect(file_path)
7.   try: memory._conn.backup(target_conn, pages=100, sleep=0.001)
8.   finally: target_conn.close()
9.   INSERT INTO checkpoints (...) VALUES (...)
10.  rotate_ring()
11. return ckpt_id

On backup() failure: unlink(file_path, missing_ok=True); re-raise.
On INSERT failure: unlink(file_path); re-raise.
```

### 4.2 Schedule restore

```
1. with memory._lock:
2.   row = SELECT ckpt_id, file_path FROM checkpoints WHERE ckpt_id = ?
3.   if not row: raise KeyError(ckpt_id)
4.   if not Path(row.file_path).exists(): raise FileNotFoundError(...)
5.   UPDATE checkpoints SET pending_restore = 0 WHERE pending_restore = 1
6.   UPDATE checkpoints SET pending_restore = 1 WHERE ckpt_id = ?
7.   commit
```

### 4.3 Apply pending restore (module-level, called from `MemoryStore.__init__`)

```
1. If db_path does not exist: return None.
2. Open a temporary read-only sqlite3 connection to db_path.
3. If checkpoints table does not exist (first-ever boot): close, return None.
4. row = SELECT ckpt_id, file_path FROM checkpoints
        WHERE pending_restore = 1 LIMIT 1.
5. Close temporary connection.
6. If no row: return None.
7. If file_path does not exist on disk: log warning, return None.
8. shutil.copy(file_path, db_path)   # overwrites claw.db
9. Reopen a short-lived connection to mark the restore applied:
      UPDATE checkpoints
      SET pending_restore = 0, restored_at = CURRENT_TIMESTAMP
      WHERE ckpt_id = ?
10. Close. Log info.
11. Return ckpt_id.
```

The caller (`MemoryStore.__init__`) then proceeds to its normal `executescript(SCHEMA)` + `_migrate()` on the newly restored file. Because migrations are idempotent (`CREATE TABLE IF NOT EXISTS`, guarded `ALTER TABLE`), a restore of a snapshot taken on an older schema version is normalized forward automatically.

---

## 5. Brain Integration

### 5.1 Trigger A — pre-critical-action snapshot

In `BrainService.execute_critical_action()` (`claw_v2/brain.py`), at the three call sites that currently invoke `executor()`:
- `status == "executed_autonomously"` (around line 629)
- `status == "executed"` (around line 646)
- `status == "executed_with_approval"` (around line 663)

Insert BEFORE `executor()`:

```python
ckpt_id: str | None = None
if self.checkpoint is not None:
    try:
        ckpt_id = self.checkpoint.create(
            trigger_reason=f"pre-critical-action:{action[:80]}",
        )
    except Exception:
        logger.warning("Pre-action checkpoint failed", exc_info=True)
```

The `ckpt_id` is included in `CriticalActionExecution.checkpoint_id` (new field, Optional[str]) and in the observe event payload from `_emit_execution_event` (extend the payload with `checkpoint_id`). When `CheckpointService` is None (off via config), the flow proceeds without a checkpoint — it is defense-in-depth, not a hard gate.

### 5.2 Trigger D — automatic rollback after 3 consecutive failures

In `BrainService._emit_verification_outcome()` (the helper added in Experience Replay Task 11), immediately after the `record_cycle_outcome` call:

```python
if self.learning is not None and self.checkpoint is not None:
    consecutive = _count_recent_consecutive_failures(
        self.memory, task_type=task_type, session_id=session_id,
        within_minutes=30,
    )
    if consecutive >= 3:
        latest = self.checkpoint.latest()
        if latest is None:
            self.observe.emit("auto_rollback_unavailable", payload={...})
        else:
            self.observe.emit(
                "auto_rollback_proposed",
                payload={
                    "ckpt_id": latest["ckpt_id"],
                    "consecutive_failures": consecutive,
                    "session_id": session_id,
                    "autonomy_mode": <from session_state>,
                },
            )
            if autonomy_mode == "autonomous":
                self.checkpoint.schedule_restore(latest["ckpt_id"])
            # assisted mode: observe event stays; operator acts via /rollback.
```

Helper `_count_recent_consecutive_failures` reads `task_outcomes` ordered by `id DESC` within the last 30 minutes, filters by optional `task_type` and `session_id`, and counts contiguous rows with `outcome == "failure"` starting from the newest. Stops at the first non-failure.

### 5.3 Manual commands (Telegram)

New handlers in `claw_v2/bot_commands.py`:

- `/checkpoints list` → show up to N=10 rows: `ckpt_id · age · trigger_reason · session_id`.
- `/rollback <ckpt_id>` or `/rollback last` → invokes `checkpoint.schedule_restore(...)`.
  - Reply: `"checkpoint ckpt_xxx marcado. Ejecuta /restart para aplicar."`
  - `/rollback last` resolves via `checkpoint.latest()`.
- Invalid `ckpt_id` → reply with error, no state change.

Phase 1 does not add automatic restart. Application is via existing restart pathway.

---

## 6. Testing Strategy

New test file: `tests/test_checkpoint.py`. Extensions to `tests/test_memory_core.py`, `tests/test_brain_verify.py`, and `tests/test_bot.py`.

### 6.1 CheckpointService unit tests

- `CheckpointCreateTests`
  - Create generates both file and row; `ckpt_id` format correct; `file_path` exists; `source_conn.backup()` is called with `pages=100` and `sleep=0.001`.
- `CheckpointRotationTests`
  - After 11 consecutive `create()` calls, exactly 10 files and 10 rows remain; the oldest by `created_at` is gone from both.
- `CheckpointScheduleRestoreTests`
  - Marks `pending_restore = 1`; clears any previous pending flag; raises `KeyError` for unknown ckpt_id; raises `FileNotFoundError` when file missing on disk.
- `CheckpointListTests`
  - Ordered desc by `created_at`; `latest()` returns first row; `latest()` returns None when empty.
- `CheckpointCreateFailureTests`
  - `backup()` raises → no file, no row; INSERT raises (simulated) → file removed; rotation failure → snapshot still created, warning logged.

### 6.2 Memory integration tests (`test_memory_core.py`)

- `CheckpointsTableSchemaTests` — tests the new table and indices exist after `MemoryStore` init.
- `ApplyPendingRestoreOnInitTests`
  - Set up a DB, record a distinctive fact, checkpoint it, mutate the DB, mark pending_restore for the checkpoint, reopen `MemoryStore`, verify the distinctive fact is present (proving the snapshot was applied) and the checkpoint row now has `restored_at` populated.
  - Second reopen with no pending_restore → no side effects.

### 6.3 Brain integration tests (`test_brain_verify.py`)

- `PreCriticalActionCheckpointTests`
  - Mock `CheckpointService.create` via MagicMock, invoke `_emit_execution_event` with various statuses, assert create() was called exactly once with the expected trigger_reason; `checkpoint_id` appears in the observe event payload.
- `ConsecutiveFailuresTriggerTests`
  - Preload 3 failure outcomes within last 30 minutes via `record_cycle_outcome`, call `_emit_verification_outcome` once more (making 4), autonomy_mode="autonomous" → assert `auto_rollback_proposed` emits AND `schedule_restore` is called with `latest()`.
- `AssistedModeDoesNotAutoRestoreTests`
  - Same scenario with autonomy_mode="assisted" → event emits, `schedule_restore` NOT called.
- `NoCheckpointAvailableTests`
  - 3 failures, checkpoint ring is empty → `auto_rollback_unavailable` emits instead, no schedule_restore call.
- `OldFailuresIgnoredTests`
  - 3 failures older than 30 minutes → no event fires, no rollback proposed.

### 6.4 Bot command tests (`test_bot.py` or new `test_bot_rollback.py`)

- `/rollback last` → calls schedule_restore with latest ckpt_id; success reply mentions `/restart`.
- `/rollback ckpt_abc123` → schedules exact ID.
- `/rollback ckpt_nonexistent` → error reply, no side effects.
- `/checkpoints list` → renders rows correctly; empty state handled.

---

## 7. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `backup()` blocks during intense writes | Emit `checkpoint_created` observe event with `duration_ms` payload; if >1s, surface a warning. |
| Disk usage grows if `claw.db` balloons | Emit warning when any snapshot > 50MB; document recommended ring size tuning. |
| Restore of old-schema snapshot incompatible with current code | Migrations are all `IF NOT EXISTS` + guarded `ALTER TABLE`; test explicitly covers restore-then-migrate. |
| `schedule_restore` never applied (Claw never restarts) | `/rollback` reply explicitly instructs `/restart`; observe event `pending_restore_set` allows heartbeat-level detection later. |
| Concurrent `create()` / `schedule_restore()` race | All mutations happen under `memory._lock` (the same lock used by every other `MemoryStore` write). |
| Telegram notification failure in assisted mode | `auto_rollback_proposed` stays in observe stream; Kairos/heartbeat can re-surface via alternate channel later. |

---

## 8. Success Criteria

A synthetic test scenario proves the end-to-end loop:

1. `MemoryStore` initialized with seed data; `CheckpointService.create()` taken.
2. Three `record_cycle_outcome(outcome="failure", ...)` emitted within 30 minutes in autonomous mode.
3. On the 3rd, `_emit_verification_outcome` triggers `auto_rollback_proposed` AND `schedule_restore`.
4. `MemoryStore` is closed and re-opened (simulating restart).
5. `apply_pending_restore_if_any` applies the snapshot; `restored_at` is set.
6. Seed data is present; failure outcomes from steps 2-3 are NOT (they post-date the snapshot).

All six verified by an integration test in `test_brain_verify.py`.

---

## 9. Open Questions Deferred to Phase 2

- Workspace filesystem snapshots (APFS `localsnapshot`, Time Machine, or git-based).
- Automatic restart after `schedule_restore` (requires coordinator-level change).
- Differential snapshots to reduce disk usage (currently full `.db` copy per snapshot).
- Retention by size or time (currently count-based only).
- Snapshot of sibling DBs (`buddy.db`, `observe.db` event store).
