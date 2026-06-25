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
            # Only the unique index's columns are captured; a partial-index
            # predicate (e.g. `WHERE write_key IS NOT NULL` on
            # ux_phase_checkpoint_writes_key) is intentionally NOT verified —
            # acceptable because the expected schema is derived from the same DDL
            # that builds the primary.
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


def _read_found_schema_version(conn: sqlite3.Connection) -> int | None:
    """Best-effort: the max schema_version across populated F2 tables, or None
    when all are empty/absent."""
    best: int | None = None
    for table in F2_DURABILITY_TABLES:
        try:
            row = conn.execute(f"SELECT MAX(schema_version) AS v FROM {table}").fetchone()
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

    paths_pass = all(p.status == PASS for p in (schema, index, counts, integrity)) and version_ok
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
