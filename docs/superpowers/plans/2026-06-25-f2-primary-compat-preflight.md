# F2 Primary DB Compatibility Preflight — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only CLI/module that answers "is the real primary `data/claw.db` compatible with the F2 durability schema the current code expects?" without ever writing to the primary.

**Architecture:** A standalone module `claw_v2/f2_primary_compat_preflight.py`. The "expected" F2 schema is derived by building a fresh temp DB via `F2DurabilityStore` (which ensures the schema on construction) and introspecting it. A supplied `--db-path` is opened **`mode=ro` (NOT `immutable`)** + `PRAGMA query_only=ON` — correct against the live, actively-writing daemon in WAL — then checked for: required tables/columns (subset/⊇), required unique indexes, F2 row counts, and a read-only `quick_check`. Output is a structured, fail-closed JSON report. Read-only-ness is documented as an INTERNAL_WIRING invariant enforced by unit tests (the same pattern as `stage2c2_synthetic_canary_uses_isolated_f2_state_only` and `maintenance_preflight`).

**Tech Stack:** Python 3.13, stdlib `sqlite3` + `argparse` + `tempfile`, `unittest`, existing `claw_v2.sqlite_runtime.RuntimeDb` + `claw_v2.f2_durability_store.F2DurabilityStore` + `claw_v2.f2_durability_schema`. Tests via `.venv/bin/python -m pytest`. Lint via `uvx ruff`.

**Spec:** `docs/superpowers/specs/2026-06-25-f2-primary-compat-preflight-design.md`

**Key facts verified against the codebase (do not re-derive):**
- `F2DurabilityStore(runtime_db)` ensures the F2 schema on construction (idempotent; `f2_durability_store.py:851-854`). So `RuntimeDb(path)` + `F2DurabilityStore(db)` yields a DB with the 4 F2 tables + 12 indexes.
- F2 tables: `F2_DURABILITY_TABLES` = `("phase_checkpoints", "phase_checkpoint_writes", "external_effect_records", "phase_recovery_cursors")` (`f2_durability_schema.py`).
- The 5 **unique** indexes F2 creates: `ux_phase_checkpoints_task_run_phase_version`, `ux_external_effect_records_idempotency_key`, `ux_phase_checkpoint_writes_order`, `ux_phase_checkpoint_writes_key` (partial: `WHERE write_key IS NOT NULL`), `ux_phase_recovery_cursors_task_run`. (Plus auto-indexes for the 4 `TEXT PRIMARY KEY`s, which appear in introspection of both expected and primary and match identically.)
- `RuntimeDb(db_path)` constructor opens/creates the DB; `db.cursor()` and `db.transaction()` are context managers yielding a cursor with `.execute(...).fetchall()`/`.fetchone()` and `sqlite3.Row`-style **key access** (the existing canary does `cur.execute(...).fetchone()["c"]`); `db.close()` closes it.
- Read-only open pattern (copy): `claw_v2/diagnostics.py:_open_readonly_sqlite` → `sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)`.
- `F2_DURABILITY_SCHEMA_VERSION = 1`.

**WAL/read-only rule (critical):** the preflight opens the target `mode=ro` **only while a writer holds the DB open** (in prod: the live daemon; in tests: a `RuntimeDb` writer kept open for the duration of the call). That guarantees the `-shm` exists so the read-only connection sees the committed WAL snapshot. Never use `immutable=1` (it ignores the `-wal`, yielding a stale read against a live writer). Tests MUST keep a `RuntimeDb` writer open on the temp "primary" during each `--db-path` call.

**Running commands (worktree has no `.venv`):** all work happens in the worktree `/Users/hector/Projects/Dr.-strange/.worktrees/f2-primary-compat-preflight`. The `.venv` lives only at the repo root, but `claw_v2` resolves from the current working directory — so run every `python`/`pytest` command **with cwd = the worktree** using the absolute interpreter `/Users/hector/Projects/Dr.-strange/.venv/bin/python` (verified: this imports the worktree's `claw_v2`, not the main tree's). E.g. `/Users/hector/Projects/Dr.-strange/.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`. The `uvx ruff ...` commands work as-is from the worktree.

---

## File Structure

- Create: `claw_v2/f2_primary_compat_preflight.py` — the whole read-only preflight (expected-schema introspection, read-only open, 4 path checks, orchestration, report, CLI). One file, one responsibility.
- Create: `tests/test_f2_primary_compat_preflight.py` — unit tests (temp DBs only).
- Modify: `claw_v2/INTERNAL_WIRING.md` — add invariant `primary_f2_compatibility_preflight_is_read_only`; bump `doc_version` 2.36→2.37, update `describes_commit`/`last_verified`/`verification_method`.

---

