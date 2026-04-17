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
    trace_id TEXT,
    root_trace_id TEXT,
    span_id TEXT,
    parent_span_id TEXT,
    job_id TEXT,
    artifact_id TEXT,
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
        self._ensure_schema()

    def emit(
        self,
        event_type: str,
        *,
        lane: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        trace_id: str | None = None,
        root_trace_id: str | None = None,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        job_id: str | None = None,
        artifact_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO observe_stream (
                    event_type, lane, provider, model,
                    trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    lane,
                    provider,
                    model,
                    trace_id,
                    root_trace_id,
                    span_id,
                    parent_span_id,
                    job_id,
                    artifact_id,
                    json.dumps(payload or {}),
                ),
            )
            self._conn.commit()

    def _ensure_schema(self) -> None:
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(observe_stream)").fetchall()
        }
        for column in (
            "trace_id",
            "root_trace_id",
            "span_id",
            "parent_span_id",
            "job_id",
            "artifact_id",
        ):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE observe_stream ADD COLUMN {column} TEXT")
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

    def total_cost_today(self) -> float:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0)
                FROM observe_stream
                WHERE event_type = 'llm_response'
                  AND timestamp >= date('now', 'start of day')
                """,
            ).fetchone()
        return float(row[0]) if row else 0.0

    def cost_per_agent_today(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT COALESCE(
                           json_extract(payload, '$.agent_name'),
                           json_extract(payload, '$.sub_agent')
                       ) as agent,
                       COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0) as cost
                FROM observe_stream
                WHERE event_type = 'llm_decision'
                  AND timestamp >= date('now', 'start of day')
                GROUP BY agent
                """,
            ).fetchall()
        return {row[0]: row[1] for row in rows if row[0]}

    def spending_today(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT lane, provider, model,
                       COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0) as cost,
                       COUNT(*) as requests
                FROM observe_stream
                WHERE event_type = 'llm_decision'
                  AND timestamp >= date('now', 'start of day')
                GROUP BY lane, provider, model
                ORDER BY cost DESC
                """,
            ).fetchall()
        by_lane: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        by_model: dict[str, float] = {}
        rows_payload: list[dict] = []
        total = 0.0
        for lane, provider, model, cost, requests in rows:
            cost = float(cost or 0.0)
            total += cost
            lane_key = lane or "unknown"
            provider_key = provider or "unknown"
            model_key = model or "unknown"
            by_lane[lane_key] = by_lane.get(lane_key, 0.0) + cost
            by_provider[provider_key] = by_provider.get(provider_key, 0.0) + cost
            by_model[model_key] = by_model.get(model_key, 0.0) + cost
            rows_payload.append(
                {
                    "lane": lane_key,
                    "provider": provider_key,
                    "model": model_key,
                    "requests": int(requests or 0),
                    "cost": round(cost, 6),
                }
            )
        return {
            "total": round(total, 6),
            "by_lane": {key: round(value, 6) for key, value in sorted(by_lane.items())},
            "by_provider": {key: round(value, 6) for key, value in sorted(by_provider.items())},
            "by_model": {key: round(value, 6) for key, value in sorted(by_model.items())},
            "rows": rows_payload,
        }

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT event_type, lane, provider, model,
                       trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                       payload, timestamp
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
                "trace_id": row[4],
                "root_trace_id": row[5],
                "span_id": row[6],
                "parent_span_id": row[7],
                "job_id": row[8],
                "artifact_id": row[9],
                "payload": json.loads(row[10]),
                "timestamp": row[11],
            }
            for row in rows
        ]

    def trace_events(self, trace_id: str, *, limit: int | None = None) -> list[dict]:
        query = """
            SELECT event_type, lane, provider, model,
                   trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                   payload, timestamp
            FROM observe_stream
            WHERE trace_id = ?
            ORDER BY id ASC
        """
        params: tuple[object, ...] = (trace_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (trace_id, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "event_type": row[0],
                "lane": row[1],
                "provider": row[2],
                "model": row[3],
                "trace_id": row[4],
                "root_trace_id": row[5],
                "span_id": row[6],
                "parent_span_id": row[7],
                "job_id": row[8],
                "artifact_id": row[9],
                "payload": json.loads(row[10]),
                "timestamp": row[11],
            }
            for row in rows
        ]
