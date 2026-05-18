from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_CONNECT_TIMEOUT_SECONDS = 15.0


def connect_runtime_sqlite(
    db_path: Path | str,
    *,
    row_factory: bool = True,
    timeout: float = SQLITE_CONNECT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open a SQLite connection configured for daemon/test concurrency."""
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=timeout)
    if row_factory:
        conn.row_factory = sqlite3.Row
    configure_runtime_sqlite(conn)
    return conn


def configure_runtime_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
