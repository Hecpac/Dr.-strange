# Claw v3 Schema Migrations

This document is the registry for v3-owned SQLite schema changes. The current
pre-v3 schema is marked with `PRAGMA user_version = 100`; every v3 migration
bumps that value by one.

## Rules

- Each v3 table has a single owner PR.
- Each migration records forward SQL, rollback SQL, and index ownership here.
- Rollback SQL is intended for development/test rollback. Production rollback
  requires an operator checkpoint or database backup first.
- Existing pre-v3 ad hoc migrations remain in their current modules until a
  later migration consolidation pass.

## Registry

| Version | Owner | Objects | Forward SQL | Rollback SQL |
|---------|-------|---------|-------------|--------------|
| 100 | PR#0.6 | baseline only | `PRAGMA user_version=100` | Manual restore from pre-v3 checkpoint; no tables are dropped |
| 101 | PR#0.7 | `idempotency_keys` | Create idempotency reservation table | `DROP TABLE IF EXISTS idempotency_keys` |
| 102 | PR#3 | `artifacts` | Create typed artifact store and trace/job/parent indexes | `DROP TABLE IF EXISTS artifacts` |
| 103 | PR#4 | `jobs`, `job_steps` | Create durable job and step journal tables | `DROP TABLE IF EXISTS job_steps; DROP TABLE IF EXISTS jobs` |

## Planned SQL

### Version 101: `idempotency_keys`

```sql
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    result TEXT
);
PRAGMA user_version=101;
```

Rollback:

```sql
DROP TABLE IF EXISTS idempotency_keys;
PRAGMA user_version=100;
```

### Version 102: `artifacts`

```sql
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    trace_id TEXT,
    root_trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    job_id TEXT,
    parent_artifact_id TEXT,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_trace ON artifacts(trace_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts(parent_artifact_id);
PRAGMA user_version=102;
```

Rollback:

```sql
DROP TABLE IF EXISTS artifacts;
PRAGMA user_version=101;
```

### Version 103: `jobs`, `job_steps`

```sql
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'queued',
    version INTEGER NOT NULL DEFAULT 1,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS job_steps (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt_id TEXT NOT NULL,
    operation_hash TEXT NOT NULL,
    idempotency_key TEXT UNIQUE NOT NULL,
    step_class TEXT NOT NULL DEFAULT 'pure',
    side_effect_ref TEXT,
    result_artifact_id TEXT,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_job_steps_job ON job_steps(job_id);
PRAGMA user_version=103;
```

Rollback:

```sql
DROP TABLE IF EXISTS job_steps;
DROP TABLE IF EXISTS jobs;
PRAGMA user_version=102;
```