## Task 1: Foundation — constants, dataclasses, read-only open, introspection, expected schema

**Files:**
- Create: `claw_v2/f2_primary_compat_preflight.py`
- Test: `tests/test_f2_primary_compat_preflight.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_f2_primary_compat_preflight.py`:

```python
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2 import f2_primary_compat_preflight as preflight
from claw_v2.f2_durability_schema import (
    F2_DURABILITY_SCHEMA_VERSION,
    F2_DURABILITY_TABLES,
)
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb

_EXPECTED_UNIQUE_INDEXES = {
    "ux_phase_checkpoints_task_run_phase_version",
    "ux_external_effect_records_idempotency_key",
    "ux_phase_checkpoint_writes_order",
    "ux_phase_checkpoint_writes_key",
    "ux_phase_recovery_cursors_task_run",
}


class ExpectedSchemaTests(unittest.TestCase):
    def test_expected_schema_has_f2_tables_and_unique_indexes(self) -> None:
        expected = preflight.expected_f2_schema()
        self.assertEqual(expected.schema_version, F2_DURABILITY_SCHEMA_VERSION)
        # All 4 F2 tables present with non-empty column lists.
        self.assertEqual(set(expected.tables), set(F2_DURABILITY_TABLES))
        for table, cols in expected.tables.items():
            self.assertTrue(cols, f"no columns introspected for {table}")
        # The 5 F2 unique indexes are a subset of what was introspected.
        self.assertTrue(
            _EXPECTED_UNIQUE_INDEXES.issubset(set(expected.unique_indexes)),
            f"missing F2 unique indexes: "
            f"{_EXPECTED_UNIQUE_INDEXES - set(expected.unique_indexes)}",
        )
        idem = expected.unique_indexes["ux_external_effect_records_idempotency_key"]
        self.assertTrue(idem.unique)
        self.assertEqual(set(idem.columns), {"idempotency_key"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`
Expected: FAIL — `ModuleNotFoundError`/`AttributeError` (module `f2_primary_compat_preflight` / `expected_f2_schema` not defined).

- [ ] **Step 3: Write minimal implementation**

Create `claw_v2/f2_primary_compat_preflight.py`:

```python
"""Read-only F2 primary DB compatibility preflight.

Answers: is the real primary ``data/claw.db`` compatible with the F2 durability
schema the current code expects? — WITHOUT mutating the primary, WITHOUT daemon
downtime, WITHOUT synthetic rows.

Read-only by construction: a supplied ``--db-path`` is opened ``mode=ro`` (NOT
``immutable``; the live daemon is a WAL writer, and ``immutable=1`` would read a
stale pre-WAL snapshot) plus ``PRAGMA query_only=ON``. The "expected" F2 schema
is derived by building a fresh temp DB via ``F2DurabilityStore`` and
introspecting it, so the check tracks ``F2_DURABILITY_SCHEMA_VERSION``
automatically.

What it proves: the primary has the F2 tables, the required columns, and the
required unique indexes the current code expects (subset/superset semantics),
and passes a read-only ``quick_check``.

What it does NOT prove: the live daemon's F2 write path, crash recovery, WAL
concurrency, a real executor producing checkpoints/effects, the durable
NotebookLM lane, external-effect dedup, or Stage 3. A PASS here is NOT a signal
that enabling F2 live (Gate B / Stage 2C2) is safe.

Usage:

    python -m claw_v2.f2_primary_compat_preflight --temp-db --json
    python -m claw_v2.f2_primary_compat_preflight --db-path data/claw.db --json

The supplied ``--db-path`` is only ever opened read-only.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claw_v2.f2_durability_schema import (
    F2_DURABILITY_SCHEMA_VERSION,
    F2_DURABILITY_TABLES,
)
from claw_v2.f2_durability_store import F2DurabilityStore
from claw_v2.sqlite_runtime import RuntimeDb

PASS = "PASS"
FAIL = "FAIL"
READY = "PRIMARY_COMPAT_PREFLIGHT_READY"
NEEDS_REPAIR = "NEEDS_REPAIR"
BLOCKED = "BLOCKED"

_DOES_NOT_PROVE = (
    "Read-only schema/index/integrity compatibility check only. Does NOT prove "
    "the live daemon's F2 write path, crash recovery, WAL concurrency, a real "
    "executor producing checkpoints/effects, the durable NotebookLM lane, "
    "external-effect dedup, or Stage 3. A PASS is NOT a signal that enabling F2 "
    "live (Gate B / Stage 2C2) is safe."
)


@dataclass(slots=True)
class _PathResult:
    status: str
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _IndexSpec:
    name: str
    unique: bool
    columns: tuple[str, ...]


@dataclass(slots=True)
class ExpectedSchema:
    schema_version: int
    tables: dict[str, tuple[str, ...]]
    unique_indexes: dict[str, _IndexSpec]


def _introspect(executor: Any) -> tuple[dict[str, tuple[str, ...]], dict[str, _IndexSpec]]:
    """Introspect F2 tables (columns) and their unique indexes via PRAGMAs.

    ``executor`` is anything with ``.execute(sql).fetchall()`` whose rows allow
    key access (a RuntimeDb cursor or a ``sqlite3.Connection`` with
    ``row_factory = sqlite3.Row``)."""
    tables: dict[str, tuple[str, ...]] = {}
    unique_indexes: dict[str, _IndexSpec] = {}
    for table in F2_DURABILITY_TABLES:
        cols = executor.execute(f"PRAGMA table_info({table})").fetchall()
        if cols:
            tables[table] = tuple(row["name"] for row in cols)
        for idx in executor.execute(f"PRAGMA index_list({table})").fetchall():
            if not int(idx["unique"]):
                continue
            name = idx["name"]
            info = executor.execute(f"PRAGMA index_info({name})").fetchall()
            unique_indexes[name] = _IndexSpec(
                name=name,
                unique=True,
                columns=tuple(row["name"] for row in info if row["name"] is not None),
            )
    return tables, unique_indexes


def expected_f2_schema() -> ExpectedSchema:
    """Build a fresh temp DB via F2DurabilityStore and introspect the canonical
    F2 schema the current code creates."""
    with tempfile.TemporaryDirectory(prefix="f2-compat-expected-") as tmpdir:
        db = RuntimeDb(Path(tmpdir) / "expected.db")
        try:
            F2DurabilityStore(db)  # ensures the F2 schema on construction
            with db.cursor() as cur:
                tables, unique_indexes = _introspect(cur)
        finally:
            db.close()
    return ExpectedSchema(
        schema_version=F2_DURABILITY_SCHEMA_VERSION,
        tables=tables,
        unique_indexes=unique_indexes,
    )


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open ``db_path`` strictly read-only (``mode=ro`` — never ``immutable``,
    which would ignore the live writer's WAL). ``query_only`` is belt-and-braces."""
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py
git commit -m "feat(f2): preflight foundation — expected schema introspection + read-only open"
```

