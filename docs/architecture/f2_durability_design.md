# F2 Durability Design

Status: Draft, 2026-06-23

Scope: design/spec only. This document does not implement runtime code, migrate
production data, deploy, restart, or add a LangGraph dependency.

## Roadmap Position

The F0 -> F6 LangGraph study decision is to port LangGraph-style patterns, not
adopt LangGraph runtime. F2 is the proposed checkpoint durability layer that
must sit between the already-live F1 RuntimeDb single-connection discipline and
later roadmap work:

- In short: port LangGraph-style patterns, not adopt LangGraph runtime.
- Current `main` baseline after docs PR #140 (`efd5185`): C4, F0.2d,
  F1/RuntimeDb and watchdog work, and browser atomic read-only tools are live
  and documented. Rollout still has to re-check the live daemon before enabling
  F2 behavior.
- F2: design-only, pending, and not live. It proposes durable phase checkpoints,
  incremental writes, external effect records, and recovery cursors inside
  `claw.db`. No F2
  `CheckpointSaver`, `checkpoint_writes`, or `external_effect_record` facility
  exists today.
- Watchdog and browser atomic tooling: current baseline and operational rollout
  constraints, not F2 implementation surfaces.
- F3: later lease/watchdog semantics. F2 must not depend on F3.
- F4: later forced-action gate. F2 must preserve current promote-gate and
  verification discipline.
- F5: later browser/runtime side-effect work. F2 only defines the generic
  external-effect ledger.
- F6: later dynamic fanout. F2 keeps the current fixed Coordinator phase model.

## Design Principles

- Use patterns, not runtime adoption: no LangGraph runtime dependency,
  `CheckpointSaver`, or graph executor.
- Use the existing production database: all proposed F2 tables live in
  `claw.db`.
- Use one owner: production reads and writes go through `RuntimeDb` only. No
  separate SQLite connections, no `data/checkpoints.db`.
- Keep F1 lock discipline: F2 storage APIs must use the RuntimeDb lock and
  transaction helpers.
- Treat observe events as diagnostics, not source of truth.
- Fail closed when recovery evidence is missing, inconsistent, or unverifiable.
- Never apply an irreversible external effect unless its intent was committed
  first.
- Never mark a task `succeeded` unless existing verification and promote-gate
  rules allow it.

## Current State Model

This section describes the current system F2 must fit, not replace.

### RuntimeDb

`RuntimeDb` owns a single SQLite connection, a shared `threading.RLock`, explicit
`cursor()` and `transaction()` context managers, and non-blocking `try_cursor()`
for diagnostic paths (`claw_v2/sqlite_runtime.py`). The implementation rejects
nested transactions, commits only at the outer transaction boundary, and exposes
a connection facade for existing stores.

F2 storage must be implemented as RuntimeDb-owned store methods. If a future
Coordinator transition needs to update F2 tables plus TaskLedger plus
orchestration rows atomically, the API must accept an existing transaction cursor
or provide a single RuntimeDb method that owns the whole transaction. It must not
open another connection for convenience.

### Coordinator Phases

The current Coordinator has a fixed phase order:

1. `research`
2. `synthesis`
3. `implementation`
4. `verification`

`Coordinator.run(..., start_phase=...)` uses scratch artifacts to skip already
completed phases. `detect_resume_phase(task_id)` returns the first phase with a
missing scratch output. The implementation phase also writes an
`implementation.started` scratch marker and blocks rerun if that marker exists.

F2 should keep this phase model in F2.2. The resume source of truth should move
from scratch files to `phase_checkpoints` and `phase_checkpoint_writes`; scratch
files can remain a cache/export artifact until a later cleanup.

### Orchestration Store

The current orchestration layer already has `orchestration_runs`,
`orchestration_events`, `orchestration_artifacts`, `orchestration_acks`, and
`orchestration_checkpoints`. `orchestration_checkpoints` is a control-plane
checkpoint pointer for orchestration state and eventing; it is not a phase
checkpoint saver with replay semantics.

