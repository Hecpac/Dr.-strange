# F2 — Primary DB Compatibility Preflight (Design Spec)

Status: Design only, 2026-06-25. **Spec only — does NOT authorize enabling F2
live, primary-DB writes, daemon restart/launchctl, durable NotebookLM, Stage 2C2
seed/purge, or Stage 3 execution.** This tool is **read-only by construction**;
see the **Gates** section at the end.

Scope owner context: builds on the already-merged F2.0 (durability schema,
`claw_v2/f2_durability_schema.py`) and F2.1 (`F2DurabilityStore`,
`claw_v2/f2_durability_store.py`). Parallels the read-only precedents
`claw_v2/diagnostics.py` (marker `diagnostics_read_only`) and
`claw_v2/maintenance_preflight.py`. Aligns with the §1 invariants in
`claw_v2/INTERNAL_WIRING.md`. Assumes F2 durability stays OFF in production.

Operator decision that produced this spec (2026-06-25): **do not** build the
seed→verify→purge synthetic canary against the live primary DB now. The only
failure mode that canary would retire and that is not already covered is
`primary_f2_write_path_incompatibility`; but it does not retire the truly
critical risks (real crash recovery, WAL concurrency with the live daemon, a
real executor producing checkpoints/effects, the durable NotebookLM lane, real
external-effect dedup, Stage 3), so mutating the primary buys little against its
operational cost (downtime, DB surgery, purge, integrity risk, turning a
synthetic canary into another production special-case). Instead, build a
**read-only primary compatibility preflight** that retires the one useful
question — *is the real primary DB schema/state compatible with F2?* — with zero
writes, zero daemon downtime, and zero synthetic rows.

---

## 1. Problem

Before F2 durability is ever enabled live, its **first real write** to the
primary `data/claw.db` could fail, corrupt, or behave differently than it does
on a fresh temp DB, because of: schema drift between the running code's expected
F2 schema and what the primary actually has; missing or differing real
indexes/unique constraints; physical state of the primary (size, WAL/locking,
page state); or filesystem permissions. Call this failure mode
**`primary_f2_write_path_incompatibility`**.

Nothing today answers *"is the real primary compatible with the F2 schema the
current code expects?"* without either mutating the primary (the rejected
seed/purge canary) or inferring it from logic that runs only against temp DBs:

- The isolated `stage2c2_synthetic_canary.py` proves the F2 store/planner
  **logic**, but only on a temp DB it creates — its AST invariant
  (`stage2c2_synthetic_canary_uses_isolated_f2_state_only`) forbids it from
  touching the primary.
- Postura 2 (F2-ON-idle, 2026-06-25) proved the F2 store **initialises** clean
  against the live primary with the daemon running, but exercised no schema
  comparison and produced no operator-facing report.
- `diagnostics --f2-recovery-report` reads the primary read-only but reports
  recovery readiness, not schema compatibility.

## 2. Goals / Non-goals

**Goals**
- Answer, against the **real primary DB**, with **zero writes**: do the 4 F2
  tables exist with the columns and the unique indexes/constraints F2 needs?
- Report F2 table row counts and whether any F2 rows already exist (summarised
  safely, never dumped).
- Open the primary **read-only** in a way that is correct against a **live,
  actively-writing daemon** (F2 OFF) — no stale/inconsistent reads.
- Emit a structured, fail-closed JSON report + a human format, with a clear
  `does_not_prove` caveat and an operator runbook (backup + integrity gate).
- Provable read-only-ness: tests + an INTERNAL_WIRING invariant guaranteeing the
  tool never writes to a supplied DB path.

**Non-goals**
- Enabling F2 live, deploying, or any primary-DB write (still gated).
- The seed→verify→purge synthetic canary on the primary (explicitly rejected;
  reserved name if ever revisited: `primary_f2_write_path_incompatibility_canary`).
- Crash-recovery, WAL-concurrency, real-executor, durable-lane, dedup, or Stage 3
  validation — those are Gate #3 / Stage 3, designed separately.
- Repairing/migrating the primary schema (this only *reports*; repair is a
  separate, separately-gated action).
- Performing the backup itself as a side effect — the tool *requires/documents*
  it in the runbook (a backup is read-safe, but kept out of the read-only tool).

## 3. Success criteria

- Run against an isolated temp DB built via `F2DurabilityStore` →
  `overall_status: PASS`, `primary_db_touched: false`, counts before == after.
- Run against a temp DB missing an F2 table or a unique index → `FAIL` with the
  specific reason; `recommendation: NEEDS_REPAIR`.
