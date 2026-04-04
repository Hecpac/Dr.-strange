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
    last_message_id INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS task_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    task_id TEXT NOT NULL,
    description TEXT NOT NULL,
    approach TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failure', 'partial')),
    lesson TEXT NOT NULL,
    error_snippet TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


_ST_MODEL = None
_ST_LOCK = threading.Lock()


def _get_st_model():
    """Lazy-load sentence-transformers model on first use."""
    global _ST_MODEL
    if _ST_MODEL is None:
        with _ST_LOCK:
            if _ST_MODEL is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    _ST_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
                except Exception:
                    _ST_MODEL = False  # Mark as unavailable
    return _ST_MODEL if _ST_MODEL is not False else None


def _simple_embedding(text: str, dim: int = 128) -> list[float]:
    """Semantic embedding via sentence-transformers, with bag-of-chars fallback."""
    model = _get_st_model()
    if model is not None:
        vec = model.encode(text, normalize_embeddings=True).tolist()
        return vec
    # Fallback: lightweight bag-of-chars embedding.
    vec = [0.0] * dim
    for i, ch in enumerate(text.lower()):
        vec[ord(ch) % dim] += 1.0 / (1 + i * 0.01)
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


_MIGRATION_ADD_AGENT_NAME = """
ALTER TABLE facts ADD COLUMN agent_name TEXT NOT NULL DEFAULT 'system';
"""

_MIGRATION_ADD_OUTCOME_FEEDBACK = """
ALTER TABLE task_outcomes ADD COLUMN feedback TEXT;
"""

_MIGRATION_ADD_OUTCOME_TAGS = """
ALTER TABLE task_outcomes ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
"""