F2 must integrate with this store but not overload the existing table name.
When the roadmap says `checkpoints` or `checkpoint_writes`, this design maps
that intent to `phase_checkpoints` and `phase_checkpoint_writes` to avoid
colliding with existing snapshot and orchestration tables.

### TaskHandler

`TaskHandler` is the main bridge between inbound work, Coordinator execution,
session state, JobService, TaskLedger, and user-facing responses. It currently:

- calls `Coordinator.detect_resume_phase()` when resuming coordinator work;
- stores high-level checkpoint dictionaries in `session_state.last_checkpoint`;
- records running checkpoints with `TaskLedger.mark_running_checkpoint()`;
- applies the promote-gate artifact lift before terminal success paths;
- resumes interrupted autonomous tasks from ledger/job metadata and existing
  checkpoint dictionaries.

F2 should not replace these surfaces in one patch. It should first make them
consume a durable recovery cursor and phase checkpoint summary, then keep writing
the high-level `last_checkpoint` field for compatibility.

### TaskLedger

`TaskLedger` owns durable task status in `agent_tasks`. It already prevents false
success: `mark_terminal(... status="succeeded" ...)` validates completion and can
park work as `completed_unverified` with `task_false_success_prevented`.

F2 must preserve this invariant. A replayed checkpoint may prove what phase
returned, but it does not by itself prove success. Terminal success still needs
verification evidence and the existing promote-gate artifact lift behavior from
PR #128 once that separate PR is merged. This document does not claim #128 is
merged in `main`.

### JobService

`JobService` owns `agent_jobs` with `checkpoint_json`, `resume_key`, retry state,
claiming, completion, deferral, and stale-running recovery. The unique active
`resume_key` already prevents duplicate active jobs for the same resumable unit.

F2 should treat JobService checkpoint JSON as job-level scheduling state, not as
phase replay state. The two must be cross-linked by `task_id`, `run_id`, and
`job_id` where available.

### Session State Checkpoint Field

`session_state.last_checkpoint_json` exists today as a compatibility and UX
surface. It can hold Coordinator checkpoint summaries, blocked state, or
completion summaries. It is not normalized, versioned phase history.

F2 should keep writing this field as a compact pointer/summary:

```json
{
  "durability_version": 2,
  "task_id": "...",
  "run_id": "...",
  "latest_phase": "verification",
  "phase_checkpoint_id": "...",
  "recovery_cursor_id": "..."
}
```

The canonical replay data would live in the proposed F2 tables once F2 is
implemented and enabled.

### Existing `checkpoints` Table

`claw_v2/checkpoint.py` and `claw_v2/memory.py` already define a `checkpoints`
table for database snapshot/rollback behavior. F2 must not reuse or rename this
table. The F2 table names are intentionally explicit: `phase_checkpoints`,
`phase_checkpoint_writes`, `external_effect_records`, and
`phase_recovery_cursors`.

### Observe Events

`observe.emit()` is useful for visibility and post-deploy diagnostics. It should
emit events after F2 commits succeed, and it may fast-drop under lock contention.
Therefore observe events must never be required to recover a phase, prove an
external effect, or reconstruct terminal status.

## F2 Concepts

### Phase Checkpoint

A phase checkpoint is the durable, versioned record for the latest known state of
a task phase. It is roughly the local equivalent of a LangGraph checkpoint
record, scoped to the current Coordinator phases.

Required properties:

- one `task_id`;
- one `run_id` for a specific Coordinator run attempt;
- one `phase`;
- monotonic `phase_version`;
- `status` such as `started`, `succeeded`, `failed`, `blocked`, or
  `recovery_required`;
- payload hash and schema version;
- link to the latest write order included in the checkpoint;
- optional link to orchestration run/checkpoint ids.

### Checkpoint Writes / Incremental Writes

Checkpoint writes are append-only phase deltas. They preserve intermediate
progress that should survive crashes before the whole phase returns. This ports
the useful LangGraph `put_writes` idea without adopting its runtime.

Examples:

