"""DB-only checkpointing primitive for Claw — Phase 1.

See docs/superpowers/specs/2026-04-19-checkpointing-design.md.
"""
from __future__ import annotations

import logging
import secrets
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claw_v2.memory import MemoryStore

logger = logging.getLogger(__name__)


class CheckpointService:
    def __init__(
        self,
        *,
        memory: "MemoryStore",
        snapshots_dir: Path,
        ring_size: int = 10,
    ) -> None:
        self.memory = memory
        self.snapshots_dir = Path(snapshots_dir)
        self.ring_size = ring_size
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        # Serializes the metadata+backup+rotation lifecycle among concurrent
        # create() calls WITHOUT holding memory._lock during the copy: an
        # overlapping create's ring rotation could otherwise purge the
        # in-flight snapshot (its metadata row is visible before its file
        # finishes writing).
        self._create_lock = threading.Lock()

    def create(
        self,
        *,
        trigger_reason: str,
        session_id: str | None = None,
        consecutive_failures: int = 0,
    ) -> str:
        ckpt_id = f"ckpt_{secrets.token_hex(4)}"
        file_path = self.snapshots_dir / f"{ckpt_id}.db"
        with self._create_lock:
            return self._create_locked(
                ckpt_id=ckpt_id,
                file_path=file_path,
                trigger_reason=trigger_reason,
                session_id=session_id,
                consecutive_failures=consecutive_failures,
            )

    def _create_locked(
        self,
        *,
        ckpt_id: str,
        file_path: Path,
        trigger_reason: str,
        session_id: str | None,
        consecutive_failures: int,
    ) -> str:
        with self.memory._lock:
            # Insert the metadata row FIRST and commit so the row is visible
            # to the subsequent backup() — snapshot must be self-describing.
            self.memory._conn.execute(
                "INSERT INTO checkpoints "
                "(ckpt_id, trigger_reason, session_id, consecutive_failures, file_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (ckpt_id, trigger_reason, session_id, consecutive_failures, str(file_path)),
            )
            self.memory._conn.commit()
        # Copy WITHOUT holding memory._lock and on a dedicated source
        # connection: WAL gives it a consistent snapshot, and a single-pass
        # backup leaves no between-step window for concurrent writers
        # (observe/jobs/ledger) to trigger restarts. The previous incremental
        # backup on the live connection held the memory lock for the whole
        # copy and restarted on every external write — under load the hot
        # path froze until "database is locked" surfaced to the user.
        # Write to a staging file and rename only after the copy completes:
        # the committed row's file_path must not exist until the snapshot is
        # whole, or a concurrent /rollback (or a crash mid-copy) could restore
        # a partial/truncated database (PR #91 review, codex P2).
        tmp_path = file_path.with_name(file_path.name + ".tmp")
        source_conn: sqlite3.Connection | None = None
        target_conn: sqlite3.Connection | None = None
        try:
            source_conn = self._open_backup_source()
            target_conn = sqlite3.connect(tmp_path)
            source_conn.backup(target_conn)
            target_conn.close()
            target_conn = None
            tmp_path.replace(file_path)  # atomic publish (same dir/filesystem)
        except Exception:
            # Roll back the metadata row so the DB stays clean.
            with self.memory._lock:
                try:
                    self.memory._conn.execute(
                        "DELETE FROM checkpoints WHERE ckpt_id = ?", (ckpt_id,),
                    )
                    self.memory._conn.commit()
                except sqlite3.Error:
                    logger.warning(
                        "Checkpoint rollback: DELETE failed for %s", ckpt_id,
                        exc_info=True,
                    )
            if target_conn is not None:
                try:
                    target_conn.close()
                except Exception:
                    pass
                target_conn = None
            tmp_path.unlink(missing_ok=True)
            file_path.unlink(missing_ok=True)
            raise
        finally:
            for conn in (source_conn, target_conn):
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        with self.memory._lock:
            # Ring rotation (unchanged from CP3)
            try:
                rows_to_purge = self.memory._conn.execute(
                    "SELECT ckpt_id, file_path FROM checkpoints "
                    "ORDER BY created_at ASC, id ASC"
                ).fetchall()
                excess = len(rows_to_purge) - self.ring_size
                if excess > 0:
                    for row in rows_to_purge[:excess]:
                        try:
                            Path(row["file_path"]).unlink(missing_ok=True)
                        except OSError:
                            logger.warning(
                                "Checkpoint rotation: failed to unlink %s",
                                row["file_path"], exc_info=True,
                            )
                        try:
                            self.memory._conn.execute(
                                "DELETE FROM checkpoints WHERE ckpt_id = ?",
                                (row["ckpt_id"],),
                            )
                        except sqlite3.Error:
                            logger.warning(
                                "Checkpoint rotation: failed to delete row %s",
                                row["ckpt_id"], exc_info=True,
                            )
                    self.memory._conn.commit()
            except Exception:
                logger.warning("Checkpoint rotation encountered an error", exc_info=True)
        return ckpt_id

    def _open_backup_source(self) -> sqlite3.Connection:
        """Dedicated read-only connection for backups (overridable in tests).

        mode=ro guarantees the backup can never write the live DB; the 15s
        timeout matches the runtime busy_timeout so the copy waits out
        contention instead of failing fast with "database is locked".

        Build the file: URI via Path.as_uri() so a path containing URI
        delimiters (a `?` or `#`, or Windows backslashes) is percent-encoded
        instead of truncating the URI and silently opening the wrong database.
        """
        raw = str(self.memory.db_path)
        if raw == ":memory:" or "mode=memory" in raw:
            # An in-memory db has no file to copy via a read-only file: URI;
            # as_uri() would silently create a file literally named ":memory:".
            raise ValueError(
                "checkpoint backup requires a file-backed database, not an in-memory db"
            )
        db_uri = f"{Path(self.memory.db_path).absolute().as_uri()}?mode=ro"
        return sqlite3.connect(db_uri, uri=True, timeout=15.0)

    def list(self) -> list[dict]:
        rows = self.memory._conn.execute(
            "SELECT ckpt_id, created_at, trigger_reason, session_id, "
            "consecutive_failures, file_path, pending_restore, restored_at "
            "FROM checkpoints "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def latest(self) -> dict | None:
        rows = self.list()
        return rows[0] if rows else None

    def schedule_restore(self, ckpt_id: str) -> None:
        with self.memory._lock:
            row = self.memory._conn.execute(
                "SELECT file_path FROM checkpoints WHERE ckpt_id = ?",
                (ckpt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(ckpt_id)
            if not Path(row["file_path"]).exists():
                raise FileNotFoundError(row["file_path"])
            self.memory._conn.execute(
                "UPDATE checkpoints SET pending_restore = 0 WHERE pending_restore = 1"
            )
            self.memory._conn.execute(
                "UPDATE checkpoints SET pending_restore = 1 WHERE ckpt_id = ?",
                (ckpt_id,),
            )
            self.memory._conn.commit()


def apply_pending_restore_if_any(db_path: Path) -> str | None:
    """Called from MemoryStore.__init__ BEFORE any schema migrations.

    If a pending_restore flag is set and the referenced snapshot file exists,
    copy the snapshot over db_path and mark restored_at. Returns the applied
    ckpt_id or None.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    try:
        probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        probe.row_factory = sqlite3.Row
        table_exists = probe.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='checkpoints'"
        ).fetchone() is not None
        if not table_exists:
            return None
        row = probe.execute(
            "SELECT ckpt_id, file_path FROM checkpoints "
            "WHERE pending_restore = 1 LIMIT 1"
        ).fetchone()
    finally:
        probe.close()
    if row is None:
        return None
    snapshot_path = Path(row["file_path"])
    ckpt_id = row["ckpt_id"]
    if not snapshot_path.exists():
        logger.warning(
            "Pending restore points to missing snapshot %s; clearing flag.",
            snapshot_path,
        )
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE checkpoints SET pending_restore = 0 WHERE ckpt_id = ?",
                (ckpt_id,),
            )
            conn.commit()
        finally:
            conn.close()
        return None
    # A leftover -wal/-shm from the old database would be recovered on the
    # next open and replay stale frames over the restored snapshot, silently
    # corrupting it. Remove the sidecars before replacing the DB file.
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    shutil.copy(snapshot_path, db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE checkpoints "
            "SET pending_restore = 0, restored_at = CURRENT_TIMESTAMP "
            "WHERE ckpt_id = ?",
            (ckpt_id,),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("Applied checkpoint %s from %s", ckpt_id, snapshot_path)
    return ckpt_id
