from __future__ import annotations

import json
import sqlite3
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
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(OBSERVE_SCHEMA)

    def emit(self, event_type: str, *, lane: str | None = None, provider: str | None = None, model: str | None = None, payload: dict | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO observe_stream (event_type, lane, provider, model, payload)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_type, lane, provider, model, json.dumps(payload or {})),
        )
        self._conn.commit()

    def recent_events(self, limit: int = 20) -> list[dict]:
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