- `phase_started`
- `worker_started`
- `worker_return`
- `worker_error`
- `artifact_recorded`
- `approval_wait`
- `external_effect_intent`
- `external_effect_result`
- `phase_return`
- `phase_error`
- `recovery_cursor`

Writes are append-only and ordered by `(task_id, run_id, phase, write_order)`.
The latest phase checkpoint points to the last included write.

### External Effect Record

An external effect record is a durable intent/result row for an irreversible or
expensive operation outside `claw.db`. Examples include GitHub push/PR actions,
social publish, file deletion outside scratch policy, email/send actions, and
future browser operations with non-idempotent consequences.

The intent row must be committed before the adapter call. The result row/update
must be committed after the adapter returns or after a verifier proves the effect
already happened.

### Idempotency Key

Every external effect needs a deterministic idempotency key:

```text
sha256(task_id || run_id || phase || effect_kind || target || content_hash || schema_version)
```

The exact target canonicalization is effect-specific, but the rule is stable:
same intended external mutation, same key; materially different mutation,
different key. Store both the key and the individual fields so operators can
debug without recomputing.

The table should store the individual fields that feed the idempotency key for
operator debugging, but the proposed database enforcement point is
`UNIQUE(idempotency_key)`. The design does not add a second composite unique
constraint over `(task_id, run_id, phase, effect_kind, target, content_hash)`.

When the target cannot be safely canonicalized, the effect must be treated as
not idempotent and require explicit recovery verification before retry.

### Recovery Cursor

A recovery cursor is the answer to "where may execution safely resume?" It is a
durable row derived from phase checkpoints, checkpoint writes, and external
effect records. It should be explicit instead of recomputed ad hoc in
TaskHandler.

Cursor statuses:

- `ready_to_start_phase`
- `ready_to_replay_completed_phase`
- `ready_to_resume_phase`
- `effect_verification_required`
- `blocked_manual_review`
- `terminal_recovery_complete`

The cursor is updated in the same transaction as the checkpoint write that makes
it true.

### Resume Semantics

Resume must follow this order:

1. Load task/job/session context.
2. Load the latest valid recovery cursor for `task_id` and `run_id`.
3. Load phase writes up to the cursor's `last_write_order`.
4. Rehydrate completed phases from `phase_return` writes.
5. For an incomplete phase, inspect external effect records before doing work.
6. If an effect intent exists without an applied/verified result, enter
   verification recovery before any retry.
7. If the verifier proves the effect happened, record `verified_applied` and
   continue from after the effect.
8. If the verifier proves absence and policy allows retry, reuse the same
   idempotency key and mark the retry attempt.
9. If absence cannot be proven, fail closed into manual review.

### Schema Versioning

All F2 rows store:

- `schema_version`;
- `payload_json`;
- `payload_sha256`;
- `created_at`;
- `updated_at` where mutable.

Readers must reject newer unsupported schema versions and emit a diagnostic
event. F2.0 should start at `schema_version = 1` for these tables even though the
feature is roadmap F2.

## Database Design

All tables in this section are proposed additive migrations inside `claw.db`.
This document does not create them. A future implementation must create them
through RuntimeDb-owned startup/migration code after the migration gate passes.

### `phase_checkpoints`

Purpose: latest durable checkpoint snapshots per task/run/phase/version.

Columns:

