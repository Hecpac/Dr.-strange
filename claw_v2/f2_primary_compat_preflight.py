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
