"""DB-only checkpointing primitive for Claw — Phase 1.

See docs/superpowers/specs/2026-04-19-checkpointing-design.md.
"""
from __future__ import annotations

import logging
import secrets
import shutil
import sqlite3
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

    def create(
        self,
        *,
        trigger_reason: str,
        session_id: str | None = None,
        consecutive_failures: int = 0,
    ) -> str:
        ckpt_id = f"ckpt_{secrets.token_hex(4)}"
        file_path = self.snapshots_dir / f"{ckpt_id}.db"
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
            target_conn: sqlite3.Connection | None = None
            try:
                target_conn = sqlite3.connect(file_path)
                self.memory._conn.backup(target_conn, pages=100, sleep=0.001)
            except Exception:
                # Roll back the metadata row so the DB stays clean.
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
                file_path.unlink(missing_ok=True)
                raise
            finally:
                if target_conn is not None:
                    try:
                        target_conn.close()
                    except Exception:
                        pass

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