- `checkpoint_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `run_id TEXT NOT NULL`
- `job_id TEXT`
- `session_id TEXT`
- `phase TEXT NOT NULL`
- `phase_version INTEGER NOT NULL`
- `status TEXT NOT NULL`
- `schema_version INTEGER NOT NULL`
- `last_write_order INTEGER NOT NULL DEFAULT 0`
- `payload_json TEXT NOT NULL`
- `payload_sha256 TEXT NOT NULL`
- `orchestration_run_id TEXT`
- `orchestration_checkpoint_id TEXT`
- `created_at TEXT NOT NULL`

Indexes and constraints:

- `UNIQUE(task_id, run_id, phase, phase_version)`
- `INDEX(task_id, run_id, phase, phase_version DESC)`
- `INDEX(task_id, status, created_at DESC)`
- Phase names should be validated at the Coordinator/RuntimeDb store layer, not
  hard-coded in a SQLite `CHECK` constraint. That keeps F2 compatible with later
  phase additions without requiring a table recreation migration.

### `phase_checkpoint_writes`

Purpose: append-only incremental writes that can reconstruct phase state between
snapshots.

Columns:

- `write_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `run_id TEXT NOT NULL`
- `job_id TEXT`
- `phase TEXT NOT NULL`
- `write_order INTEGER NOT NULL`
- `write_kind TEXT NOT NULL`
- `write_key TEXT`
- `schema_version INTEGER NOT NULL`
- `payload_json TEXT NOT NULL`
- `payload_sha256 TEXT NOT NULL`
- `external_effect_id TEXT REFERENCES external_effect_records(external_effect_id)`
- `created_at TEXT NOT NULL`

Indexes and constraints:

- `UNIQUE(task_id, run_id, phase, write_order)`
- `INDEX(task_id, run_id, phase, write_order)`
- `INDEX(external_effect_id)` for effect-linked writes

The non-null write-key uniqueness rule should be a partial unique index, not a
table-level constraint:

```sql
CREATE UNIQUE INDEX idx_phase_checkpoint_writes_key
ON phase_checkpoint_writes(task_id, run_id, phase, write_kind, write_key)
WHERE write_key IS NOT NULL;
```

`write_key` is the local idempotency key for replayable writes such as
`artifact_recorded` or `approval_wait`; it is distinct from external effect
idempotency keys.

### `external_effect_records`

Purpose: before/after ledger for non-idempotent or externally visible effects.

Columns:

- `external_effect_id TEXT PRIMARY KEY`
- `idempotency_key TEXT NOT NULL`
- `task_id TEXT NOT NULL`
- `run_id TEXT NOT NULL`
- `job_id TEXT`
- `phase TEXT NOT NULL`
- `effect_kind TEXT NOT NULL`
- `target TEXT NOT NULL`
- `content_hash TEXT NOT NULL`
- `request_json TEXT NOT NULL`
- `request_sha256 TEXT NOT NULL`
- `status TEXT NOT NULL`
- `attempt_count INTEGER NOT NULL DEFAULT 0`
- `verifier_kind TEXT`
- `verification_json TEXT`
- `result_json TEXT`
- `result_sha256 TEXT`
- `error TEXT`
- `schema_version INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Statuses:

- `intent_recorded`
- `apply_in_progress`
- `applied`
- `failed`
- `verification_required`
- `verified_applied`
- `verified_absent`
- `blocked_manual_review`

Indexes and constraints:

- `UNIQUE(idempotency_key)`
- `INDEX(task_id, run_id, phase, status)`
- `INDEX(status, updated_at)`

### `phase_recovery_cursors`

Purpose: explicit resume cursor for TaskHandler and Coordinator.

Columns:

- `recovery_cursor_id TEXT PRIMARY KEY`
- `task_id TEXT NOT NULL`
- `run_id TEXT NOT NULL`
- `job_id TEXT`
- `session_id TEXT`
- `phase TEXT NOT NULL`
- `cursor_status TEXT NOT NULL`
- `last_checkpoint_id TEXT REFERENCES phase_checkpoints(checkpoint_id)`
- `last_write_order INTEGER NOT NULL DEFAULT 0`
- `external_effect_id TEXT REFERENCES external_effect_records(external_effect_id)`
- `resume_payload_json TEXT NOT NULL`
- `schema_version INTEGER NOT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Indexes and constraints:

- `UNIQUE(task_id, run_id)`
- `INDEX(task_id, cursor_status, updated_at DESC)`
- `INDEX(external_effect_id)`

Only one active cursor should exist per task/run. Historical cursor movement is
recoverable from checkpoint writes; the cursor table is the fast authoritative
pointer.

## Transaction Boundaries

### Phase Start

Before a phase begins real work:

1. Insert a `phase_started` checkpoint write.
2. Insert a `phase_checkpoints` row with `status='started'`.
3. Upsert `phase_recovery_cursors` to `ready_to_resume_phase`.
4. Optionally emit observe diagnostics after commit.

