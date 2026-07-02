from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import threading
import time
import weakref
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SQLITE_BUSY_TIMEOUT_MS = 15_000
SQLITE_CONNECT_TIMEOUT_SECONDS = 15.0
SQLITE_HEADER = b"SQLite format 3\x00"
SQLITE_WAL_AUTOCHECKPOINT_PAGES = 1_000
SQLITE_PERSISTENT_LOCK_THRESHOLD = 3


class RuntimeDatabaseError(sqlite3.DatabaseError):
    """Raised when the runtime database file is not usable as SQLite."""


@dataclass(frozen=True, slots=True)
class RuntimeDbDegradedReason:
    reason_code: str
    message: str
    sqlite_error_code: int | None
    operation: str | None
    database_path: str
    detected_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeDbDegradedError(RuntimeDatabaseError):
    def __init__(self, reason: RuntimeDbDegradedReason) -> None:
        super().__init__(
            f"RuntimeDb degraded reason_code={reason.reason_code} "
            f"operation={reason.operation or 'unknown'} path={reason.database_path}: "
            f"{reason.message}"
        )
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RuntimeSqliteHealth:
    healthy: bool
    degraded: bool
    database_path: str
    reason_code: str | None = None
    message: str | None = None
    detected_at: float | None = None
    thorough_check_result: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    try:
        _verify_sqlite_file_header(path)
    except RuntimeDatabaseError as exc:
        error = _degraded_error_from_reason(
            db_path=path,
            reason_code="healthcheck_failed",
            message=str(exc),
            operation="connect_runtime_sqlite",
            sqlite_error_code=None,
        )
        _mark_runtime_db_degraded(path, error)
        raise error from exc
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


def unregister_wal_heal(db_path: Path | str, handle: "StoreWalHealHandle") -> None:
    """Drop a previously registered legacy heal handle (clean owner teardown)."""
    key = _registry_key(db_path)
    with _WAL_HEAL_REGISTRY_LOCK:
        bucket = _WAL_HEAL_REGISTRY.get(key)
        if not bucket:
            return
        bucket[:] = [h for h in bucket if h is not handle and h.alive]
        if not bucket:
            _WAL_HEAL_REGISTRY.pop(key, None)


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
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF == sqlite3.SQLITE_IOERR:
        return True
    return "disk i/o error" in str(exc).lower()


def is_sqlite_closed_connection_error(exc: BaseException) -> bool:
    """True when a stale handle is used after a registry-wide reopen."""
    message = str(exc).lower()
    return isinstance(exc, sqlite3.ProgrammingError) and (
        "closed database" in message or "cannot operate on a closed database" in message
    )


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


def runtime_db_degraded_reason(db_path: Path | str) -> RuntimeDbDegradedReason | None:
    error = runtime_db_degraded_error(db_path)
    if isinstance(error, RuntimeDbDegradedError):
        return error.reason
    if error is None:
        return None
    return RuntimeDbDegradedReason(
        reason_code="unknown_sqlite_critical",
        message=str(error),
        sqlite_error_code=None,
        operation=None,
        database_path=str(Path(db_path)),
        detected_at=time.time(),
    )


def _degraded_error_from_reason(
    *,
    db_path: Path | str,
    reason_code: str,
    message: str,
    operation: str | None,
    sqlite_error_code: int | None,
) -> RuntimeDbDegradedError:
    return RuntimeDbDegradedError(
        RuntimeDbDegradedReason(
            reason_code=reason_code,
            message=message,
            sqlite_error_code=sqlite_error_code,
            operation=operation,
            database_path=str(Path(db_path)),
            detected_at=time.time(),
        )
    )


def _sqlite_error_code(exc: BaseException) -> int | None:
    code = getattr(exc, "sqlite_errorcode", None)
    return int(code) if isinstance(code, int) else None


def _runtime_degraded_reason_code(exc: BaseException) -> str | None:
    message = str(exc).lower()
    name = str(getattr(exc, "sqlite_errorname", "") or "").upper()
    code = _sqlite_error_code(exc)
    if is_sqlite_closed_connection_error(exc):
        return "connection_closed"
    if (
        name == "SQLITE_IOERR_SHORT_READ"
        or code == 522
        or "short read" in message
    ):
        return "sqlite_ioerr_short_read"
    if is_sqlite_disk_io_error(exc):
        return "sqlite_ioerr"
    if "database disk image is malformed" in message or "file is not a database" in message:
        return "healthcheck_failed"
    critical_names = (
        "SQLITE_CORRUPT",
        "SQLITE_NOTADB",
        "SQLITE_FULL",
        "SQLITE_CANTOPEN",
        "SQLITE_PERM",
        "SQLITE_READONLY",
        "SQLITE_UNKNOWN_CRITICAL",
    )
    if name.startswith(critical_names) or "critical" in message:
        return "unknown_sqlite_critical"
    return None