---

## Task 2: The four read-only path checks

**Files:**
- Modify: `claw_v2/f2_primary_compat_preflight.py`
- Test: `tests/test_f2_primary_compat_preflight.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_f2_primary_compat_preflight.py` (before the `if __name__` guard):

```python
class PathCheckTests(unittest.TestCase):
    def _open_primary(self, tmpdir: str, *, build: bool = True) -> sqlite3.Connection:
        """Build a temp 'primary' with the F2 schema, keep a writer open (so the
        read-only open sees the WAL), and return a read-only connection."""
        path = Path(tmpdir) / "claw.db"
        wdb = RuntimeDb(path)
        if build:
            F2DurabilityStore(wdb)
        self.addCleanup(wdb.close)
        self._writers = getattr(self, "_writers", [])
        self._writers.append(wdb)
        conn = preflight._open_readonly(path)
        self.addCleanup(conn.close)
        return conn, wdb

    def test_schema_check_passes_on_matching_db(self) -> None:
        expected = preflight.expected_f2_schema()
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, _ = self._open_primary(tmpdir)
            result = preflight._check_schema(conn, expected)
        self.assertEqual(result.status, preflight.PASS)

    def test_schema_check_fails_on_missing_table(self) -> None:
        expected = preflight.expected_f2_schema()
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, wdb = self._open_primary(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP TABLE phase_recovery_cursors")
            result = preflight._check_schema(conn, expected)
        self.assertEqual(result.status, preflight.FAIL)
        self.assertTrue(any("missing_table:phase_recovery_cursors" in r for r in result.reasons))

    def test_index_check_fails_on_missing_unique_index(self) -> None:
        expected = preflight.expected_f2_schema()
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, wdb = self._open_primary(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP INDEX ux_external_effect_records_idempotency_key")
            result = preflight._check_indexes(conn, expected)
        self.assertEqual(result.status, preflight.FAIL)
        self.assertTrue(
            any("missing_unique_index:ux_external_effect_records_idempotency_key" in r
                for r in result.reasons)
        )

    def test_counts_check_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, _ = self._open_primary(tmpdir)
            result = preflight._check_counts(conn)
        self.assertEqual(result.status, preflight.PASS)
        self.assertEqual(result.details["non_empty_f2_tables"], [])
        self.assertEqual(
            set(result.details["f2_table_counts"]), set(F2_DURABILITY_TABLES)
        )

    def test_counts_check_nonempty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, wdb = self._open_primary(tmpdir)
            store = F2DurabilityStore(wdb)
            store.append_checkpoint_write(
                task_id="compat-probe", run_id="compat-probe", phase="implementation",
                write_kind="phase_started", payload={"event": "probe"},
            )
            result = preflight._check_counts(conn)
        self.assertEqual(result.status, preflight.PASS)
        self.assertIn("phase_checkpoint_writes", result.details["non_empty_f2_tables"])

    def test_integrity_check_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            conn, _ = self._open_primary(tmpdir)
            result = preflight._check_integrity(conn)
        self.assertEqual(result.status, preflight.PASS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py::PathCheckTests -q`