For `implementation`, this replaces the safety role of the scratch
`implementation.started` marker. During rollout, the marker may still be written
as a compatibility artifact, but F2 recovery must trust the database first.

### Phase Return

When a phase completes:

1. Insert all final writes, including `artifact_recorded` and `phase_return`.
2. Insert the next `phase_checkpoints` row with `status='succeeded'`.
3. Update `phase_recovery_cursors` to the next phase or terminal recovery state.
4. Record/update orchestration artifacts and orchestration checkpoint in the
   same RuntimeDb transaction.
5. Update TaskLedger running checkpoint where needed in the same transaction.
6. Commit.
7. Emit observe diagnostics after commit.

If current store APIs cannot participate in the same transaction because they
commit internally, F2.1 must add transaction-friendly internal methods before
F2.2 wires Coordinator writes.

### Phase Error / Block

When a phase errors or blocks:

1. Insert `phase_error` or `approval_wait` write.
2. Insert `phase_checkpoints` row with `failed` or `blocked`.
3. Update recovery cursor to `blocked_manual_review` or the precise retryable
   status.
4. Update TaskLedger/JobService state in the same transaction where the current
   flow owns that transition.

### External Effect Intent

Before calling a non-idempotent adapter:

1. Canonicalize target and request.
2. Compute `content_hash`, `request_sha256`, and `idempotency_key`.
3. Insert `external_effect_records(status='intent_recorded')`.
4. Insert a linked `phase_checkpoint_writes(write_kind='external_effect_intent')`.
5. Update recovery cursor to `effect_verification_required` or
   `ready_to_resume_phase` with the effect pointer.
6. Commit.
7. Only after commit, call the adapter.

If the intent insert conflicts, do not blindly call the adapter. Load the
existing row and apply recovery semantics.

### External Effect Result

After the adapter returns:

1. Update `external_effect_records` to `applied` or `failed`.
2. Store result or error hashes.
3. Insert `external_effect_result` checkpoint write.
4. Update the recovery cursor.
5. Commit.

If the process crashes after the adapter call and before result commit, resume
will find `intent_recorded` or `apply_in_progress` and must verify externally
before retrying.

## Execution Semantics

### Reconstructing Phase State

Reconstruction uses database state in this order:

1. latest supported `phase_checkpoints` row;
2. checkpoint writes after the checkpoint's base point up to
   `last_write_order`;
3. linked orchestration artifacts for large payloads where the checkpoint stores
   only hashes/pointers;
4. scratch files only as cache/export, never as authority.

If payload hashes do not match, recovery stops and marks the task blocked for
manual review.

### Avoiding Double-Apply

F2 prevents double-apply by combining:

- durable intent-before-effect rows;
- unique idempotency keys;
- effect-specific verification;
- recovery cursor states that force verification before retry;
- fail-closed behavior when verification is unavailable.

Adapters for irreversible effects must be wrapped so there is no code path from
Coordinator/TaskHandler to the adapter without an external effect record. Read
only tools do not need external effect records, but their results may be stored
as checkpoint writes if needed for replay.

### Terminal Success

F2 phase success is not task success. A completed verification phase can propose
terminal success only after:

- verification evidence exists;
- TaskLedger validation accepts it;
- the promote-gate artifact lift from PR #128 has attached the
  success-condition artifact envelope expected by the terminal path once that
  separate PR is merged;
- no external effect record remains in `intent_recorded`,
  `apply_in_progress`, or `verification_required`.

If any condition is missing, TaskLedger should keep or move the task to
`completed_unverified`, `running`, `failed`, or blocked state according to the
existing verification discipline.

## Retention

Default policy:

- Keep all F2 rows for active, running, blocked, and non-terminal tasks.
- Keep terminal task phase checkpoints and writes for at least 30 days or the
  configured task-retention window, whichever is longer.
- Keep `external_effect_records` longer than phase writes because they are
  audit/idempotency evidence; default 180 days.