- Run against the **live primary** (read-only, daemon up, F2 OFF) → a truthful
  compatibility verdict with **no write** to the primary and no mutation of its
  WAL/SHM state caused by the tool.
- Extra tables/columns/indexes present in the DB (migration surplus) do **not**
  fail the check (subset/⊇ semantics).
- `git diff --check` clean; targeted tests + existing F2 harness tests green;
  ruff clean.

## 4. Approach

A new, standalone, read-only module `claw_v2/f2_primary_compat_preflight.py`
(CLI `python -m claw_v2.f2_primary_compat_preflight`). Four refinements over the
operator's draft brief, each grounded in an existing repo pattern:

**4.1 `mode=ro` only — NOT `immutable=1`.** The repo has both patterns:
`maintenance_preflight.py:378` uses `?mode=ro&immutable=1` (but it runs with the
daemon in maintenance mode — gated, not writing), while `diagnostics.py` and
`sqlite_runtime.py:530` use plain `?mode=ro` against a potentially-live DB. This
preflight runs with the **daemon alive and writing** (F2 OFF; zero downtime is a
requirement). In WAL with an active writer, `immutable=1` tells SQLite the file
never changes, so it ignores the `-wal` file and reads a **stale/inconsistent
snapshot** → it would misreport schema/counts. A preflight whose entire job is to
report accurately must not misreport. → Use **`mode=ro` without `immutable`**,
copying `diagnostics._open_readonly_sqlite`
(`sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)`), and set
`PRAGMA query_only=ON` as defense-in-depth. (The SessionStart hook already shows
WAL-sidecar fragility on this DB — extra reason not to invent.)

**4.2 New module, do not extend the synthetic canary.**
`stage2c2_synthetic_canary.py`'s AST invariant forbids it from touching the
primary; extending it would violate its own invariant. The new module follows
the `diagnostics_read_only` precedent and accepts a real `--db-path` (the inverse
of the synthetic canary, which *refuses* `--db-path` because it writes — this one
*accepts* it because it only reads).

**4.3 Schema-diff = subset (⊇), not equality.** "Expected" schema is derived by
building a fresh temp DB via `F2DurabilityStore` and introspecting its
`sqlite_master` (tables, columns via `PRAGMA table_info`, indexes via
`PRAGMA index_list`/`index_info`, with the unique flag). The primary passes if it
**contains** everything F2 needs — the 4 tables, their required columns, and the
5 unique indexes (`ux_phase_checkpoints_task_run_phase_version`,
`ux_external_effect_records_idempotency_key`, `ux_phase_checkpoint_writes_order`,
`ux_phase_checkpoint_writes_key`, `ux_phase_recovery_cursors_task_run`). Extra
columns/indexes/tables are benign. Deriving expected from the store (not a
hand-list) means the check tracks `F2_DURABILITY_SCHEMA_VERSION` automatically.

**4.4 Integrity read-only; backup in the runbook.** The tool runs
`PRAGMA quick_check` (read-only) and sets `integrity_required: true`; the
operator runbook documents taking a backup (`sqlite3 .backup`, which is
read-safe even with a live writer) before any future F2-enable step. The tool
does not perform the backup.

## 5. Components / module shape

`claw_v2/f2_primary_compat_preflight.py`:

- `expected_f2_schema() -> ExpectedSchema` — build a temp `F2DurabilityStore`,
  introspect `sqlite_master`/PRAGMAs, return the canonical {tables → columns,
  unique-indexes} the running code expects, plus `F2_DURABILITY_SCHEMA_VERSION`.
- `_open_readonly(db_path) -> sqlite3.Connection` — `?mode=ro` URI +
  `PRAGMA query_only=ON`; fail closed if it cannot open read-only.
- `_check_schema(conn, expected) -> PathResult` — tables/columns subset check.
- `_check_indexes(conn, expected) -> PathResult` — each required unique index
  present, matched by name and verified by (table, column-set, uniqueness); the
  primary and the expected temp DB are created by the same schema code, so names
  match exactly.
- `_check_counts(conn) -> PathResult` — F2 table counts + non-empty summary.
- `_check_integrity(conn) -> PathResult` — `PRAGMA quick_check`.
- `run_primary_compat_preflight(*, db_path=None) -> dict` — orchestrates; when
  `db_path is None`, runs the temp-DB **smoke** (build expected, check against
  itself → PASS); fail-closed report on any exception.
- CLI `--db-path <path>` (opened read-only), `--temp-db` (smoke, default),
  `--json`.

`PathResult` mirrors the synthetic canary's `_PathResult` (status + reasons +
details) for report-shape consistency.