def _is_sqlite_locked_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    name = str(getattr(exc, "sqlite_errorname", "") or "").upper()
    code = _sqlite_error_code(exc)
    return (
        "database is locked" in message
        or name in {"SQLITE_BUSY", "SQLITE_LOCKED"}
        or code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


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


def check_runtime_sqlite_health(
    db_path: Path | str,
    *,
    thorough: bool = False,
    return_status: bool = False,
) -> RuntimeSqliteHealth | None:
    """Fail fast if an existing runtime DB is structurally unhealthy.

    Missing or empty DB files are allowed so first boot can create them. Existing
    files must be real SQLite and pass `quick_check`; `thorough=True` also runs
    `integrity_check`, intended for daemon startup/restart boundaries.
    """
    path = Path(db_path)
    degraded = runtime_db_degraded_error(path)
    if degraded is not None:
        health = _health_from_degraded(path, degraded)
        if return_status:
            return health
        raise degraded
    try:
        _verify_sqlite_file_header(path)
        if not path.exists() or path.stat().st_size == 0:
            health = RuntimeSqliteHealth(
                healthy=True,
                degraded=False,
                database_path=str(path),
                thorough_check_result="empty_or_missing",
            )
            return health if return_status else None
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=SQLITE_CONNECT_TIMEOUT_SECONDS
        )
        try:
            _run_sqlite_health_pragma(conn, path, "quick_check")
            if thorough:
                _run_sqlite_health_pragma(conn, path, "integrity_check")
        finally:
            conn.close()
    except (RuntimeDatabaseError, sqlite3.DatabaseError) as exc:
        error = _degraded_error_from_reason(
            db_path=path,
            reason_code="healthcheck_failed",
            message=(
                f"Runtime database failed SQLite health check: {path}. "
                "Do not restart the daemon until the DB is recovered from a verified backup."
            ),
            operation="check_runtime_sqlite_health",
            sqlite_error_code=_sqlite_error_code(exc),
        )
        _mark_runtime_db_degraded(path, error)
        if return_status:
            return _health_from_degraded(path, error)
        raise error from exc
    health = RuntimeSqliteHealth(
        healthy=True,
        degraded=False,
        database_path=str(path),
        thorough_check_result="integrity_check_ok" if thorough else "quick_check_ok",
    )
    return health if return_status else None


def _health_from_degraded(
    db_path: Path | str,
    error: RuntimeDatabaseError,
) -> RuntimeSqliteHealth:
    reason = error.reason if isinstance(error, RuntimeDbDegradedError) else None
    return RuntimeSqliteHealth(
        healthy=False,
        degraded=True,
        database_path=str(Path(db_path)),
        reason_code=reason.reason_code if reason is not None else "unknown_sqlite_critical",
        message=reason.message if reason is not None else str(error),
        detected_at=reason.detected_at if reason is not None else None,
        thorough_check_result="degraded",
    )


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


# Sentinel: "use the connection's configured row_factory" (vs. an explicit
# per-cursor override). Distinct from None, which means "tuple rows".
_RUNTIME_DB_DEFAULT_ROW_FACTORY = object()


