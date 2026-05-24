from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_CONNECT_TIMEOUT_SECONDS = 15.0
SQLITE_HEADER = b"SQLite format 3\x00"


class RuntimeDatabaseError(sqlite3.DatabaseError):
    """Raised when the runtime database file is not usable as SQLite."""


def connect_runtime_sqlite(
    db_path: Path | str,
    *,
    row_factory: bool = True,
    timeout: float = SQLITE_CONNECT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open a SQLite connection configured for daemon/test concurrency."""
    path = Path(db_path)
    _verify_sqlite_file_header(path)
    try:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=timeout)
        if row_factory:
            conn.row_factory = sqlite3.Row
        configure_runtime_sqlite(conn)
    except sqlite3.DatabaseError as exc:
        raise RuntimeDatabaseError(
            f"Runtime database is not a readable SQLite database: {path}. "
            "Do not overwrite it; restore from a verified checkpoint or backup."
        ) from exc
    return conn


def configure_runtime_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")


def _verify_sqlite_file_header(db_path: Path) -> None:
    if not db_path.exists() or db_path.stat().st_size == 0:
        return
    with db_path.open("rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise RuntimeDatabaseError(
            f"Runtime database is not a SQLite database: {db_path}. "
            "The file was left untouched; restore from backup before restarting the daemon."
        )
