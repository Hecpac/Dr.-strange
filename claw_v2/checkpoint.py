"""DB-only checkpointing primitive for Claw — Phase 1.

See docs/superpowers/specs/2026-04-19-checkpointing-design.md.
"""
from __future__ import annotations

import logging
import secrets
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
        target_conn: sqlite3.Connection | None = None
        try:
            with self.memory._lock:
                target_conn = sqlite3.connect(file_path)
                self.memory._conn.backup(target_conn, pages=100, sleep=0.001)
                target_conn.close()
                target_conn = None
                self.memory._conn.execute(
                    "INSERT INTO checkpoints "
                    "(ckpt_id, trigger_reason, session_id, consecutive_failures, file_path) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ckpt_id, trigger_reason, session_id, consecutive_failures, str(file_path)),
                )
                self.memory._conn.commit()
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
        except Exception:
            if target_conn is not None:
                try:
                    target_conn.close()
                except Exception:
                    pass
            file_path.unlink(missing_ok=True)
            raise
        return ckpt_id