Expected: FAIL — `_check_schema`/`_check_indexes`/`_check_counts`/`_check_integrity` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `claw_v2/f2_primary_compat_preflight.py`:

```python
def _finalize(result: _PathResult, ok_reason: str) -> _PathResult:
    """FAIL keeps its reasons; PASS gets the success marker appended."""
    if result.status == PASS:
        result.reasons.append(ok_reason)
    return result


def _check_schema(conn: sqlite3.Connection, expected: ExpectedSchema) -> _PathResult:
    reasons: list[str] = []
    found: dict[str, list[str]] = {}
    for table, exp_cols in expected.tables.items():
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        actual = [row["name"] for row in cols]
        found[table] = actual
        if not actual:
            reasons.append(f"missing_table:{table}")
            continue
        missing = [c for c in exp_cols if c not in actual]
        if missing:
            reasons.append(f"missing_columns:{table}:{missing}")
    result = _PathResult(
        status=PASS if not reasons else FAIL, reasons=reasons, details={"tables": found}
    )
    return _finalize(result, "schema_subset_satisfied")


def _check_indexes(conn: sqlite3.Connection, expected: ExpectedSchema) -> _PathResult:
    reasons: list[str] = []
    _, actual_unique = _introspect(conn)
    for name, spec in expected.unique_indexes.items():
        found = actual_unique.get(name)
        if found is None:
            reasons.append(f"missing_unique_index:{name}")
        elif set(found.columns) != set(spec.columns):
            reasons.append(f"index_columns_mismatch:{name}:{sorted(found.columns)}")
    result = _PathResult(
        status=PASS if not reasons else FAIL,
        reasons=reasons,
        details={"required_unique_indexes": sorted(expected.unique_indexes)},
    )
    return _finalize(result, "required_unique_indexes_present")


def _check_counts(conn: sqlite3.Connection) -> _PathResult:
    reasons: list[str] = []
    counts: dict[str, int | None] = {}
    non_empty: list[str] = []
    for table in F2_DURABILITY_TABLES:
        try:
            row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = int(row["c"])
            if counts[table]:
                non_empty.append(table)
        except sqlite3.OperationalError:
            counts[table] = None
            reasons.append(f"count_failed_missing_table:{table}")
    result = _PathResult(
        status=PASS if not reasons else FAIL,
        reasons=reasons,
        details={"f2_table_counts": counts, "non_empty_f2_tables": non_empty},
    )
    return _finalize(result, "counts_collected")


def _check_integrity(conn: sqlite3.Connection) -> _PathResult:
    rows = conn.execute("PRAGMA quick_check").fetchall()
    results = [row[0] for row in rows]
    ok = results == ["ok"]
    result = _PathResult(
        status=PASS if ok else FAIL,
        reasons=[] if ok else [f"integrity_check_failed:{results[:3]}"],
        details={"quick_check": results[:5]},
    )
    return _finalize(result, "integrity_ok")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py
git commit -m "feat(f2): preflight path checks — schema/index/counts/integrity (read-only)"
```

---

## Task 3: Orchestration, report assembly, fail-closed paths

**Files:**
- Modify: `claw_v2/f2_primary_compat_preflight.py`
- Test: `tests/test_f2_primary_compat_preflight.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_f2_primary_compat_preflight.py`:

