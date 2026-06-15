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
    degraded = runtime_db_degraded_error(path)
    if degraded is not None:
        raise degraded
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
        detail = sqlite_error_details(exc)
        logger.exception(
            "SQLite runtime connect/configure failed for %s%s%s",
            path,
            ": " if detail else "",
            detail,
        )
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
_WAL_HEAL_LOCKS: dict[str, threading.Lock] = {}
_NULL_STORE_LOCK = contextlib.nullcontext()
_RUNTIME_DB_DEGRADED: dict[str, RuntimeDatabaseError] = {}
# M5 (2026-06-14): monotonic count of COMPLETED heals per db_path. Lets a
# forced heal that lost the race for the per-db heal lock notice that another
# writer already reopened every registered connection while it waited, and
# coalesce onto that fresh generation instead of forcing a redundant
# registry-wide reopen (which would re-close every other writer's just-healed
# connection and re-trigger the cascade). GIL-atomic dict ops, like the
# adjacent generation/degraded maps.
_HEAL_GENERATION: dict[str, int] = {}

# Upper bound on consecutive heals a single writer operation will absorb before
# giving up. A burst of concurrent heals can re-close a connection mid-retry;
# tolerating a bounded run (instead of exactly one) lets the operation ride the
# cascade to convergence, while still failing visibly on a genuinely wedged DB.
WAL_HEAL_RETRY_LIMIT = 8


def _registry_key(db_path: Path | str) -> str:
    return str(Path(db_path).expanduser().resolve(strict=False))


def register_wal_heal(db_path: Path | str, handle: "StoreWalHealHandle") -> None:
    """Register a close/reopen handle for one store's connection to ``db_path``."""
    key = _registry_key(db_path)
    with _WAL_HEAL_REGISTRY_LOCK:
        bucket = _WAL_HEAL_REGISTRY.setdefault(key, [])
        # Prune dead (gc'd) handles on every registration so repeated
        # create/destroy cycles (large test suites) never accumulate.
        bucket[:] = [h for h in bucket if h.alive]
        bucket.append(handle)


def sqlite_error_details(exc: BaseException) -> str:
    """Return sqlite extended error metadata when Python exposes it."""
    details: list[str] = []
    code = getattr(exc, "sqlite_errorcode", None)
    name = getattr(exc, "sqlite_errorname", None)
    if code is not None:
        details.append(f"sqlite_errorcode={code}")
    if name:
        details.append(f"sqlite_errorname={name}")
    return ", ".join(details)


