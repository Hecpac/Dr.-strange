from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import weakref
from pathlib import Path

logger = logging.getLogger(__name__)

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
        # A fresh connection always joins the ON-DISK WAL generation: stamp it
        # so later writers can detect an external generation swap by inode.
        note_wal_generation(path)
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


# --- WAL generation guard (T10, incidente 2026-06-12) -----------------------
#
# Something unlinked data/claw.db-wal/-shm under the daemon's live
# connections. The orphaned connections then fail every write with
# "database is locked" forever (messages, events and task closes silently
# stop persisting), while NEW connections silently form a second WAL
# generation on fresh sidecar files. The guard makes that state recoverable:
# every store holding a long-lived connection registers a heal callback; when
# a writer exhausts its locked retries AND the sidecars are gone, ALL
# registered connections for that path reopen together so the process rejoins
# a single WAL generation instead of splitting into two writers.

_WAL_HEAL_REGISTRY: dict[str, list["StoreWalHealHandle"]] = {}
_WAL_HEAL_REGISTRY_LOCK = threading.Lock()
_NULL_STORE_LOCK = contextlib.nullcontext()


def _registry_key(db_path: Path | str) -> str:
    return str(Path(db_path).expanduser().resolve(strict=False))


def register_wal_heal(db_path: Path | str, handle: "StoreWalHealHandle") -> None:
    """Register a close/reopen handle for one store's connection to ``db_path``."""
    with _WAL_HEAL_REGISTRY_LOCK:
        _WAL_HEAL_REGISTRY.setdefault(_registry_key(db_path), []).append(handle)


class StoreWalHealHandle:
    """Close/reopen handle for the conventional store shape (.db_path/._conn/._lock).

    Two-phase on purpose: every registered connection must CLOSE before any
    reopens, so the process abandons the orphaned WAL generation completely
    instead of splitting into two concurrent generations.
    """

    def __init__(self, store: object, *, row_factory: bool = True) -> None:
        # weakref (PR #97 review): the global registry must not leak every
        # store instance created during the process/test-suite lifetime.
        self._store_ref = weakref.ref(store)
        self._row_factory = row_factory

    @property
    def alive(self) -> bool:
        return self._store_ref() is not None

    @staticmethod
    def _lock_ctx(store: object):
        return getattr(store, "_lock", None) or _NULL_STORE_LOCK

    def close(self) -> None:
        store = self._store_ref()
        if store is None:
            return
        with self._lock_ctx(store):
            old = getattr(store, "_conn", None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    logger.debug("closing orphaned connection failed", exc_info=True)

    def reopen(self) -> None:
        store = self._store_ref()
        if store is None:
            return
        with self._lock_ctx(store):
            store._conn = connect_runtime_sqlite(
                store.db_path, row_factory=self._row_factory
            )


def make_store_wal_heal(store: object, *, row_factory: bool = True) -> StoreWalHealHandle:
    return StoreWalHealHandle(store, row_factory=row_factory)


_WAL_GENERATION_INODES: dict[str, int] = {}


def note_wal_generation(db_path: Path | str) -> None:
    """Record the inode of the ``-wal`` this process is successfully writing.

    Called by writers after a successful persist (one stat). Lets the orphan
    check detect a GENERATION SWAP: an external process can delete our
    sidecars AND leave fresh ones of its own on disk — the wal "exists" but
    it is not the one our connections write to (live drill, 2026-06-12).
    """
    key = _registry_key(db_path)
    try:
        _WAL_GENERATION_INODES[key] = os.stat(
            f"{Path(db_path).expanduser().resolve(strict=False)}-wal"
        ).st_ino
    except OSError:
        # No wal yet (fresh DB before its first write): leave the stamp as-is;
        # the first successful persist will stamp it.
        pass


def wal_generation_stamp_missing(db_path: Path | str) -> bool:
    return _registry_key(db_path) not in _WAL_GENERATION_INODES


def wal_sidecars_orphaned(db_path: Path | str) -> bool:
    """True when this process's WAL generation is broken.

    Either the ``-wal`` sidecar is gone from disk, or it exists with a
    DIFFERENT inode than the one our last successful write used (an external
    process replaced the generation under us). Only meaningful from a
    locked-error context, never as a standalone health probe.
    """
    path = Path(db_path).expanduser().resolve(strict=False)
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
    except OSError:
        return False
    wal = Path(f"{path}-wal")
    try:
        wal_inode = wal.stat().st_ino
    except OSError:
        return True
    expected = _WAL_GENERATION_INODES.get(_registry_key(db_path))
    return expected is not None and wal_inode != expected


def heal_orphaned_wal(db_path: Path | str) -> bool:
    """Reopen every registered connection for ``db_path`` if sidecars are gone.

    Returns True when a heal ran. Safe to call from any writer's
    locked-retry-exhausted path; idempotent (each heal callback reopens under
    its own store lock).
    """
    if not wal_sidecars_orphaned(db_path):
        return False
    key = _registry_key(db_path)
    with _WAL_HEAL_REGISTRY_LOCK:
        handles = [h for h in _WAL_HEAL_REGISTRY.get(key, ()) if h.alive]
        _WAL_HEAL_REGISTRY[key] = handles
    logger.critical(
        "SQLite WAL sidecars for %s are missing while connections are open; "
        "reopening %d registered connection(s) to rejoin one WAL generation.",
        db_path,
        len(handles),
    )
    for handle in handles:
        try:
            handle.close()
        except Exception:
            logger.exception("WAL heal close failed for %s", db_path)
    # A reopen attempt that raced the unlink can leave an EMPTY recreated
    # -wal without its -shm; that husk makes every subsequent open fail with
    # "disk I/O error". Empty means zero frames, so removing it loses nothing.
    wal = Path(f"{Path(db_path)}-wal")
    shm = Path(f"{Path(db_path)}-shm")
    try:
        if wal.exists() and not shm.exists() and wal.stat().st_size == 0:
            wal.unlink()
    except OSError:
        logger.debug("WAL husk cleanup failed for %s", db_path, exc_info=True)
    for handle in handles:
        try:
            handle.reopen()
        except Exception:
            logger.exception("WAL heal reopen failed for %s", db_path)
    # The new generation has no wal until the first write; clear the stamp so
    # the next successful persist records the fresh inode.
    _WAL_GENERATION_INODES.pop(key, None)
    return True


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