```python
_REQUIRED_JSON_FIELDS = (
    "overall_status",
    "db_path_checked",
    "opened_read_only",
    "immutable_mode_used",
    "primary_db_touched",
    "schema_version_expected",
    "schema_version_found",
    "schema_path",
    "index_path",
    "counts_path",
    "integrity_path",
    "integrity_required",
    "f2_table_counts",
    "non_empty_f2_tables",
    "reasons",
    "checks",
    "recommendation",
    "does_not_prove",
)


class RunReportTests(unittest.TestCase):
    def _primary_with_writer(self, tmpdir: str):
        path = Path(tmpdir) / "claw.db"
        wdb = RuntimeDb(path)
        F2DurabilityStore(wdb)
        self.addCleanup(wdb.close)
        return path, wdb

    def test_smoke_temp_db_passes(self) -> None:
        report = preflight.run_primary_compat_preflight()
        self.assertEqual(report["overall_status"], preflight.PASS)
        self.assertEqual(report["recommendation"], preflight.READY)
        self.assertFalse(report["primary_db_touched"])
        self.assertTrue(report["opened_read_only"])
        self.assertFalse(report["immutable_mode_used"])
        self.assertTrue(report["integrity_required"])

    def test_json_output_contains_required_fields(self) -> None:
        report = preflight.run_primary_compat_preflight()
        for key in _REQUIRED_JSON_FIELDS:
            self.assertIn(key, report)
        self.assertEqual(report["does_not_prove"], preflight._DOES_NOT_PROVE)

    def test_matching_primary_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = self._primary_with_writer(tmpdir)
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.PASS)
        self.assertEqual(report["recommendation"], preflight.READY)
        self.assertFalse(report["primary_db_touched"])

    def test_missing_table_needs_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, wdb = self._primary_with_writer(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP TABLE phase_recovery_cursors")
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.FAIL)
        self.assertEqual(report["recommendation"], preflight.NEEDS_REPAIR)

    def test_missing_unique_index_needs_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, wdb = self._primary_with_writer(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("DROP INDEX ux_phase_recovery_cursors_task_run")
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.FAIL)
        self.assertEqual(report["recommendation"], preflight.NEEDS_REPAIR)

    def test_subset_extra_objects_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, wdb = self._primary_with_writer(tmpdir)
            with wdb.transaction() as cur:
                cur.execute("ALTER TABLE phase_checkpoints ADD COLUMN extra_col TEXT")
                cur.execute("CREATE TABLE unrelated_extra (id TEXT PRIMARY KEY)")
            report = preflight.run_primary_compat_preflight(db_path=str(path))
        self.assertEqual(report["overall_status"], preflight.PASS)

    def test_open_failure_is_blocked(self) -> None:
        report = preflight.run_primary_compat_preflight(db_path="/nonexistent/dir/claw.db")
        self.assertEqual(report["overall_status"], preflight.FAIL)
        self.assertEqual(report["recommendation"], preflight.BLOCKED)
        self.assertFalse(report["opened_read_only"])
        self.assertFalse(report["primary_db_touched"])

    def test_read_only_enforcement_write_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = self._primary_with_writer(tmpdir)
            conn = preflight._open_readonly(path)
            self.addCleanup(conn.close)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE should_fail (x TEXT)")
            query_only = conn.execute("PRAGMA query_only").fetchone()[0]
        self.assertEqual(int(query_only), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py::RunReportTests -q`
Expected: FAIL — `run_primary_compat_preflight` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `claw_v2/f2_primary_compat_preflight.py`:

