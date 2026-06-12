"""Bloqueante review #2: the ``task_outcomes`` CHECK migration must be
crash-safe.

The original migration RENAMEd the live table to ``task_outcomes_old``,
created a new one with the broader CHECK, then INSERTed and DROPped.
If the process died between RENAME and INSERT, the next boot saw the
fresh CHECK table already present and EARLY-RETURNed, leaving
``task_outcomes_old`` orphan and SILENTLY losing every row.

We pin two invariants:
  1. A wrap in ``BEGIN IMMEDIATE; ... COMMIT;`` so the rename + create +
     insert + drop is one atomic unit when uninterrupted.
  2. On reboot with an orphan ``task_outcomes_old`` present, the
     migration resumes — copies remaining rows, verifies count equality,
     drops the old table.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from claw_v2.memory import MemoryStore


def _create_legacy_task_outcomes(conn: sqlite3.Connection) -> None:
    """Pre-migration schema after the ADD_COLUMN migrations had applied:
    tags is NOT NULL DEFAULT '[]', matching what production DBs carry
    into the wider-CHECK migration."""
    conn.executescript(
        """
        CREATE TABLE task_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            task_id TEXT NOT NULL,
            description TEXT NOT NULL,
            approach TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failure', 'partial')),
            lesson TEXT NOT NULL,
            error_snippet TEXT,
            retries INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            tags TEXT NOT NULL DEFAULT '[]',
            predicted_confidence REAL,
            feedback TEXT
        );
        """
    )


def _seed_outcomes(conn: sqlite3.Connection, rows: int, *, table: str = "task_outcomes") -> None:
    for idx in range(rows):
        conn.execute(
            f"INSERT INTO {table} (task_type, task_id, description, approach, outcome, lesson) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (
                "telegram_message",
                f"task_{idx}",
                f"description #{idx}",
                "brain.handle_message",
                "success",
                "previously usable reply",
            ),
        )
    conn.commit()


class TaskOutcomesMigrationResumeTests(unittest.TestCase):
    def test_fresh_db_migration_applies_new_check(self) -> None:
        """No previous data: the migration creates the new table with the
        wider CHECK and does not leave any ``task_outcomes_old``."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "claw.db"
            MemoryStore(db)
            with sqlite3.connect(db) as conn:
                sql_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='task_outcomes'"
                ).fetchone()
                assert sql_row is not None
                self.assertIn("usable_reply_unverified", sql_row[0])
                orphan = conn.execute(
                    "SELECT name FROM sqlite_master WHERE name='task_outcomes_old'"
                ).fetchone()
                self.assertIsNone(orphan, "task_outcomes_old must not survive a fresh boot")

    def test_legacy_db_migration_is_lossless_and_drops_old(self) -> None:
        """Pre-migration DB with the narrow CHECK and rows: after boot,
        all rows are preserved, the CHECK is the new (wider) one, and
        ``task_outcomes_old`` no longer exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "claw.db"
            with sqlite3.connect(db) as conn:
                _create_legacy_task_outcomes(conn)
                _seed_outcomes(conn, 5)
            MemoryStore(db)
            with sqlite3.connect(db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM task_outcomes").fetchone()[0]
                self.assertEqual(count, 5)
                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE name='task_outcomes_old'"
                    ).fetchone()
                )

    def test_legacy_db_with_child_rows_migrates_under_foreign_keys_on(self) -> None:
        """AH2 (2026-06-11): the runtime connection enables PRAGMA
        foreign_keys=ON. With child rows in outcome_entity_edges /
        outcome_embeddings the rebuild used to fail at DROP (FK violation),
        and the RENAME rewrote child REFERENCES to task_outcomes_old —
        pointing them at a dropped table. The migration must complete and
        leave children referencing the rebuilt task_outcomes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "claw.db"
            with sqlite3.connect(db) as conn:
                _create_legacy_task_outcomes(conn)
                _seed_outcomes(conn, 5)
                conn.executescript(
                    """
                    CREATE TABLE outcome_embeddings (
                        outcome_id INTEGER PRIMARY KEY REFERENCES task_outcomes(id),
                        embedding TEXT NOT NULL
                    );
                    CREATE TABLE outcome_entity_edges (
                        outcome_id INTEGER NOT NULL REFERENCES task_outcomes(id) ON DELETE CASCADE,
                        entity_tag TEXT NOT NULL,
                        PRIMARY KEY (outcome_id, entity_tag)
                    );
                    INSERT INTO outcome_embeddings VALUES (1, '[0.1]');
                    INSERT INTO outcome_entity_edges VALUES (1, 'tag-a');
                    """
                )
            MemoryStore(db)
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                schema = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='task_outcomes'"
                ).fetchone()[0]
                self.assertIn("usable_reply_unverified", schema)
                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE name='task_outcomes_old'"
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM task_outcomes").fetchone()[0], 5
                )
                # Children must still reference the live table, not the
                # renamed-and-dropped one, and pass FK enforcement.
                for child in ("outcome_embeddings", "outcome_entity_edges"):
                    child_sql = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE name=?", (child,)
                    ).fetchone()[0]
                    self.assertNotIn("task_outcomes_old", child_sql, child)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.execute("INSERT INTO outcome_entity_edges VALUES (2, 'tag-b')")

    def test_orphan_task_outcomes_old_after_crash_is_resumed(self) -> None:
        """Simulates: previous run ran RENAME (creating task_outcomes_old)
        and was killed before the new table got the rows. On next boot
        the migration must finish the copy from task_outcomes_old and
        drop it — NOT early-return and lose data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "claw.db"
            with sqlite3.connect(db) as conn:
                # `task_outcomes_old` carries the rows the previous boot
                # was about to migrate.
                _create_legacy_task_outcomes(conn)
                _seed_outcomes(conn, 7)
                conn.execute("ALTER TABLE task_outcomes RENAME TO task_outcomes_old")
                conn.commit()
            # At this point the DB looks like: only task_outcomes_old exists.
            with sqlite3.connect(db) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'task_outcomes%'"
                ).fetchall()
                self.assertEqual({r[0] for r in rows}, {"task_outcomes_old"})

            MemoryStore(db)
            with sqlite3.connect(db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM task_outcomes").fetchone()[0]
                self.assertEqual(count, 7, "resume must copy ALL rows from task_outcomes_old")
                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE name='task_outcomes_old'"
                    ).fetchone(),
                    "task_outcomes_old must be dropped once the copy is verified lossless",
                )
                # The CHECK constraint must be the NEW one.
                schema = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name='task_outcomes'"
                ).fetchone()[0]
                self.assertIn("usable_reply_unverified", schema)

    def test_orphan_with_partial_new_table_does_not_lose_rows(self) -> None:
        """Simulates the very specific race the review flagged: the
        previous boot ran RENAME then created the new table (but did NOT
        INSERT). Next boot must NOT early-return on the presence of the
        new CHECK; it must finish the copy from task_outcomes_old."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / "claw.db"
            with sqlite3.connect(db) as conn:
                _create_legacy_task_outcomes(conn)
                _seed_outcomes(conn, 4)
                # Rename + create new table (empty) — die before INSERT.
                conn.execute("ALTER TABLE task_outcomes RENAME TO task_outcomes_old")
                conn.executescript(
                    """
                    CREATE TABLE task_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_type TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        description TEXT NOT NULL,
                        approach TEXT NOT NULL,
                        outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failure', 'partial', 'usable_reply_unverified')),
                        lesson TEXT NOT NULL,
                        error_snippet TEXT,
                        retries INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        tags TEXT,
                        predicted_confidence REAL,
                        feedback TEXT
                    );
                    """
                )
                conn.commit()
            MemoryStore(db)
            with sqlite3.connect(db) as conn:
                count = conn.execute("SELECT COUNT(*) FROM task_outcomes").fetchone()[0]
                self.assertEqual(
                    count,
                    4,
                    "resume must complete the copy even when the new table already exists",
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE name='task_outcomes_old'"
                    ).fetchone()
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
