from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path


OBSERVE_SCHEMA = """
CREATE TABLE IF NOT EXISTS observe_stream (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    lane TEXT,
    provider TEXT,
    model TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
"""


class ObserveStream:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.executescript(OBSERVE_SCHEMA)
        self._lock = threading.Lock()

    def emit(self, event_type: str, *, lane: str | None = None, provider: str | None = None, model: str | None = None, payload: dict | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO observe_stream (event_type, lane, provider, model, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, lane, provider, model, json.dumps(payload or {})),
            )
            self._conn.commit()

    def cache_summary(self, hours: int = 24) -> dict:
        """Return prompt cache stats for the last N hours."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT payload
                FROM observe_stream
                WHERE event_type = 'prompt_cache'
                  AND timestamp > datetime('now', ?)
                """,
                (f"-{hours} hours",),
            ).fetchall()
        total_input = 0
        total_cache_read = 0
        total_cache_create = 0
        for (raw,) in rows:
            data = json.loads(raw)
            total_input += data.get("input_tokens", 0)
            total_cache_read += data.get("cache_read_tokens", 0)
            total_cache_create += data.get("cache_create_tokens", 0)
        hit_ratio = total_cache_read / max(total_input, 1) if total_input else 0.0
        estimated_savings_pct = round(hit_ratio * 75, 1)
        return {
            "requests": len(rows),
            "total_input_tokens": total_input,
            "cache_read_tokens": total_cache_read,
            "cache_create_tokens": total_cache_create,
            "hit_ratio": round(hit_ratio, 3),
            "estimated_savings_pct": estimated_savings_pct,
        }

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_type, lane, provider, model, payload, timestamp
                FROM observe_stream
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "event_type": row[0],
                "lane": row[1],
                "provider": row[2],
                "model": row[3],
                "payload": json.loads(row[4]),
                "timestamp": row[5],
            }
            for row in rows
        ]