- Never delete an external effect record while any task, job, or recovery cursor
  references it.
- Retention jobs must run through RuntimeDb and must not hold write locks while
  doing slow external verification.

## Migration and Backfill

F2 migrations are proposed to be additive when implementation starts:

1. Add the four F2 tables if missing.
2. Add indexes.
3. Add a lightweight `runtime_schema_migrations` or reuse the existing migration
   mechanism if RuntimeDb already has one by F2.0 implementation time.
4. Run `PRAGMA quick_check` in the migration gate.
5. Verify RuntimeDb is the only production writer path.

No production backfill is required for old tasks. Existing `last_checkpoint_json`,
JobService `checkpoint_json`, scratch artifacts, and orchestration checkpoints
remain readable for legacy recovery paths until F2 is enabled per task.

Optional dry-run backfill may compute synthetic phase summaries for diagnostics,
but it must not become authoritative and must not mutate production DBs outside
the approved migration path.

## Rollout Plan

Feature flags:

- `CLAW_F2_DURABILITY=0|1`: enable F2 authoritative writes and reads.
- `CLAW_F2_DURABILITY_DRY_RUN=0|1`: compute and emit diagnostics without
  changing resume authority.
- `CLAW_F2_EFFECT_RECORDS_ENFORCE=0|1`: require external effect records before
  irreversible adapters.

Migration gate:

- RuntimeDb F1 single-connection discipline verified in the running process.
- DB path is the expected `claw.db`.
- F2 tables and indexes exist.
- `PRAGMA quick_check` passes.
- No observed `database is locked` regression under the F2 concurrency test.
- Feature flags are logged in startup diagnostics.

Deploy sequence:

1. Ship F2.0/F2.1 with flags off.
2. Enable dry-run diagnostics for local/test only.
3. Enable dry-run diagnostics in production after the migration gate passes.
4. Compare scratch-based resume decisions with F2 proposed cursors.
5. Enable authoritative phase checkpoints for low-risk phases.
6. Enable external-effect enforcement only after wrappers and verifiers are
   covered by tests.
7. Remove reliance on scratch resume only after at least one full task lifecycle
   proves F2 recovery events in production.

Rollback:

- Turn off `CLAW_F2_DURABILITY` to return to legacy resume behavior.
- Turn off `CLAW_F2_EFFECT_RECORDS_ENFORCE` to stop enforcement.
- Leave additive tables in place; do not drop tables during incident rollback.
- Keep observe diagnostics available to compare F2 state against legacy state.

## PR Breakdown

### F2.0 Schema and Design Tests

Deliverables:

- Add migration/spec tests for F2 table shape, constraints, and indexes.
- Add architecture invariant tests proving F2 does not introduce
  `data/checkpoints.db`, direct SQLite connections, or non-RuntimeDb production
  writes.
- Add fixture builders for phase checkpoint payloads and external effect records.

Acceptance criteria:

- Tests fail on `checkpoints` table name reuse for F2.
- Tests fail on any production `sqlite3.connect()` introduced outside RuntimeDb
  approved setup/test paths.
- Migration is additive and idempotent.
- No runtime behavior changes with all F2 flags off.

### F2.1 RuntimeDb Storage Layer

Deliverables:

- Add RuntimeDb-owned storage APIs for `phase_checkpoints`,
  `phase_checkpoint_writes`, `external_effect_records`, and
  `phase_recovery_cursors`.
- Add transaction-friendly methods that can share one RuntimeDb transaction with
  TaskLedger and orchestration updates.
- Add payload hash validation and schema-version rejection.

Acceptance criteria:

- All F2 writes occur under RuntimeDb lock/transaction helpers.
- No nested transaction errors in normal Coordinator/TaskHandler paths.
- Hash mismatch or unsupported schema version fails closed.
- Read APIs can reconstruct a phase from checkpoint plus writes.

### F2.2 Phase Checkpoint Writes

Deliverables:

- Write `phase_started`, `phase_return`, `phase_error`, and artifact writes for
  Coordinator phases.
