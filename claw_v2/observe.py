from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Callable

from claw_v2.sqlite_runtime import connect_runtime_sqlite
from claw_v2.turn_context import (
    CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID,
    current_turn_id,
)

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], None]


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

OBSERVE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_observe_stream_event_time
    ON observe_stream(event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_observe_stream_trace_id_id
    ON observe_stream(trace_id, id);
CREATE INDEX IF NOT EXISTS idx_observe_stream_job_id_id
    ON observe_stream(job_id, id);
CREATE INDEX IF NOT EXISTS idx_observe_stream_root_trace_id_id
    ON observe_stream(root_trace_id, id);
CREATE INDEX IF NOT EXISTS idx_observe_stream_turn_id
    ON observe_stream(json_extract(payload, '$.turn_id'));
CREATE INDEX IF NOT EXISTS idx_observe_stream_timestamp
    ON observe_stream(timestamp);
"""

# Retention for the immutable event log. The table grows ~3+ rows/min when
# idle and dozens per active turn; without pruning the per-turn receipt
# lookup and every timestamp-window query degrade linearly forever.
OBSERVE_RETENTION_DAYS = 30
OBSERVE_PRUNE_MAX_ROWS = 20_000


class ObserveStream:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = connect_runtime_sqlite(self.db_path, row_factory=False)
        self._conn.executescript(OBSERVE_SCHEMA)
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[EventCallback]] = {}
        self._ensure_schema()

    def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Register a callback to fire whenever `event_type` is emitted.

        Callbacks must be cheap and non-blocking; long work belongs in a
        consumer thread. Exceptions inside callbacks are logged and
        swallowed so they cannot break the emit path.
        """
        self._subscribers.setdefault(event_type, []).append(callback)

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
        # Wave 3.5: defense-in-depth — scrub system-reminder markers from the
        # payload BEFORE we persist or fan out to subscribers, so a leak that
        # bypassed the chat sanitizer still doesn't end up on disk.
        from claw_v2.leak_scrub import scrub_for_persistence
        from claw_v2.redaction import redact_sensitive

        clean_payload = redact_sensitive(scrub_for_persistence(payload or {}), limit=0)
        # P0-B: stamp the active turn_id (if any) on every persisted payload so
        # behavior receipts can join observe, task ledger, and approval rows by
        # one column instead of fragile timestamp windows. When a critical
        # event fires WITHOUT a turn_id context, emit a sibling
        # ``turn_id_missing`` so the gap is visible.
        active_turn_id = current_turn_id()
        if isinstance(clean_payload, dict):
            if active_turn_id and "turn_id" not in clean_payload:
                clean_payload["turn_id"] = active_turn_id
            emit_turn_id_missing = (
                event_type in CRITICAL_OBSERVE_EVENTS_REQUIRING_TURN_ID
                and "turn_id" not in clean_payload
                and event_type != "turn_id_missing"
            )
        else:
            emit_turn_id_missing = False
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
                    json.dumps(clean_payload),
                ),
            )
            self._conn.commit()
        callbacks = self._subscribers.get(event_type)
        if callbacks:
            event_payload = clean_payload
            for cb in callbacks:
                try:
                    cb(event_payload)
                except Exception:
                    logger.exception("observe subscriber for %s failed", event_type)
        if emit_turn_id_missing:
            # Recurse with a sentinel payload; the early `event_type !=
            # "turn_id_missing"` guard above prevents infinite recursion.
            self.emit(
                "turn_id_missing",
                payload={"origin_event": event_type},
            )

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
        self._conn.executescript(OBSERVE_INDEXES)
        self._conn.commit()

    def prune(
        self,
        *,
        retention_days: int = OBSERVE_RETENTION_DAYS,
        max_rows: int = OBSERVE_PRUNE_MAX_ROWS,
    ) -> int:
        """Delete events older than the retention window, bounded per call.

        The LIMIT keeps each sweep short so the scheduler tick that runs it
        never stalls; a backlog drains across consecutive runs.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM observe_stream
                WHERE id IN (
                    SELECT id FROM observe_stream
                    WHERE timestamp < datetime('now', ?)
                    ORDER BY id
                    LIMIT ?
                )
                """,
                (f"-{int(retention_days)} days", int(max_rows)),
            )
            self._conn.commit()
            return int(cursor.rowcount or 0)

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

    def total_cost_today(self, *, providers: set[str] | None = None) -> float:
        provider_filter = ""
        params: tuple[object, ...] = ()
        if providers is not None:
            normalized = sorted(provider for provider in providers if provider)
            if not normalized:
                return 0.0
            placeholders = ",".join("?" for _ in normalized)
            provider_filter = f" AND provider IN ({placeholders})"
            params = tuple(normalized)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT COALESCE(SUM(json_extract(payload, '$.cost_estimate')), 0.0)
                FROM observe_stream
                WHERE event_type = 'llm_response'
                  AND timestamp >= date('now', 'start of day')
                  {provider_filter}
                """,
                params,
            ).fetchone()
        return float(row[0]) if row else 0.0

    def has_unknown_billable_cost_today(self, *, providers: set[str] | None = None) -> bool:
        """True if any billable LLM call today had an unpriced (cost_unknown) model.

        Lets the daily cost gate fail closed on unmetered billable spend instead
        of treating cost_unknown as zero (2026-05-31 audit H5).
        """
        provider_filter = ""
        params: tuple[object, ...] = ()
        if providers is not None:
            normalized = sorted(provider for provider in providers if provider)
            if not normalized:
                return False
            placeholders = ",".join("?" for _ in normalized)
            provider_filter = f" AND provider IN ({placeholders})"
            params = tuple(normalized)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT 1
                FROM observe_stream
                WHERE event_type = 'llm_response'
                  AND timestamp >= date('now', 'start of day')
                  AND json_extract(payload, '$.cost_unknown') = 1
                  {provider_filter}
                LIMIT 1
                """,
                params,
            ).fetchone()
        return row is not None

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

    def recent_events(self, limit: int = 20, *, event_type: str | None = None) -> list[dict]:
        query = """
            SELECT event_type, lane, provider, model,
                   trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                   payload, timestamp
            FROM observe_stream
        """
        params: tuple[object, ...]
        if event_type:
            query += " WHERE event_type = ?\n            ORDER BY id DESC\n            LIMIT ?"
            params = (event_type, limit)
        else:
            query += " ORDER BY id DESC\n            LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [
            _event_row_to_dict(row)
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
            _event_row_to_dict(row)
            for row in rows
        ]

    def job_events(self, job_id: str, *, limit: int | None = None) -> list[dict]:
        query = """
            SELECT event_type, lane, provider, model,
                   trace_id, root_trace_id, span_id, parent_span_id, job_id, artifact_id,
                   payload, timestamp
            FROM observe_stream
            WHERE job_id = ?
            ORDER BY id ASC
        """
        params: tuple[object, ...] = (job_id,)
        if limit is not None:
            query += " LIMIT ?"
            params = (job_id, limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_event_row_to_dict(row) for row in rows]


def _event_row_to_dict(row: sqlite3.Row | tuple) -> dict:
    return {
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
