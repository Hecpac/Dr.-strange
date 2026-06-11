from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_CONNECT_TIMEOUT_SECONDS = 15.0
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_AUTOCHECKPOINT_PAGES = 1_000


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
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    journal_mode = str(row[0] if row else "").lower()
    if journal_mode != "wal":
        raise RuntimeDatabaseError(f"SQLite WAL mode did not activate: {journal_mode or 'unknown'}")
    # FULL is slower than NORMAL, but the daemon's memory/ledger DB is a
    # correctness surface. Prefer durability over throughput.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA fullfsync=ON")
    conn.execute("PRAGMA checkpoint_fullfsync=ON")
    conn.execute(f"PRAGMA wal_autocheckpoint={SQLITE_WAL_AUTOCHECKPOINT_PAGES}")
    conn.execute("PRAGMA foreign_keys=ON")


def check_runtime_sqlite_health(db_path: Path | str, *, thorough: bool = False) -> None:
    """Fail fast if an existing runtime DB is structurally unhealthy.

    Missing or empty DB files are allowed so first boot can create them. Existing
    files must be real SQLite and pass `quick_check`; `thorough=True` also runs
    `integrity_check`, intended for daemon startup/restart boundaries.
    """
    path = Path(db_path)
    _verify_sqlite_file_header(path)
    if not path.exists() or path.stat().st_size == 0:
        return
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=SQLITE_CONNECT_TIMEOUT_SECONDS)
        try:
            _run_sqlite_health_pragma(conn, path, "quick_check")
            if thorough:
                _run_sqlite_health_pragma(conn, path, "integrity_check")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise RuntimeDatabaseError(
            f"Runtime database failed SQLite health check: {path}. "
            "Do not restart the daemon until the DB is recovered from a verified backup."
        ) from exc


def _run_sqlite_health_pragma(conn: sqlite3.Connection, path: Path, pragma: str) -> None:
    rows = conn.execute(f"PRAGMA {pragma}").fetchall()
    values = [str(row[0]) for row in rows]
    if values == ["ok"]:
        return
    detail = "; ".join(values[:20])
    if len(values) > 20:
        detail += f"; ... {len(values) - 20} more"
    raise RuntimeDatabaseError(f"Runtime database failed PRAGMA {pragma}: {path}: {detail}")


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
