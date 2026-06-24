from __future__ import annotations

from claw_v2.sqlite_runtime import RuntimeDb

F2_DURABILITY_SCHEMA_VERSION = 1

PHASE_CHECKPOINTS_TABLE = "phase_checkpoints"
PHASE_CHECKPOINT_WRITES_TABLE = "phase_checkpoint_writes"
EXTERNAL_EFFECT_RECORDS_TABLE = "external_effect_records"
PHASE_RECOVERY_CURSORS_TABLE = "phase_recovery_cursors"

F2_DURABILITY_TABLES: tuple[str, ...] = (
    PHASE_CHECKPOINTS_TABLE,
    PHASE_CHECKPOINT_WRITES_TABLE,
    EXTERNAL_EFFECT_RECORDS_TABLE,
    PHASE_RECOVERY_CURSORS_TABLE,
)

F2_DURABILITY_INDEXES: tuple[str, ...] = (
    "ux_phase_checkpoints_task_run_phase_version",
    "idx_phase_checkpoints_task_status_created_at",
    "ux_phase_checkpoint_writes_order",
    "idx_phase_checkpoint_writes_external_effect_id",
    "ux_phase_checkpoint_writes_key",
    "ux_external_effect_records_idempotency_key",
    "idx_external_effect_records_task_run_phase_status",
    "idx_external_effect_records_status_updated_at",
    "ux_phase_recovery_cursors_task_run",
    "idx_phase_recovery_cursors_task_status_updated_at",
    "idx_phase_recovery_cursors_external_effect_id",
    "idx_phase_recovery_cursors_last_checkpoint_id",
)

F2_DURABILITY_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS phase_checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        job_id TEXT,
        session_id TEXT,
        phase TEXT NOT NULL,
        phase_version INTEGER NOT NULL,
        status TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        last_write_order INTEGER NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        orchestration_run_id TEXT,
        orchestration_checkpoint_id TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_phase_checkpoints_task_run_phase_version
    ON phase_checkpoints(task_id, run_id, phase, phase_version)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_phase_checkpoints_task_status_created_at
    ON phase_checkpoints(task_id, status, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS external_effect_records (
        external_effect_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL,
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        job_id TEXT,
        phase TEXT NOT NULL,
        effect_kind TEXT NOT NULL,
        target TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        request_json TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        verifier_kind TEXT,
        verification_json TEXT,
        result_json TEXT,
        result_sha256 TEXT,
        error TEXT,
        schema_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_external_effect_records_idempotency_key
    ON external_effect_records(idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_external_effect_records_task_run_phase_status
    ON external_effect_records(task_id, run_id, phase, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_external_effect_records_status_updated_at
    ON external_effect_records(status, updated_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS phase_checkpoint_writes (
        write_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        job_id TEXT,
        phase TEXT NOT NULL,
        write_order INTEGER NOT NULL,
        write_kind TEXT NOT NULL,
        write_key TEXT,
        schema_version INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        external_effect_id TEXT REFERENCES external_effect_records(external_effect_id),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_phase_checkpoint_writes_order
    ON phase_checkpoint_writes(task_id, run_id, phase, write_order)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_phase_checkpoint_writes_external_effect_id
    ON phase_checkpoint_writes(external_effect_id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_phase_checkpoint_writes_key
    ON phase_checkpoint_writes(task_id, run_id, phase, write_kind, write_key)
    WHERE write_key IS NOT NULL
    """,
    """
    CREATE TABLE IF NOT EXISTS phase_recovery_cursors (
        recovery_cursor_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        job_id TEXT,
        session_id TEXT,
        phase TEXT NOT NULL,
        cursor_status TEXT NOT NULL,
        last_checkpoint_id TEXT REFERENCES phase_checkpoints(checkpoint_id),
        last_write_order INTEGER NOT NULL DEFAULT 0,
        external_effect_id TEXT REFERENCES external_effect_records(external_effect_id),
        resume_payload_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_phase_recovery_cursors_task_run
    ON phase_recovery_cursors(task_id, run_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_phase_recovery_cursors_task_status_updated_at
    ON phase_recovery_cursors(task_id, cursor_status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_phase_recovery_cursors_external_effect_id
    ON phase_recovery_cursors(external_effect_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_phase_recovery_cursors_last_checkpoint_id
    ON phase_recovery_cursors(last_checkpoint_id)
    """,
)


def ensure_f2_durability_schema(runtime_db: RuntimeDb) -> None:
    """Create the F2.0 durability schema skeleton on a RuntimeDb-owned connection.

    This is intentionally an explicit migration helper only. It is not wired into
    daemon startup or Coordinator/TaskHandler checkpoint writes in F2.0.
    """
    with runtime_db.transaction() as cur:
        for statement in F2_DURABILITY_SCHEMA_STATEMENTS:
            cur.execute(statement)


def validate_f2_schema_version(schema_version: int) -> None:
    if schema_version != F2_DURABILITY_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported F2 durability schema_version: {schema_version}; "
            f"expected {F2_DURABILITY_SCHEMA_VERSION}"
        )