class RuntimeDb:
    """Single owner of a runtime SQLite connection + a shared re-entrant lock.

    Foundation for the single-writer collapse of ``claw.db`` (F1.1/F1.2,
    RAÍZ #1). Production stores share this one connection and lock; legacy
    ``runtime_db=None`` stores keep their own connections for tests/back-compat.

    The connection is owned privately: callers serialize through
    :meth:`cursor` / :meth:`transaction` / :meth:`try_cursor` (all under the
    shared lock) and must NOT cache a raw connection. There is intentionally no
    public ``.conn`` accessor; tests use :meth:`current_connection_id` /
    :meth:`_current_connection_for_tests`.

    The lock is an :class:`threading.RLock` because runtime read paths (e.g.
    ``MemoryStore``'s ``@_synchronized`` methods) can nest under an
    already-held lock. Row shape is selected per cursor (``cur.row_factory``);
    the shared connection's ``row_factory`` is never mutated, so concurrent
    callers wanting different shapes never race on global connection state.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        row_factory: bool = True,
        persistent_lock_threshold: int = SQLITE_PERSISTENT_LOCK_THRESHOLD,
        degraded_event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._row_factory = row_factory
        self._lock = threading.RLock()
        self._in_transaction = False
        self._closed = False
        self._persistent_lock_threshold = max(1, int(persistent_lock_threshold))
        self._consecutive_locked_errors = 0
        self._degraded_event_sink = degraded_event_sink
        # Opened through the shared connect/configure path so the durable
        # runtime pragmas (WAL, synchronous=FULL, busy_timeout, foreign_keys)
        # are preserved unchanged.
        self._conn = connect_runtime_sqlite(self.db_path, row_factory=row_factory)

    # -- identity / test seams (no public .conn for stores to cache) ----------
    def current_connection_id(self) -> int:
        """Identity of the current connection (for tests asserting a reopen
        swapped it). Not a handle stores may cache."""
        with self._lock:
            return id(self._conn)

    def _current_connection_for_tests(self) -> sqlite3.Connection:
        with self._lock:
            return self._conn

    def _reconnect_for_tests(self) -> None:
        """Swap the owned connection directly under the RuntimeDb lock.

        F1.3 removes RuntimeDb from the legacy WAL-heal registry; this seam
        keeps dynamic-handle tests able to prove stores follow the owner after
        an owner-controlled reconnect.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("RuntimeDb is closed")
            old_conn = self._conn
            self._conn = connect_runtime_sqlite(self.db_path, row_factory=self._row_factory)
            try:
                old_conn.close()
            except Exception:
                logger.debug("RuntimeDb old connection close failed", exc_info=True)

    @property
    def degraded_reason(self) -> RuntimeDbDegradedReason | None:
        return runtime_db_degraded_reason(self.db_path)

    def healthcheck(self, *, thorough: bool = False) -> RuntimeSqliteHealth:
        return check_runtime_sqlite_health(
            self.db_path,
            thorough=thorough,
            return_status=True,
        )

    def _ensure_operational(self, operation: str) -> None:
        degraded = runtime_db_degraded_error(self.db_path)
        if degraded is not None:
            raise degraded
        if self._closed:
            raise self._mark_degraded(
                reason_code="connection_closed",
                message="RuntimeDb connection is closed",
                operation=operation,
                sqlite_error_code=None,
            )

    def _record_sqlite_success(self) -> None:
        self._consecutive_locked_errors = 0

    def _handle_sqlite_exception(self, operation: str, exc: BaseException) -> None:
        if _is_sqlite_locked_error(exc):
            self._consecutive_locked_errors += 1
            if self._consecutive_locked_errors >= self._persistent_lock_threshold:
                raise self._mark_degraded(
                    reason_code="persistent_lock",
                    message=str(exc),
                    operation=operation,
                    sqlite_error_code=_sqlite_error_code(exc),
                ) from exc
            return
        reason_code = _runtime_degraded_reason_code(exc)
        if reason_code is None:
            return
        raise self._mark_degraded(
            reason_code=reason_code,
            message=str(exc),
            operation=operation,
            sqlite_error_code=_sqlite_error_code(exc),
        ) from exc

    def _mark_degraded(
        self,
        *,
        reason_code: str,
        message: str,
        operation: str | None,
        sqlite_error_code: int | None,
    ) -> RuntimeDbDegradedError:
        existing = runtime_db_degraded_error(self.db_path)
        if isinstance(existing, RuntimeDbDegradedError):
            return existing
        error = _degraded_error_from_reason(
            db_path=self.db_path,
            reason_code=reason_code,
            message=message,
            operation=operation,
            sqlite_error_code=sqlite_error_code,
        )
        _mark_runtime_db_degraded(self.db_path, error)
        payload = error.reason.to_dict()
        logger.critical(
            "RuntimeDb marked degraded reason_code=%s operation=%s path=%s",
            error.reason.reason_code,
            error.reason.operation,
            error.reason.database_path,
        )
        if self._degraded_event_sink is not None:
            try:
                self._degraded_event_sink(payload)
            except Exception:
                logger.exception("RuntimeDb degraded event sink failed")
        return error

    # -- store wiring (F1.1a1) ------------------------------------------------
    @property
    def lock(self) -> threading.RLock:
        """The shared re-entrant lock. A store that shares this RuntimeDb sets
        ``self._lock = runtime_db.lock`` so its existing ``with self._lock:``
        blocks serialize on the one shared lock (RLock: memory's
        ``@_synchronized`` reads nest)."""
        return self._lock

    def connection_handle(self, *, row_factory: bool = True) -> "_RuntimeConnHandle":
        """Return a non-owning, dynamic facade over this RuntimeDb's CURRENT
        connection. A store assigns it to ``self._conn`` and keeps using
        ``self._conn.execute(...)`` / ``.executescript()`` / ``.commit()`` /
        ``.rollback()`` unchanged, while the real connection lives here and a
        RuntimeDb-owned reconnect that swaps it stays invisible (no stale
        connection). The store must hold :attr:`lock` around its access, exactly
        as before."""
        return _RuntimeConnHandle(self, row_factory=row_factory)

    # -- access ---------------------------------------------------------------
    @contextlib.contextmanager
    def cursor(self, *, row_factory=_RUNTIME_DB_DEFAULT_ROW_FACTORY):
        """Yield a cursor under the shared lock. No implicit transaction
        control — for reads or caller-managed writes; ``cursor()`` never commits
        on its own. It MAY be nested inside :meth:`transaction` (the re-entrant
        lock allows it): the cursor then shares that open transaction, so its
        writes commit or roll back atomically with the enclosing
        ``transaction()``. ``row_factory`` shapes rows for THIS cursor only
        (``None`` -> tuples); the shared connection's factory is never mutated."""
        cur = None
        with self._lock:
            self._ensure_operational("RuntimeDb.cursor")
            try:
                cur = self._conn.cursor()
                if row_factory is not _RUNTIME_DB_DEFAULT_ROW_FACTORY:
                    cur.row_factory = row_factory
                yield cur
                self._record_sqlite_success()
            except BaseException as exc:
                self._handle_sqlite_exception("RuntimeDb.cursor", exc)
                raise
            finally:
                if cur is not None:
                    cur.close()

    @contextlib.contextmanager
    def transaction(self, *, row_factory=_RUNTIME_DB_DEFAULT_ROW_FACTORY):
        """Yield a cursor under the shared lock, committing on success and
        rolling back on any exception. Nested transactions on the same
        RuntimeDb are rejected: one connection has a single transaction scope,
        so an inner commit would silently commit the outer's writes."""
        with self._lock:
            self._ensure_operational("RuntimeDb.transaction")
            if self._in_transaction:
                raise RuntimeError(
                    "nested RuntimeDb.transaction() is not supported "
                    "(one connection has a single transaction scope)"
                )
            self._in_transaction = True
            cur = None
            try:
                cur = self._conn.cursor()
                if row_factory is not _RUNTIME_DB_DEFAULT_ROW_FACTORY:
                    cur.row_factory = row_factory
                yield cur
                self._conn.commit()
                self._record_sqlite_success()
            except BaseException as exc:
                rollback_exc: BaseException | None = None
                try:
                    self._conn.rollback()
                except BaseException as caught_rollback_exc:
                    rollback_exc = caught_rollback_exc
                self._handle_sqlite_exception("RuntimeDb.transaction", exc)
                if rollback_exc is not None:
                    self._handle_sqlite_exception("RuntimeDb.transaction.rollback", rollback_exc)
                raise
            finally:
                if cur is not None:
                    cur.close()
                self._in_transaction = False

    @contextlib.contextmanager
    def try_acquire(self):
        """Non-blocking lock acquire. Yields ``True`` if acquired (released on
        exit), else yields ``False`` immediately without blocking. The
        primitive F1.1a1 uses to preserve observe's fast-drop-on-contention
        emit semantics."""
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    @contextlib.contextmanager
    def try_cursor(self, *, row_factory=_RUNTIME_DB_DEFAULT_ROW_FACTORY):
        """Non-blocking cursor: yields a cursor if the lock is immediately
        available, else yields ``None`` (caller drops the work). Built on
        :meth:`try_acquire`."""
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            yield None
            return
        cur = None
        try:
            self._ensure_operational("RuntimeDb.try_cursor")
            cur = self._conn.cursor()
            if row_factory is not _RUNTIME_DB_DEFAULT_ROW_FACTORY:
                cur.row_factory = row_factory
            try:
                yield cur
                self._record_sqlite_success()
            except BaseException as exc:
                self._handle_sqlite_exception("RuntimeDb.try_cursor", exc)
                raise
            finally:
                if cur is not None:
                    cur.close()
        finally:
            self._lock.release()

    # -- lifecycle ------------------------------------------------------------
    def health(self) -> bool:
        """Lightweight liveness probe: ``True`` if a trivial query succeeds."""
        with self._lock:
            if self._closed:
                return False
        return self.healthcheck().healthy

    def synthetic_healthcheck(
        self,
        *,
        correlation_id: str,
        now: float | None = None,
        ttl_seconds: float = 300.0,
    ) -> dict[str, Any]:
        correlation_id = str(correlation_id or "").strip()
        if not correlation_id:
            raise ValueError("correlation_id is required")
        current = time.time() if now is None else float(now)
        ttl = max(1.0, float(ttl_seconds))
        expires_at = current + ttl
        namespace = "synthetic_healthcheck"
        with self.transaction() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_synthetic_healthcheck (
                    correlation_id TEXT PRIMARY KEY,
                    namespace TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            cur.execute(
                "DELETE FROM runtime_synthetic_healthcheck WHERE expires_at <= ?",
                (current,),
            )
            cur.execute(
                """
                INSERT OR REPLACE INTO runtime_synthetic_healthcheck (
                    correlation_id, namespace, created_at, expires_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (correlation_id, namespace, current, expires_at),
            )
            row = cur.execute(
                """
                SELECT correlation_id, namespace, created_at, expires_at
                FROM runtime_synthetic_healthcheck
                WHERE correlation_id = ?
                """,
                (correlation_id,),
            ).fetchone()
        return {
            "correlation_id": str(row["correlation_id"]),
            "namespace": str(row["namespace"]),
            "created_at": float(row["created_at"]),
            "expires_at": float(row["expires_at"]),
        }

    def close(self) -> None:
        """Close the owned connection. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except Exception:
                logger.debug("RuntimeDb connection close failed", exc_info=True)


class _RuntimeConnHandle:
    """Non-owning, dynamic facade over a :class:`RuntimeDb`'s current sqlite3
    connection (F1.1a1).

    A runtime store assigns this to ``self._conn`` and keeps calling the four
    connection methods it already uses — ``execute``, ``executescript``,
    ``commit``, ``rollback`` — each delegating to RuntimeDb's CURRENT connection.
    RuntimeDb can swap that connection under the shared lock, and because the
    handle resolves ``self._db._conn`` on every call it never holds a stale
    connection. ``execute`` shapes its result rows per handle (``Row`` or tuple)
    on a fresh cursor, never mutating the shared connection's ``row_factory``.

    It deliberately exposes NO ``close``/``cursor``/``.conn`` and does not own
    the connection: RuntimeDb is the sole owner of the connection lifecycle.
    Callers must hold ``RuntimeDb.lock`` around their access (the store keeps
    its existing ``with self._lock:`` blocks, now bound to the shared lock)."""

    __slots__ = ("_db", "_row_factory")

    def __init__(self, runtime_db: "RuntimeDb", *, row_factory: bool = True) -> None:
        self._db = runtime_db
        self._row_factory = sqlite3.Row if row_factory else None

    def execute(self, sql: str, parameters=()):  # noqa: ANN001 - sqlite param shape
        self._db._ensure_operational("RuntimeDb.connection_handle.execute")
        try:
            cur = self._db._conn.cursor()
            cur.row_factory = self._row_factory
            result = cur.execute(sql, parameters)
            self._db._record_sqlite_success()
            return result
        except BaseException as exc:
            self._db._handle_sqlite_exception("RuntimeDb.connection_handle.execute", exc)
            raise

    def executescript(self, sql_script: str):
        self._db._ensure_operational("RuntimeDb.connection_handle.executescript")
        try:
            result = self._db._conn.executescript(sql_script)
            self._db._record_sqlite_success()
            return result
        except BaseException as exc:
            self._db._handle_sqlite_exception("RuntimeDb.connection_handle.executescript", exc)
            raise

    def commit(self) -> None:
        self._db._ensure_operational("RuntimeDb.connection_handle.commit")
        try:
            self._db._conn.commit()
            self._db._record_sqlite_success()
        except BaseException as exc:
            self._db._handle_sqlite_exception("RuntimeDb.connection_handle.commit", exc)
            raise

    def rollback(self) -> None:
        try:
            self._db._conn.rollback()
        except BaseException as exc:
            self._db._handle_sqlite_exception("RuntimeDb.connection_handle.rollback", exc)
            raise
