from __future__ import annotations

import json
import math
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterable


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
    agent_name TEXT NOT NULL DEFAULT 'system',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS provider_sessions (
    app_session_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_session_id TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (app_session_id, provider)
);

CREATE TABLE IF NOT EXISTS fact_embeddings (
    fact_id INTEGER PRIMARY KEY REFERENCES facts(id),
    embedding TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cron_state (
    job_name TEXT PRIMARY KEY,
    last_run_at REAL NOT NULL DEFAULT 0.0,
    runs INTEGER NOT NULL DEFAULT 0
);
"""


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _simple_embedding(text: str, dim: int = 128) -> list[float]:
    """Lightweight bag-of-chars embedding. Replace with a real model for production."""
    vec = [0.0] * dim
    for i, ch in enumerate(text.lower()):
        vec[ord(ch) % dim] += 1.0 / (1 + i * 0.01)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


_MIGRATION_ADD_AGENT_NAME = """
ALTER TABLE facts ADD COLUMN agent_name TEXT NOT NULL DEFAULT 'system';
"""


class MemoryStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        cursor = self._conn.execute("PRAGMA table_info(facts)")
        columns = {row[1] for row in cursor.fetchall()}
        if "agent_name" not in columns:
            try:
                self._conn.execute(_MIGRATION_ADD_AGENT_NAME)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    def store_message(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
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

    def count_messages(self, session_id: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) as count
            FROM messages
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        return row["count"] if row else 0

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
        agent_name: str = "system",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO facts (
                    key, value, source, source_trust, confidence, entity_tags, valid_from, valid_until, agent_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    agent_name,
                ),
            )
            self._conn.commit()

    def search_facts(self, query: str, limit: int = 10, agent_name: str | None = None) -> list[dict]:
        if agent_name:
            rows = self._conn.execute(
                """
                SELECT key, value, source, source_trust, confidence, entity_tags, agent_name
                FROM facts
                WHERE (key LIKE ? OR value LIKE ?) AND agent_name = ?
                ORDER BY confidence DESC, id DESC
                LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", agent_name, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT key, value, source, source_trust, confidence, entity_tags, agent_name
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

    def build_context(
        self,
        session_id: str,
        message: str | None = None,
        budget: int = 16000,
        include_history: bool = True,
    ) -> str:
        facts = self.get_profile_facts()[:10]
        fact_lines = [f"{row['key']}={row['value']}" for row in facts]
        sections = ["# Profile facts", *fact_lines]
        if include_history:
            recent = self.get_recent_messages(session_id, limit=20)
            recent_lines = [f"{row['role']}: {row['content']}" for row in recent]
            sections.extend(["# Recent messages", *recent_lines])
        if message is not None:
            sections.extend(["# Current input", message])
        context = "\n".join(sections)
        return context[:budget]

    def get_provider_session(
        self, app_session_id: str, provider: str, *, max_age_seconds: int = 7200,
    ) -> str | None:
        row = self._conn.execute(
            """
            SELECT provider_session_id, updated_at
            FROM provider_sessions
            WHERE app_session_id = ? AND provider = ?
            """,
            (app_session_id, provider),
        ).fetchone()
        if row is None:
            return None
        # Expire sessions older than max_age_seconds to avoid stale resume failures.
        if row["updated_at"] is not None:
            from datetime import datetime, timezone
            try:
                updated = datetime.fromisoformat(row["updated_at"]).replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated).total_seconds()
                if age > max_age_seconds:
                    self.clear_provider_session(app_session_id, provider)
                    return None
            except (ValueError, TypeError):
                pass
        return row["provider_session_id"]

    def link_provider_session(self, app_session_id: str, provider: str, provider_session_id: str) -> None:
        with self._lock:
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

    def clear_provider_session(self, app_session_id: str, provider: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM provider_sessions WHERE app_session_id = ? AND provider = ?",
                (app_session_id, provider),
            )
            self._conn.commit()

    # --- Cron state ---

    def load_cron_state(self) -> dict[str, tuple[float, int]]:
        rows = self._conn.execute("SELECT job_name, last_run_at, runs FROM cron_state").fetchall()
        return {row["job_name"]: (row["last_run_at"], row["runs"]) for row in rows}

    def save_cron_job(self, job_name: str, last_run_at: float, runs: int) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cron_state (job_name, last_run_at, runs)
                VALUES (?, ?, ?)
                ON CONFLICT(job_name)
                DO UPDATE SET last_run_at = excluded.last_run_at, runs = excluded.runs
                """,
                (job_name, last_run_at, runs),
            )
            self._conn.commit()

    # --- Semantic memory ---

    def store_fact_with_embedding(
        self,
        key: str,
        value: str,
        *,
        source: str,
        source_trust: str = "untrusted",
        confidence: float = 0.5,
        entity_tags: Iterable[str] = (),
        embed_fn: Callable[..., list[float]] | None = None,
    ) -> int:
        embedder = embed_fn or _simple_embedding
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO facts (key, value, source, source_trust, confidence, entity_tags)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key, value, source, source_trust, confidence, json.dumps(list(entity_tags))),
            )
            fact_id = cursor.lastrowid
            embedding = embedder(f"{key} {value}")
            self._conn.execute(
                "INSERT INTO fact_embeddings (fact_id, embedding) VALUES (?, ?)",
                (fact_id, json.dumps(embedding)),
            )
            self._conn.commit()
        return fact_id

    def search_facts_semantic(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.1,
        embed_fn: Callable[..., list[float]] | None = None,
    ) -> list[dict]:
        embedder = embed_fn or _simple_embedding
        query_vec = embedder(query)
        rows = self._conn.execute(
            """
            SELECT f.id, f.key, f.value, f.source, f.source_trust, f.confidence, fe.embedding
            FROM facts f
            JOIN fact_embeddings fe ON f.id = fe.fact_id
            """,
        ).fetchall()
        scored = []
        for row in rows:
            stored_vec = json.loads(row["embedding"])
            sim = _cosine_similarity(query_vec, stored_vec)
            if sim >= min_similarity:
                scored.append({
                    "key": row["key"], "value": row["value"],
                    "source": row["source"], "confidence": row["confidence"],
                    "similarity": round(sim, 4),
                })
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]

    def backfill_embeddings(self, embed_fn: Callable[..., list[float]] | None = None) -> int:
        embedder = embed_fn or _simple_embedding
        rows = self._conn.execute(
            "SELECT id, key, value FROM facts WHERE id NOT IN (SELECT fact_id FROM fact_embeddings)"
        ).fetchall()
        with self._lock:
            for row in rows:
                embedding = embedder(f"{row['key']} {row['value']}")
                self._conn.execute(
                    "INSERT OR IGNORE INTO fact_embeddings (fact_id, embedding) VALUES (?, ?)",
                    (row["id"], json.dumps(embedding)),
                )
            self._conn.commit()
        return len(rows)