- Update recovery cursor at phase boundaries.
- Keep current scratch outputs as compatibility artifacts.
- Keep session `last_checkpoint_json` as a compact pointer/summary.

Acceptance criteria:

- Crash after phase start resumes from F2 cursor, not scratch inference.
- Crash after phase return reconstructs the completed phase without rerunning it.
- Implementation phase rerun protection is enforced by DB state.
- Orchestration checkpoint/artifact updates and F2 checkpoint updates are atomic.

### F2.3 External Effect Records

Deliverables:

- Define effect wrapper API for irreversible adapters.
- Insert intent records before adapter calls.
- Update result records after adapter calls.
- Add verifier hooks for supported effect kinds.

Acceptance criteria:

- There is no supported irreversible effect path without a committed intent row.
- Duplicate intent uses the existing idempotency key and does not double-apply.
- Crash after intent but before adapter call resumes safely.
- Crash after adapter success but before result commit enters verification before
  retry.
- Effects without verifiers fail closed to manual review.

### F2.4 Recovery and Resume

Deliverables:

- Teach TaskHandler/Coordinator resume to load `phase_recovery_cursors`.
- Rehydrate completed phase outputs from F2 writes/artifact pointers.
- Handle orphaned effect intents with verification-first recovery.
- Preserve legacy fallback only when F2 is disabled for the task.

Acceptance criteria:

- Resuming a partially completed task chooses the F2 cursor when F2 is enabled.
- Legacy scratch/session/job checkpoint resume remains available when flags are
  off.
- Recovery never marks terminal success without TaskLedger verification.
- Recovery surfaces manual-review blockers with enough IDs to inspect state.

### F2.5 Observability and Diagnostics

Deliverables:

- Emit post-commit observe events for checkpoint writes, cursor movement, and
  effect records.
- Add diagnostic queries/reports for active cursors and unresolved effects.
- Add startup diagnostics showing F2 flags and migration readiness.

Acceptance criteria:

- Observe events are derived from committed DB state.
- Dropped observe events do not affect recovery.
- Diagnostics redact secrets and request bodies where required.
- Operators can answer: latest phase, latest cursor, unresolved external
  effects, and whether F2 is authoritative for a task.

## Exact Test Matrix

| Area | Proposed file | Scenario | Expected result |
| --- | --- | --- | --- |
| Schema | `tests/test_f2_durability_schema.py` | Fresh DB migration | F2 tables/indexes/constraints exist |
| Schema | `tests/test_f2_durability_schema.py` | Re-run migration | Idempotent, no duplicate index/table errors |
| Schema | `tests/test_f2_durability_schema.py` | Existing snapshot `checkpoints` table | F2 uses `phase_*` names only |
| Architecture | `tests/test_architecture_invariants.py` | Search for new `sqlite3.connect` production paths | Only RuntimeDb-approved paths pass |
| Storage | `tests/test_f2_checkpoint_store.py` | Insert phase start and return writes | Latest checkpoint reconstructs phase |
| Storage | `tests/test_f2_checkpoint_store.py` | Payload hash mismatch | Reader rejects and returns fail-closed error |
| Storage | `tests/test_f2_checkpoint_store.py` | Unsupported schema version | Reader rejects and emits diagnostic |
| Storage | `tests/test_f2_checkpoint_store.py` | Duplicate write key | Existing write is reused or conflict is explicit |
| Coordinator | `tests/test_f2_coordinator_checkpointing.py` | Crash after `phase_started` commit | Resume cursor points to same phase |
| Coordinator | `tests/test_f2_coordinator_checkpointing.py` | Crash after `phase_return` commit | Phase is not rerun |
| Coordinator | `tests/test_f2_coordinator_checkpointing.py` | Implementation started then crash | DB blocks unsafe implementation rerun |
| Coordinator | `tests/test_f2_coordinator_checkpointing.py` | Orchestration checkpoint write fails | F2 checkpoint rolls back too |
| External effects | `tests/test_f2_external_effect_records.py` | Adapter called without intent | Test wrapper raises/fails closed |
| External effects | `tests/test_f2_external_effect_records.py` | Duplicate idempotency key | No second adapter call |
| External effects | `tests/test_f2_external_effect_records.py` | Crash after intent before call | Resume may call adapter once |
| External effects | `tests/test_f2_external_effect_records.py` | Crash after call before result write | Resume verifies before retry |
| External effects | `tests/test_f2_external_effect_records.py` | No verifier available | Cursor becomes manual review |
| Recovery | `tests/test_f2_resume_recovery.py` | Completed research/synthesis, crash in implementation | Resume rehydrates prior phases and resumes implementation safely |
| Recovery | `tests/test_f2_resume_recovery.py` | External effect `intent_recorded` unresolved | Recovery enters `effect_verification_required` |
| Recovery | `tests/test_f2_resume_recovery.py` | Verified applied effect | Recovery records `verified_applied` and continues after effect |
| Recovery | `tests/test_f2_resume_recovery.py` | Verification evidence missing | Task is blocked, not succeeded |
| Migration | `tests/test_f2_migrations.py` | Legacy DB with jobs/session checkpoints | No destructive mutation; legacy fields remain |
| Migration | `tests/test_f2_migrations.py` | Dry-run mode | Diagnostics only; authoritative resume unchanged |
| Concurrency | `tests/test_sqlite_runtime.py` | Concurrent F2 writes/read diagnostics | No `database is locked`; observe can fast-drop |
| Concurrency | `tests/test_f2_checkpoint_store.py` | Multiple writers same task/run | Unique constraints preserve order/idempotency |
| Verification | `tests/test_task_ledger.py` | F2 recovered verification phase lacks evidence | `succeeded` is prevented |
| Promote gate | `tests/test_promote_gate.py` or existing gate tests | F2 terminal summary includes lifted artifact envelope | PR #128 behavior remains required |