```python
def _read_found_schema_version(conn: sqlite3.Connection) -> int | None:
    """Best-effort: the max schema_version across populated F2 tables, or None
    when all are empty/absent."""
    best: int | None = None
    for table in F2_DURABILITY_TABLES:
        try:
            row = conn.execute(
                f"SELECT MAX(schema_version) AS v FROM {table}"
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        value = row["v"]
        if value is not None:
            best = value if best is None else max(best, int(value))
    return best


def _blocked_report(
    *, db_path_checked: str, opened_read_only: bool, reasons: list[str]
) -> dict[str, Any]:
    """Fail-closed report for paths where no checks ran (open failure / exception)."""
    return {
        "overall_status": FAIL,
        "db_path_checked": db_path_checked,
        "opened_read_only": opened_read_only,
        "immutable_mode_used": False,
        "primary_db_touched": False,
        "schema_version_expected": F2_DURABILITY_SCHEMA_VERSION,
        "schema_version_found": None,
        "schema_path": FAIL,
        "index_path": FAIL,
        "counts_path": FAIL,
        "integrity_path": FAIL,
        "integrity_required": True,
        "f2_table_counts": {table: None for table in F2_DURABILITY_TABLES},
        "non_empty_f2_tables": [],
        "reasons": reasons,
        "checks": {},
        "recommendation": BLOCKED,
        "does_not_prove": _DOES_NOT_PROVE,
    }


def _run_checks(
    conn: sqlite3.Connection, expected: ExpectedSchema, *, db_path_checked: str
) -> dict[str, Any]:
    schema = _check_schema(conn, expected)
    index = _check_indexes(conn, expected)
    counts = _check_counts(conn)
    integrity = _check_integrity(conn)
    found_version = _read_found_schema_version(conn)

    reasons: list[str] = []
    reasons.extend(f"schema:{r}" for r in schema.reasons)
    reasons.extend(f"index:{r}" for r in index.reasons)
    reasons.extend(f"counts:{r}" for r in counts.reasons)
    reasons.extend(f"integrity:{r}" for r in integrity.reasons)

    version_ok = found_version is None or found_version == expected.schema_version
    if not version_ok:
        reasons.append(f"schema_version_mismatch:found={found_version}")

    paths_pass = all(
        p.status == PASS for p in (schema, index, counts, integrity)
    ) and version_ok
    overall = PASS if paths_pass else FAIL
    if overall == PASS:
        recommendation = READY
        reasons.append("primary_f2_compatible")
    elif schema.status == FAIL or index.status == FAIL or not version_ok:
        recommendation = NEEDS_REPAIR
    else:
        recommendation = BLOCKED

    return {
        "overall_status": overall,
        "db_path_checked": db_path_checked,
        "opened_read_only": True,
        "immutable_mode_used": False,
        "primary_db_touched": False,
        "schema_version_expected": expected.schema_version,
        "schema_version_found": found_version,
        "schema_path": schema.status,
        "index_path": index.status,
        "counts_path": counts.status,
        "integrity_path": integrity.status,
        "integrity_required": True,
        "f2_table_counts": counts.details.get("f2_table_counts", {}),
        "non_empty_f2_tables": counts.details.get("non_empty_f2_tables", []),
        "reasons": reasons,
        "checks": {
            "schema": schema.details,
            "index": index.details,
            "counts": counts.details,
            "integrity": integrity.details,
        },
        "recommendation": recommendation,
        "does_not_prove": _DOES_NOT_PROVE,
    }


def run_primary_compat_preflight(*, db_path: str | None = None) -> dict[str, Any]:
    """Run the read-only F2 primary compatibility preflight and return a
    structured, fail-closed report. Never writes to ``db_path``."""
    try:
        expected = expected_f2_schema()
    except Exception as exc:  # building the expected schema itself failed
        return _blocked_report(
            db_path_checked=db_path or "temp",
            opened_read_only=False,
            reasons=[f"expected_schema_build_failed:{exc.__class__.__name__}", str(exc)],
        )

    if db_path is None:
        # Smoke: build a temp 'primary', keep its writer open so the read-only
        # open sees the WAL, and check it against the expected schema.
        try:
            with tempfile.TemporaryDirectory(prefix="f2-compat-smoke-") as tmpdir:
                temp_path = Path(tmpdir) / "claw.db"
                writer = RuntimeDb(temp_path)
                try:
                    F2DurabilityStore(writer)
                    conn = _open_readonly(temp_path)
                    try:
                        return _run_checks(conn, expected, db_path_checked="temp")
                    finally:
                        conn.close()
                finally:
                    writer.close()
        except Exception as exc:
            return _blocked_report(
                db_path_checked="temp",
                opened_read_only=False,
                reasons=[f"unexpected_exception:{exc.__class__.__name__}", str(exc)],
            )

    target = Path(db_path)
    try:
        conn = _open_readonly(target)
    except Exception as exc:
        return _blocked_report(
            db_path_checked=str(target),
            opened_read_only=False,
            reasons=[f"read_only_open_failed:{exc.__class__.__name__}", str(exc)],
        )
    try:
        return _run_checks(conn, expected, db_path_checked=str(target))
    except Exception as exc:
        return _blocked_report(
            db_path_checked=str(target),
            opened_read_only=True,
            reasons=[f"unexpected_exception:{exc.__class__.__name__}", str(exc)],
        )
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`
Expected: PASS (all tests so far, ~15).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py
git commit -m "feat(f2): preflight orchestration + fail-closed report (read-only)"
```

---

## Task 4: CLI

**Files:**
- Modify: `claw_v2/f2_primary_compat_preflight.py`
- Test: `tests/test_f2_primary_compat_preflight.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_f2_primary_compat_preflight.py`:

```python
import io
import contextlib