## 6. Output contract (structured JSON)

```text
overall_status:        PASS | FAIL
db_path_checked:       <path | "temp">
opened_read_only:      true
immutable_mode_used:   false          # explicit: NOT immutable (live-writer-safe)
primary_db_touched:    false          # touched == written; always false
schema_version_expected: <int>
schema_version_found:    <int | null>
schema_path:           PASS | FAIL
index_path:            PASS | FAIL
counts_path:           PASS | FAIL
integrity_path:        PASS | FAIL
integrity_required:    true
f2_table_counts:       {phase_checkpoints, phase_checkpoint_writes,
                        external_effect_records, phase_recovery_cursors}
non_empty_f2_tables:   [ ... ]
reasons:               [ ... ]
checks:                { schema, index, counts, integrity → details }
recommendation:        PRIMARY_COMPAT_PREFLIGHT_READY | NEEDS_REPAIR | BLOCKED
does_not_prove:        "<caveat: not crash-recovery / WAL-concurrency / real
                        executor / durable lane / dedup / Stage 3>"
```

`recommendation`: `READY` when all paths PASS; `NEEDS_REPAIR` on schema/index
mismatch; `BLOCKED` on open failure / unexpected exception / integrity failure.

## 7. Fail-closed behavior

Return a FAIL report (never raise to the operator) on: cannot open read-only;
any required table/column/unique-index missing; `quick_check` not "ok";
accidental mutable/write attempt; any unexpected exception (`overall_status:
FAIL`, `recommendation: BLOCKED`, reason carries the exception class). Every FAIL
report still asserts `primary_db_touched: false` / `opened_read_only: true`.

## 8. Testing (temp DBs only — never the primary)

- PASS on a temp DB built via `F2DurabilityStore`.
- FAIL (`NEEDS_REPAIR`) on a temp DB with an F2 table dropped.
- FAIL (`NEEDS_REPAIR`) on a temp DB with a required unique index dropped.
- PASS + empty summary when F2 tables exist but are empty.
- PASS + non-empty counts summary when seeded.
- Subset: extra table/column/index present → still PASS.
- JSON contains all required fields.
- Read-only enforcement: a primary-like temp path is opened `mode=ro`; a write
  through the tool's connection raises; `query_only` is ON.
- No-write proof: counts/integrity of the temp "primary" unchanged before/after.
- `does_not_prove` present and non-empty.

## 9. Operator runbook (read-only)

```text
# Read-only — safe with the daemon live (F2 OFF). Never writes.
python -m claw_v2.f2_primary_compat_preflight --db-path data/claw.db --json
# Before any FUTURE F2-enable step (separate gate): take a backup +
# verify integrity (the preflight reports integrity_required: true):
sqlite3 data/claw.db ".backup data/claw.db.bak-pre-f2-<ts>"
```

The preflight never restarts the daemon, never uses launchctl, never enables F2.

## 10. INTERNAL_WIRING invariant

Add invariant `primary_f2_compatibility_preflight_is_read_only`, documented in
`claw_v2/INTERNAL_WIRING.md` and enforced by the listed unit tests (the same
documented-invariant + `enforced_by` pattern as
`stage2c2_synthetic_canary_uses_isolated_f2_state_only` and
`maintenance_preflight` — not an AST scan):
the module must never construct a writing `RuntimeDb`/`F2DurabilityStore` against
a supplied `--db-path`, must open supplied paths `mode=ro` only, and must not
open with `immutable=1`. Document in INTERNAL_WIRING: this replaces the proposed
primary seed/purge canary for now; the failure mode it retires
(`primary_f2_write_path_incompatibility`); what it does NOT prove; and why Stage 3
stays separate. Bump `doc_version` + `describes_commit`/`last_verified`.

## 11. Gates (what this does NOT authorize)

This spec and the resulting tool are **read-only**. They do **not** authorize and
must not be read as authorizing:

1. Enabling F2 durability live (`CLAW_F2_DURABILITY_ENABLED`) — Gate B / Stage 2C2.
2. Any primary-DB write, seed, or purge.
3. Daemon restart / `launchctl` / deploy.
4. Durable NotebookLM (`CLAW_NOTEBOOKLM_RESEARCH_DURABLE`) — Gate #3.
5. Stage 3.

A `PRIMARY_COMPAT_PREFLIGHT_READY` result means only that the primary schema is
compatible — it is **not** a signal that enabling F2 live is safe. Each gate
above remains separate and unauthorized.

Related: `f2-notebooklm-research-durable-lane.md`,
`docs/architecture/f2_durability_design.md`.