Crash simulation should inject failure after each boundary:

- before phase-start transaction;
- after phase-start commit;
- after incremental write commit;
- before external effect intent commit;
- after intent commit before adapter call;
- after adapter call before result commit;
- after result commit before phase return;
- after phase return before TaskLedger terminal update.

Each crash test should assert both the resumed behavior and the absence of
duplicate external effects.

## Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| F2 accidentally creates a second SQLite writer | Architecture invariant tests and code review gate: RuntimeDb only |
| Existing stores commit internally and break atomicity | F2.1 adds transaction-friendly APIs before Coordinator integration |
| Table name confusion with existing `checkpoints` | Use `phase_checkpoints`; tests reject F2 reuse of `checkpoints` |
| External effect verifier cannot prove state | Fail closed to manual review; never retry blindly |
| Observe events appear authoritative | Document and test that recovery uses DB rows only |
| Payload JSON grows too large | Store hashes/pointers to orchestration artifacts for large payloads |
| Lock contention regresses F1 | Stress tests cover no `database is locked`; diagnostics use try paths |
| Legacy resume and F2 resume disagree | Dry-run compares proposed F2 cursor with legacy decision before enablement |
| Promote-gate artifact lift is bypassed | Terminal success tests require PR #128 envelope and TaskLedger validation |

## Global Acceptance Criteria

F2 is done only when:

- all F2 tables exist inside `claw.db` and are owned by RuntimeDb;
- phase checkpoints and writes can reconstruct the fixed Coordinator phases;
- recovery cursors replace scratch inference for F2-enabled tasks;
- every supported irreversible external effect has intent-before-effect records;
- unresolved or unverifiable effects fail closed;
- terminal success still requires TaskLedger verification and promote-gate
  artifact evidence;
- rollback is a flag change, not a destructive schema operation;
- concurrency tests show no `database is locked` regression.

## Non-Goals

- No LangGraph runtime adoption.
- No LangGraph `CheckpointSaver` dependency.
- No separate `checkpoints.db`.
- No dynamic fanout or F6 graph scheduling.
- No F3 leases.
- No F4 forced-action gate.
- No browser/F5 behavior changes.
- No weakening of F1 RuntimeDb single-writer discipline.
- No production DB edits from this design document.
