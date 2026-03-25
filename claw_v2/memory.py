from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_trust TEXT NOT NULL DEFAULT 'untrusted',
    confidence REAL NOT NULL DEFAULT 0.5,
    entity_tags TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT,
    valid_until TEXT,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_sessions (
    app_session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_session_id TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_session_id, provider)
);
"""


class MemoryStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def store_message(self, session_id: str, role: str, content: str) -> None:
        self._conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )
        self._conn.commit()

    def get_recent_messages(self, session_id: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT role, content, created_at
            FROM messages
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def store_fact(
        self,
        key: str,
        value: str,
        *,
        source: str,
        source_trust: str = "untrusted",
        confidence: float = 0.5,
        entity_tags: Iterable[str] = (),
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO facts (
                key, value, source, source_trust, confidence, entity_tags, valid_from, valid_until
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                value,
                source,
                source_trust,
                confidence,
                json.dumps(list(entity_tags)),
                valid_from,
                valid_until,
            ),
        )
        self._conn.commit()

    def search_facts(self, query: str, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT key, value, source, source_trust, confidence, entity_tags
            FROM facts
            WHERE key LIKE ? OR value LIKE ?
            ORDER BY confidence DESC, id DESC
            LIMIT ?
            """,
            (f"%{query}%", f"%{query}%", limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_profile_facts(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT key, value, source_trust, confidence
            FROM facts
            WHERE key LIKE 'profile.%'
            ORDER BY confidence DESC, id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def build_context(self, session_id: str, message: str, budget: int = 16000) -> str:
        recent = self.get_recent_messages(session_id, limit=20)
        facts = self.get_profile_facts()[:10]
        recent_lines = [f"{row['role']}: {row['content']}" for row in recent]
        fact_lines = [f"{row['key']}={row['value']}" for row in facts]
        context = "\n".join(
            ["# Profile facts", *fact_lines, "# Recent messages", *recent_lines, "# Current input", message]
        )
        return context[:budget]

    def get_provider_session(self, app_session_id: str, provider: str) -> str | None:
        row = self._conn.execute(
            """
            SELECT provider_session_id
            FROM provider_sessions
            WHERE app_session_id = ? AND provider = ?
            """,
            (app_session_id, provider),
        ).fetchone()
        if row is None:
            return None
        return row["provider_session_id"]

    def link_provider_session(self, app_session_id: str, provider: str, provider_session_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO provider_sessions (app_session_id, provider, provider_session_id)
            VALUES (?, ?, ?)
            ON CONFLICT(app_session_id, provider)
            DO UPDATE SET provider_session_id = excluded.provider_session_id, updated_at = CURRENT_TIMESTAMP
            """,
            (app_session_id, provider, provider_session_id),
        )
        self._conn.commit()
