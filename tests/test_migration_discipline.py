from __future__ import annotations

import sqlite3
from pathlib import Path

from claw_v2.memory import CURRENT_SCHEMA_VERSION, MemoryStore, V3_SCHEMA_BASELINE_VERSION
from claw_v2.observe import ObserveStream


def _pragma_value(conn: sqlite3.Connection, pragma: str):
    return conn.execute(f"PRAGMA {pragma}").fetchone()[0]


def test_memory_store_sets_pre_v3_user_version_baseline(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "claw.db")

    assert _pragma_value(store._conn, "user_version") == CURRENT_SCHEMA_VERSION


def test_memory_store_does_not_downgrade_future_user_version(tmp_path: Path) -> None:
    db_path = tmp_path / "claw.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version={V3_SCHEMA_BASELINE_VERSION + 5}")
    conn.close()

    store = MemoryStore(db_path)

    assert _pragma_value(store._conn, "user_version") == V3_SCHEMA_BASELINE_VERSION + 5


def test_memory_store_applies_idempotency_migration(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "claw.db")

    table = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='idempotency_keys'"
    ).fetchone()
    columns = {
        row[1]
        for row in store._conn.execute("PRAGMA table_info(idempotency_keys)").fetchall()
    }

    assert table is not None
    assert {"key", "status", "created_at", "completed_at", "result"} <= columns
    assert _pragma_value(store._conn, "user_version") == CURRENT_SCHEMA_VERSION


def test_memory_and_observe_use_wal_and_busy_timeout(tmp_path: Path) -> None:
    memory = MemoryStore(tmp_path / "claw.db")
    observe = ObserveStream(tmp_path / "claw.db")

    assert str(_pragma_value(memory._conn, "journal_mode")).lower() == "wal"
    assert str(_pragma_value(observe._conn, "journal_mode")).lower() == "wal"
    assert int(_pragma_value(memory._conn, "busy_timeout")) >= 5000
    assert int(_pragma_value(observe._conn, "busy_timeout")) >= 5000


def test_schema_migration_registry_documents_planned_v3_tables() -> None:
    doc = Path("docs/schema-migrations.md").read_text(encoding="utf-8")

    assert f"PRAGMA user_version = {V3_SCHEMA_BASELINE_VERSION}" in doc
    for table in ("idempotency_keys", "artifacts", "jobs", "job_steps"):
        assert table in doc
        assert f"DROP TABLE IF EXISTS {table}" in doc


def test_documented_new_table_rollback_pattern_is_executable(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "claw.db")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT,
            result TEXT
        );
        PRAGMA user_version=101;
        """
    )
    assert _pragma_value(conn, "user_version") == 101

    conn.executescript(
        f"""
        DROP TABLE IF EXISTS idempotency_keys;
        PRAGMA user_version={V3_SCHEMA_BASELINE_VERSION};
        """
    )

    assert _pragma_value(conn, "user_version") == V3_SCHEMA_BASELINE_VERSION
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='idempotency_keys'"
    ).fetchone() is None