class CliTests(unittest.TestCase):
    def test_cli_temp_db_json_smoke_exits_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = preflight.main(["--temp-db", "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["overall_status"], preflight.PASS)
        self.assertFalse(payload["primary_db_touched"])

    def test_cli_db_path_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claw.db"
            wdb = RuntimeDb(path)
            F2DurabilityStore(wdb)
            self.addCleanup(wdb.close)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = preflight.main(["--db-path", str(path), "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["recommendation"], preflight.READY)
        self.assertTrue(payload["opened_read_only"])
```

Add `import io` / `import json` / `import contextlib` at the top of the test file if not already present (json is used; add the rest).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py::CliTests -q`
Expected: FAIL — `preflight.main` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `claw_v2/f2_primary_compat_preflight.py`:

```python
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only F2 primary DB compatibility preflight. Opens a supplied "
            "--db-path strictly read-only (mode=ro, never immutable); proves "
            "schema/index/integrity compatibility, not the live F2 write path."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--temp-db",
        action="store_true",
        help="Smoke: build an isolated temp DB and check it (default).",
    )
    group.add_argument(
        "--db-path",
        default=None,
        help="Path to a DB to check. Opened read-only only; never written.",
    )
    parser.add_argument("--json", action="store_true", help="Emit structured JSON only.")
    return parser


def _format_human(report: dict[str, Any]) -> str:
    lines = [
        f"overall_status: {report['overall_status']}",
        f"recommendation: {report['recommendation']}",
        f"db_path_checked: {report['db_path_checked']}",
        f"opened_read_only: {str(report['opened_read_only']).lower()}",
        f"immutable_mode_used: {str(report['immutable_mode_used']).lower()}",
        f"primary_db_touched: {str(report['primary_db_touched']).lower()}",
        f"schema_version_expected: {report['schema_version_expected']}",
        f"schema_version_found: {report['schema_version_found']}",
        f"schema_path: {report['schema_path']}",
        f"index_path: {report['index_path']}",
        f"counts_path: {report['counts_path']}",
        f"integrity_path: {report['integrity_path']}",
        f"f2_table_counts: {report['f2_table_counts']}",
        f"non_empty_f2_tables: {report['non_empty_f2_tables']}",
        "reasons:",
    ]
    lines.extend(f"  - {reason}" for reason in report["reasons"])
    lines.append(f"does_not_prove: {report['does_not_prove']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = run_primary_compat_preflight(db_path=args.db_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_human(report))
    return 0 if report["overall_status"] == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests + ruff**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`
Expected: PASS (all tests, ~17).
Run: `uvx ruff check claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py && uvx ruff format --check claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py`
Expected: clean (run `uvx ruff format` on the two files if format check fails, then re-run).

- [ ] **Step 5: Commit**

```bash
git add claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py
git commit -m "feat(f2): preflight CLI (--temp-db / --db-path read-only / --json)"
```

---

## Task 5: INTERNAL_WIRING invariant + meta bump

**Files:**
- Modify: `claw_v2/INTERNAL_WIRING.md`

- [ ] **Step 1: Add the invariant block**

In the invariants section, immediately AFTER the `stage2c2_synthetic_canary_uses_isolated_f2_state_only` block (ends at its `enforced_by` list, around line 442), insert:

```yaml
  primary_f2_compatibility_preflight_is_read_only:
    rule: The F2 primary compatibility preflight only ever READS a supplied DB.
          A supplied `--db-path` is opened `mode=ro` (URI `?mode=ro`) plus
          `PRAGMA query_only=ON`; it MUST NOT be opened `immutable=1` (the live
          daemon is the single RuntimeDb WAL writer, and `immutable` ignores the
          `-wal`, yielding a stale snapshot). It never constructs a writing
          `RuntimeDb`/`F2DurabilityStore` against the supplied path — those are
          built only on its own temp DBs (for the expected-schema derivation and
          the `--temp-db` smoke). `primary_db_touched` is always false.
    entrypoint: `python -m claw_v2.f2_primary_compat_preflight --db-path data/claw.db --json`
    replaces: The proposed primary seed/verify/purge synthetic canary
          (`primary_f2_write_path_incompatibility_canary`), rejected by the
          operator 2026-06-25 (mutating the primary buys little vs its cost).
    retires_failure_mode: `primary_f2_write_path_incompatibility` — the first
          real F2 write to the primary failing/corrupting/behaving differently
          due to schema drift, missing real indexes/constraints, or physical
          state. Answered read-only: do the F2 tables/columns/unique-indexes the
          code expects exist (subset semantics) and does `quick_check` pass?
    does_not_prove: NOT the live F2 write path, crash recovery, WAL concurrency,
          a real executor, the durable NotebookLM lane, external-effect dedup,
          or Stage 3. A `PRIMARY_COMPAT_PREFLIGHT_READY` result means only that
          the primary schema is compatible — it is NOT a signal that enabling F2
          live (Gate B / Stage 2C2) is safe. Each gate stays separate.
    output_contract: Structured `--json` includes `overall_status`,
          `recommendation` (PRIMARY_COMPAT_PREFLIGHT_READY / NEEDS_REPAIR /
          BLOCKED), `db_path_checked`, `opened_read_only`, `immutable_mode_used`
          (false), `primary_db_touched` (false), `schema_version_expected`,
          `schema_version_found`, `schema_path`, `index_path`, `counts_path`,
          `integrity_path`, `integrity_required` (true), `f2_table_counts`,
          `non_empty_f2_tables`, `reasons`, `checks`, and `does_not_prove`. Fails
          closed (`BLOCKED`) on read-only open failure or any exception.
    enforced_by:
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_read_only_enforcement_write_raises
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_open_failure_is_blocked
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_matching_primary_passes
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_missing_table_needs_repair
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_missing_unique_index_needs_repair
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_subset_extra_objects_still_passes
      - tests/test_f2_primary_compat_preflight.py::RunReportTests::test_json_output_contains_required_fields
      - tests/test_f2_primary_compat_preflight.py::CliTests::test_cli_db_path_is_read_only
```

- [ ] **Step 2: Bump the meta block**

In the `## meta` YAML block (lines ~10-17), change:
- `describes_commit: "Stage 2C2 isolated synthetic F2 canary harness"` → `describes_commit: "F2 primary DB compatibility preflight (read-only)"`
- `doc_version: 2.36` → `doc_version: 2.37`
- `last_verified: 2026-06-25` → keep `2026-06-25`
- Append to `verification_method` (before the closing quote): ` + f2_primary_compat_preflight --temp-db --json smoke (overall PASS, opened_read_only/immutable_mode_used/primary_db_touched verified) and targeted pytest`

- [ ] **Step 3: Verify the doc edits**

Run: `grep -n "primary_f2_compatibility_preflight_is_read_only\|doc_version: 2.37\|primary DB compatibility preflight" claw_v2/INTERNAL_WIRING.md`
Expected: the invariant name, `doc_version: 2.37`, and the new describes_commit all appear.

- [ ] **Step 4: Commit**

```bash
git add claw_v2/INTERNAL_WIRING.md
git commit -m "docs(f2): INTERNAL_WIRING invariant — primary compat preflight is read-only (doc 2.37)"
```

---

## Task 6: Final validation gate

**Files:** none (validation only)

- [ ] **Step 1: Full targeted test run**

Run: `.venv/bin/python -m pytest tests/test_f2_primary_compat_preflight.py -q`
Expected: all PASS.

- [ ] **Step 2: F2 regression sweep (ensure nothing else broke)**

Run: `.venv/bin/python -m pytest tests/test_stage2c2_synthetic_canary.py tests/test_f2_durability_store.py tests/test_f2_recovery.py tests/test_architecture_invariants.py -q`
Expected: all PASS (no F2 or invariant regressions).

- [ ] **Step 3: Lint/format**

Run: `uvx ruff check claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py && uvx ruff format --check claw_v2/f2_primary_compat_preflight.py tests/test_f2_primary_compat_preflight.py`
Expected: clean.

- [ ] **Step 4: Real read-only smoke against the live primary (read-only — safe)**

Run (from the worktree, pointing at the repo-root primary DB): `.venv/bin/python -m claw_v2.f2_primary_compat_preflight --db-path ../../data/claw.db --json`
Expected: valid JSON; `opened_read_only: true`, `immutable_mode_used: false`, `primary_db_touched: false`. Record `overall_status` / `recommendation` (likely `PRIMARY_COMPAT_PREFLIGHT_READY` given F2 tables exist and are empty per the handoff). This run only reads; it does not restart the daemon, use launchctl, or enable F2.

- [ ] **Step 5: Diff hygiene**

Run: `git diff --check && git log --oneline main..HEAD`
Expected: no whitespace errors; the 5 feature commits (spec already committed earlier) listed.

---

## Self-Review (run after writing, before execution)

- **Spec coverage:** §2 goals → Tasks 1-4; §4.1 mode=ro → Task 1 `_open_readonly` + Task 5 invariant; §4.2 new module → Task 1; §4.3 subset diff → Task 2 `_check_schema`/`_check_indexes`; §4.4 integrity/backup → Task 2 `_check_integrity` + spec runbook; §6 output contract → Task 3 `_run_checks` + Task 4 fields test; §7 fail-closed → Task 3 `_blocked_report`; §8 tests → Tasks 1-4; §10 invariant → Task 5; §11 gates → Task 5 `does_not_prove`/`replaces`.
- **Type consistency:** `_PathResult`/`_IndexSpec`/`ExpectedSchema`, `expected_f2_schema()`, `_open_readonly()`, `_introspect()`, `_check_schema/_check_indexes/_check_counts/_check_integrity()`, `_run_checks()`, `_blocked_report()`, `run_primary_compat_preflight()`, `main()` — names used consistently across tasks. Constants `PASS/FAIL/READY/NEEDS_REPAIR/BLOCKED/_DOES_NOT_PROVE` defined in Task 1.
- **Note vs spec:** spec §10 said "AST-enforced"; the actual repo pattern for these preflight/canary invariants is documented-in-INTERNAL_WIRING + `enforced_by` unit tests (Task 5 follows that). No AST scan is added.
```