def is_sqlite_disk_io_error(exc: BaseException) -> bool:
    """True for SQLite I/O errors, including extended SQLITE_IOERR variants."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    name = str(getattr(exc, "sqlite_errorname", "") or "").upper()
    if name.startswith("SQLITE_IOERR"):
        return True
    return "disk i/o error" in str(exc).lower()


def is_sqlite_closed_connection_error(exc: BaseException) -> bool:
    """True when a stale handle is used after a registry-wide reopen."""
    return isinstance(exc, sqlite3.ProgrammingError) and "closed database" in str(exc).lower()


def heal_wal_after_closed_connection(
    db_path: Path | str,
    exc: BaseException,
    *,
    context: str,
) -> bool:
    """Heal once when a writer observes a connection closed by WAL recovery."""
    if not is_sqlite_closed_connection_error(exc):
        return False
    logger.warning(
        "SQLite closed connection in %s for %s; forcing conservative WAL heal",
        context,
        db_path,
        exc_info=True,
    )
    return _force_conservative_wal_heal(
        db_path,
        reason="sqlite_closed_connection",
        context=context,
        cause=exc,
    )


def heal_wal_after_disk_io(db_path: Path | str, exc: BaseException, *, context: str) -> bool:
    """Heal once for a disk I/O error that may be caused by stale WAL sidecars."""
    if not is_sqlite_disk_io_error(exc):
        return False
    detail = sqlite_error_details(exc)
    logger.warning(
        "SQLite disk I/O error in %s for %s; forcing conservative WAL heal%s%s",
        context,
        db_path,
        ": " if detail else "",
        detail,
        exc_info=True,
    )
    return _force_conservative_wal_heal(
        db_path,
        reason="sqlite_disk_io",
        context=context,
        cause=exc,
    )


def _heal_lock_for_key(key: str) -> threading.Lock:
    with _WAL_HEAL_REGISTRY_LOCK:
        return _WAL_HEAL_LOCKS.setdefault(key, threading.Lock())


def _mark_runtime_db_degraded(db_path: Path | str, error: RuntimeDatabaseError) -> None:
    _RUNTIME_DB_DEGRADED[_registry_key(db_path)] = error


def runtime_db_degraded_error(db_path: Path | str) -> RuntimeDatabaseError | None:
    return _RUNTIME_DB_DEGRADED.get(_registry_key(db_path))


class _DegradedConnection:
    """Connection sentinel that fails explicitly after a failed WAL heal."""

    def __init__(self, error: RuntimeDatabaseError) -> None:
        self._error = error

    def execute(self, *args, **kwargs):
        raise self._error

    def executescript(self, *args, **kwargs):
        raise self._error

    def commit(self) -> None:
        raise self._error

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


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
            new_conn = connect_runtime_sqlite(store.db_path, row_factory=self._row_factory)
            store._conn = new_conn

    def mark_degraded(self, error: RuntimeDatabaseError) -> None:
        store = self._store_ref()
        if store is None:
            return
        with self._lock_ctx(store):
            current = getattr(store, "_conn", None)
            if current is not None:
                try:
                    current.close()
                except Exception:
                    logger.debug("closing degraded connection failed", exc_info=True)
            store._conn = _DegradedConnection(error)

    def describe(self) -> str:
        store = self._store_ref()
        if store is None:
            return "<dead store>"
        return f"{store.__class__.__module__}.{store.__class__.__name__}@{id(store):x}"


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
    key = _registry_key(db_path)
    heal_lock = _heal_lock_for_key(key)
    with heal_lock:
        if not wal_sidecars_orphaned(db_path):
            return False
        return _run_conservative_wal_heal(
            key,
            db_path,
            reason="orphaned_wal",
            context="heal_orphaned_wal",
            cause=None,
        )


def _force_conservative_wal_heal(
    db_path: Path | str,
    *,
    reason: str,
    context: str,
    cause: BaseException | None,
) -> bool:
    key = _registry_key(db_path)
    heal_lock = _heal_lock_for_key(key)
    # Read the completed-heal counter BEFORE competing for the heal lock. If it
    # advances while we block, another writer already reopened every registered
    # connection (ours included) into the fresh generation — so we coalesce onto
    # it and signal "retry" rather than forcing a redundant reopen that would
    # re-close the connections the other heal just restored (M5).
    generation_before = _HEAL_GENERATION.get(key, 0)
    with heal_lock:
        if _HEAL_GENERATION.get(key, 0) != generation_before:
            return True
        return _run_conservative_wal_heal(
            key,
            db_path,
            reason=reason,
            context=context,
            cause=cause,
        )


def _run_conservative_wal_heal(
    key: str,
    db_path: Path | str,
    *,
    reason: str,
    context: str,
    cause: BaseException | None,
) -> bool:
    with _WAL_HEAL_REGISTRY_LOCK:
        handles = [h for h in _WAL_HEAL_REGISTRY.get(key, ()) if h.alive]
        _WAL_HEAL_REGISTRY[key] = handles
    detail = sqlite_error_details(cause) if cause is not None else ""
    if reason == "orphaned_wal":
        logger.critical(
            "SQLite WAL sidecars for %s are missing while connections are open; "
            "reopening %d registered connection(s) to rejoin one WAL generation.",
            db_path,
            len(handles),
        )
    else:
        logger.critical(
            "SQLite WAL conservative heal for %s reason=%s context=%s%s%s; "
            "reopening %d registered connection(s) to rejoin one WAL generation.",
            db_path,
            reason,
            context,
            ": " if detail else "",
            detail,
            len(handles),
        )
    for handle in handles:
        try:
            handle.close()
        except Exception:
            logger.exception("WAL heal close failed for %s handle=%s", db_path, handle.describe())
    _cleanup_recoverable_sidecars(key, db_path)
    failures: list[tuple[StoreWalHealHandle, BaseException]] = []
    for handle in handles:
        try:
            handle.reopen()
        except Exception as exc:
            detail = sqlite_error_details(exc)
            logger.exception(
                "WAL heal reopen failed for %s handle=%s reason=%s context=%s%s%s",
                db_path,
                handle.describe(),
                reason,
                context,
                ": " if detail else "",
                detail,
            )
            failures.append((handle, exc))
    if failures:
        first_handle, first_exc = failures[0]
        message = (
            f"Runtime database WAL heal failed for {db_path}; "
            f"reason={reason}; context={context}; "
            f"{len(failures)} of {len(handles)} registered connection(s) did not reopen. "
            f"First failed handle: {first_handle.describe()}. "
            "Runtime DB marked degraded; restart or recover sidecars before continuing."
        )
        error = RuntimeDatabaseError(message)
        _mark_runtime_db_degraded(db_path, error)
        for handle in handles:
            handle.mark_degraded(error)
        raise error from first_exc
    # The new generation has no wal until the first write; clear the stamp so
    # the next successful persist records the fresh inode.
    _WAL_GENERATION_INODES.pop(key, None)
    _RUNTIME_DB_DEGRADED.pop(key, None)
    # Publish the completed generation last, so a concurrent forced heal blocked
    # on the heal lock observes the bump and coalesces (M5). Both heal entry
    # points (forced + orphaned) run this under the per-db heal lock, so the
    # increment is serialized per key.
    _HEAL_GENERATION[key] = _HEAL_GENERATION.get(key, 0) + 1
    return True


def _cleanup_recoverable_sidecars(key: str, db_path: Path | str) -> None:
    """Remove only WAL/SHM states that cannot contain committed frames."""
    wal = Path(f"{key}-wal")
    shm = Path(f"{key}-shm")
    try:
        wal_size = wal.stat().st_size
    except FileNotFoundError:
        wal_size = None
    except OSError:
        logger.debug("WAL sidecar stat failed for %s", db_path, exc_info=True)
        return
    try:
        if wal_size == 0:
            wal.unlink(missing_ok=True)
            shm.unlink(missing_ok=True)
            logger.warning(
                "Removed empty SQLite WAL and paired SHM sidecar for %s before reopen",
                db_path,
            )
            return
        if wal_size is None:
            if shm.exists():
                shm.unlink()
                logger.warning(
                    "Removed stale SQLite SHM sidecar for %s because WAL is missing",
                    db_path,
                )
            return
        logger.error(
            "SQLite WAL sidecar for %s is non-empty (%d bytes); leaving WAL/SHM in place",
            db_path,
            wal_size,
        )
    except OSError:
        logger.debug("WAL sidecar cleanup failed for %s", db_path, exc_info=True)


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
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=SQLITE_CONNECT_TIMEOUT_SECONDS
        )
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