_MIGRATION_ADD_PROVIDER_LAST_MESSAGE_ID = """
ALTER TABLE provider_sessions ADD COLUMN last_message_id INTEGER NOT NULL DEFAULT 0;
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
        cursor = self._conn.execute("PRAGMA table_info(task_outcomes)")
        outcome_cols = {row[1] for row in cursor.fetchall()}
        for col, sql in [("feedback", _MIGRATION_ADD_OUTCOME_FEEDBACK), ("tags", _MIGRATION_ADD_OUTCOME_TAGS)]:
            if col not in outcome_cols:
                try:
                    self._conn.execute(sql)
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass
        cursor = self._conn.execute("PRAGMA table_info(provider_sessions)")
        provider_cols = {row[1] for row in cursor.fetchall()}
        if "last_message_id" not in provider_cols:
            try:
                self._conn.execute(_MIGRATION_ADD_PROVIDER_LAST_MESSAGE_ID)
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

    def get_messages_since(self, session_id: str, after_id: int, limit: int | None = None) -> list[dict]:
        sql = [
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE session_id = ? AND id > ?
            ORDER BY id ASC
            """
        ]
        params: list[object] = [session_id, after_id]
        if limit is not None:
            sql.append("LIMIT ?")
            params.append(limit)
        rows = self._conn.execute("\n".join(sql), params).fetchall()
        return [dict(row) for row in rows]

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

    def last_message_id(self, session_id: str) -> int:
        row = self._conn.execute(
            """
            SELECT MAX(id) AS max_id
            FROM messages
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        return int(row["max_id"] or 0) if row else 0

    def replace_latest_assistant_message(self, session_id: str, previous_content: str, new_content: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, content
                FROM messages
                WHERE session_id = ? AND role = 'assistant'
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None or row["content"] != previous_content:
                return False
            self._conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (new_content, row["id"]),
            )
            self._conn.commit()
            return True

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

    def get_learning_facts(self, limit: int = 3) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT key, value, source, confidence
            FROM facts
            WHERE key = 'learning_loop_consolidated' OR entity_tags LIKE '%"learning"%'
            ORDER BY confidence DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def build_context(
        self,
        session_id: str,
        message: str | None = None,
        budget: int = 64000,
        include_history: bool = True,
    ) -> str:
        # Current input goes FIRST — never truncate what the user just said.
        sections: list[str] = []
        if message is not None:
            sections.extend(["# Current input", message])

        facts = self.get_profile_facts()[:20]
        if facts:
            fact_lines = [f"{row['key']}={row['value']}" for row in facts]
            sections.extend(["# Profile facts", *fact_lines])

        learning_facts = self.get_learning_facts(limit=5)
        if learning_facts:
            learning_lines = [row["value"] for row in learning_facts if row.get("value")]
            if learning_lines:
                sections.extend(["# Learning rules", *learning_lines])

        if include_history:
            recent = self.get_recent_messages(session_id, limit=20)
            if recent:
                current_len = sum(len(s) for s in sections) + len(sections)
                remaining = budget - current_len
                # Always preserve first message (foundational context) and newest messages.
                # Sacrifice middle messages when budget is tight.
                first_line = f"{recent[0]['role']}: {recent[0]['content']}"
                rest = recent[1:]
                recent_lines: list[str] = []
                # Reserve space for the foundational first message.
                first_cost = len(first_line) + 1
                if first_cost < remaining:
                    remaining -= first_cost
                    recent_lines.append(first_line)
                    # Fill from newest backwards with the remaining budget.
                    tail_lines: list[str] = []
                    skipped = 0
                    for row in reversed(rest):
                        line = f"{row['role']}: {row['content']}"
                        if remaining - len(line) - 1 < 0:
                            skipped += 1
                            continue
                        tail_lines.insert(0, line)
                        remaining -= len(line) + 1
                    if skipped:
                        recent_lines.append(f"[... {skipped} mensajes intermedios omitidos ...]")
                    recent_lines.extend(tail_lines)
                else:
                    # Budget too small even for first message — fill newest only.
                    for row in reversed(recent):
                        line = f"{row['role']}: {row['content']}"
                        if remaining - len(line) - 1 < 0:
                            break
                        recent_lines.insert(0, line)
                        remaining -= len(line) + 1
                if recent_lines:
                    sections.extend(["# Recent messages", *recent_lines])

        return "\n".join(sections)

    def _provider_session_row(
        self, app_session_id: str, provider: str, *, max_age_seconds: int = 7200,
    ) -> sqlite3.Row | None:
        row = self._conn.execute(
            """
            SELECT provider_session_id, last_message_id, updated_at
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
        return row

    def get_provider_session(
        self, app_session_id: str, provider: str, *, max_age_seconds: int = 7200,
    ) -> str | None:
        row = self._provider_session_row(app_session_id, provider, max_age_seconds=max_age_seconds)
        return row["provider_session_id"] if row else None

    def get_provider_session_cursor(
        self, app_session_id: str, provider: str, *, max_age_seconds: int = 7200,
    ) -> int | None:
        row = self._provider_session_row(app_session_id, provider, max_age_seconds=max_age_seconds)
        return int(row["last_message_id"] or 0) if row else None

    def link_provider_session(
        self,
        app_session_id: str,
        provider: str,
        provider_session_id: str,
        *,
        last_message_id: int | None = None,
    ) -> None:
        if last_message_id is None:
            last_message_id = self.last_message_id(app_session_id)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_sessions (app_session_id, provider, provider_session_id, last_message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_session_id, provider)
                DO UPDATE SET
                    provider_session_id = excluded.provider_session_id,
                    last_message_id = excluded.last_message_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (app_session_id, provider, provider_session_id, last_message_id),
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
        query_dim = len(query_vec)
        rows = self._conn.execute(
            """
            SELECT f.id, f.key, f.value, f.source, f.source_trust, f.confidence, fe.embedding
            FROM facts f
            JOIN fact_embeddings fe ON f.id = fe.fact_id
            """,
        ).fetchall()
        scored = []
        stale_ids: list[tuple[int, str, str]] = []
        for row in rows:
            stored_vec = json.loads(row["embedding"])
            if len(stored_vec) != query_dim:
                # Dimension mismatch — re-embed on the fly and queue for update.
                stored_vec = embedder(f"{row['key']} {row['value']}")
                stale_ids.append((row["id"], row["key"], row["value"]))
            sim = _cosine_similarity(query_vec, stored_vec)
            if sim >= min_similarity:
                scored.append({
                    "key": row["key"], "value": row["value"],
                    "source": row["source"], "confidence": row["confidence"],
                    "similarity": round(sim, 4),
                })
        # Lazily migrate stale embeddings in the background.
        if stale_ids:
            with self._lock:
                for fact_id, key, value in stale_ids:
                    new_vec = embedder(f"{key} {value}")
                    self._conn.execute(
                        "UPDATE fact_embeddings SET embedding = ? WHERE fact_id = ?",
                        (json.dumps(new_vec), fact_id),
                    )
                self._conn.commit()
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

    # --- Learning loop ---

    def store_task_outcome(
        self,
        *,
        task_type: str,
        task_id: str,
        description: str,
        approach: str,
        outcome: str,
        lesson: str,
        error_snippet: str | None = None,
        retries: int = 0,
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO task_outcomes
                    (task_type, task_id, description, approach, outcome, lesson, error_snippet, retries)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_type, task_id, description, approach, outcome, lesson, error_snippet, retries),
            )
            self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def search_past_outcomes(
        self, query: str, *, task_type: str | None = None, limit: int = 5,
    ) -> list[dict]:
        if task_type:
            rows = self._conn.execute(
                """
                SELECT task_type, task_id, description, approach, outcome, lesson, error_snippet, retries, created_at, feedback
                FROM task_outcomes
                WHERE task_type = ? AND (description LIKE ? OR lesson LIKE ? OR approach LIKE ?)
                ORDER BY id DESC LIMIT ?
                """,
                (task_type, f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT task_type, task_id, description, approach, outcome, lesson, error_snippet, retries, created_at, feedback
                FROM task_outcomes
                WHERE description LIKE ? OR lesson LIKE ? OR approach LIKE ?
                ORDER BY id DESC LIMIT ?
                """,
                (f"%{query}%", f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_failures(self, *, task_type: str | None = None, limit: int = 5) -> list[dict]:
        if task_type:
            rows = self._conn.execute(
                """
                SELECT task_type, task_id, description, approach, outcome, lesson, error_snippet, retries, created_at, feedback
                FROM task_outcomes WHERE outcome = 'failure' AND task_type = ?
                ORDER BY id DESC LIMIT ?
                """,
                (task_type, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT task_type, task_id, description, approach, outcome, lesson, error_snippet, retries, created_at, feedback
                FROM task_outcomes WHERE outcome = 'failure'
                ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Learning loop: feedback & retrieval helpers ---

    def get_outcome(self, outcome_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM task_outcomes WHERE id = ?", (outcome_id,),
        ).fetchone()
        return dict(row) if row else None

    def update_outcome_feedback(self, outcome_id: int, feedback: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE task_outcomes SET feedback = ? WHERE id = ?",
                (feedback, outcome_id),
            )
            self._conn.commit()

    def last_outcome_id(self) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM task_outcomes ORDER BY id DESC LIMIT 1",
        ).fetchone()
        return row["id"] if row else None

    def outcomes_without_feedback(self, *, limit: int = 10) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT id, task_type, task_id, description, outcome, lesson, created_at
            FROM task_outcomes WHERE feedback IS NULL
            ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
