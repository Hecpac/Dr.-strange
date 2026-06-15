"""behavior_turn_receipt: structured per-turn summary emitted at the end
of ``BotService.handle_text``.

A receipt is a single JSON-shaped observe event tagged with the active
``turn_id`` that summarises everything the agent did during the turn:
intent classification, tools invoked, approvals requested, ledger row
created (if any) and its verification status, plus a hash of the user
text (never the literal content, per privacy rules).

Builders here are pure functions over the persisted record so they are
easy to unit-test without spinning up the full BotService.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterable

USER_TEXT_HASH_LEN = 16


def hash_user_text(text: str) -> str:
    """Return a short hex digest of ``text``; never store the literal user
    content in the receipt for privacy."""
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()[
        :USER_TEXT_HASH_LEN
    ]


def _row_event_type_and_payload(row: Any) -> tuple[str, dict[str, Any]]:
    """Normalize a row from observe_stream to ``(event_type, payload_dict)``.

    Supports three shapes:
      - ``(event_type, payload_json_str)`` tuple/list (sqlite3 default)
      - ``sqlite3.Row`` with named columns
      - ``{"event_type": ..., "payload": dict | str}`` plain dict
    """
    event_type = ""
    payload_raw: Any = None
    if isinstance(row, dict):
        event_type = str(row.get("event_type", "") or "")
        payload_raw = row.get("payload")
    elif hasattr(row, "keys"):
        event_type = str(row["event_type"]) if "event_type" in row.keys() else ""
        payload_raw = row["payload"] if "payload" in row.keys() else None
    else:
        try:
            event_type = str(row[0])
            payload_raw = row[1] if len(row) > 1 else None
        except Exception:
            event_type = ""
            payload_raw = None
    if isinstance(payload_raw, str):
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
    elif isinstance(payload_raw, dict):
        payload = dict(payload_raw)
    else:
        payload = {}
    return event_type, payload


def aggregate_observe_events(
    rows: Iterable[Any],
) -> dict[str, Any]:
    """Reduce a list of observe_stream rows (already filtered by
    ``turn_id``) into the receipt's tool / approval / intent buckets.

    Each row may be a ``sqlite3.Row``, a tuple ``(event_type, payload)``
    or a dict. Unknown event types are tolerated and ignored.
    """
    intents: list[str] = []
    handlers: list[str] = []
    tools: list[str] = []
    tool_failures: list[str] = []
    approvals: list[str] = []
    task_ids: set[str] = set()
    cost_estimate = 0.0

    for row in rows:
        event_type, payload = _row_event_type_and_payload(row)
        if event_type == "dispatch_decision":
            # F0.3c: a turn now emits ONE consolidated dispatch_decision whose
            # tried_handlers[] array is canonical. Collect every captured
            # handler from it; fall back to the legacy single-handler shape
            # for events emitted before consolidation.
            tried = payload.get("tried_handlers")
            if isinstance(tried, list):
                for entry in tried:
                    if not isinstance(entry, dict) or entry.get("captured") is not True:
                        continue
                    handler = str(entry.get("handler") or "")
                    if handler:
                        handlers.append(handler)
            else:
                handler = str(payload.get("handler") or "")
                if handler and payload.get("captured") is True:
                    handlers.append(handler)
        elif event_type == "semantic_turn_trace":
            intent = str(payload.get("semantic_intent") or "")
            if intent:
                intents.append(intent)
        elif event_type in {"sdk_post_tool_use", "action_executed"}:
            tool = str(payload.get("tool_name") or payload.get("tool") or "")
            if tool:
                tools.append(tool)
        elif event_type == "sdk_post_tool_use_failure":
            tool = str(payload.get("tool_name") or "")
            if tool:
                tool_failures.append(tool)
        elif event_type in {"approval_pending", "critical_action_verification"}:
            action = str(payload.get("action") or payload.get("recommendation") or "")
            if action:
                approvals.append(action)
        elif event_type == "task_ledger_created":
            task_id = str(payload.get("task_id") or "")
            if task_id:
                task_ids.add(task_id)
        elif event_type == "llm_response":
            try:
                cost_estimate += float(payload.get("cost_estimate") or 0.0)
            except Exception:
                pass

    return {
        "intents": intents,
        "handlers_matched": handlers,
        "tools": tools,
        "tool_failures": tool_failures,
        "approvals_requested": approvals,
        "task_ids": sorted(task_ids),
        "cost_estimate": round(cost_estimate, 6),
    }


def build_turn_receipt_payload(
    *,
    turn_id: str,
    session_id: str,
    user_text: str,
    started_at: float,
    completed_at: float,
    observe_rows: Iterable[Any],
    ledger_record: Any | None = None,
    learning_outcome: str | None = None,
) -> dict[str, Any]:
    """Build the payload dict that will be emitted as a ``turn_receipt``
    observe event. Pure function; no I/O.
    """
    aggregated = aggregate_observe_events(observe_rows)
    payload: dict[str, Any] = {
        "turn_id": turn_id,
        "session_id": session_id,
        "user_text_hash": hash_user_text(user_text),
        "user_text_length": len(user_text or ""),
        "started_at": float(started_at),
        "completed_at": float(completed_at),
        "duration_ms": max(0, int((completed_at - started_at) * 1000)),
        "intent": aggregated["intents"][0] if aggregated["intents"] else "",
        "handlers_matched": aggregated["handlers_matched"],
        "tools_used": sorted(set(aggregated["tools"])),
        "tools_invoked_count": len(aggregated["tools"]),
        "tool_failures": sorted(set(aggregated["tool_failures"])),
        "approvals_requested": aggregated["approvals_requested"],
        "ledger_task_ids": aggregated["task_ids"],
        "cost_estimate": aggregated["cost_estimate"],
    }
    if ledger_record is not None:
        payload["ledger_status"] = str(getattr(ledger_record, "status", "") or "")
        payload["verification_status"] = str(
            getattr(ledger_record, "verification_status", "") or ""
        )
        payload["evidence_manifest_present"] = bool(
            (getattr(ledger_record, "artifacts", {}) or {}).get("evidence_manifest")
        )
    if learning_outcome is not None:
        payload["learning_outcome"] = str(learning_outcome)
    return payload


def emit_turn_receipt(
    observe: Any,
    *,
    turn_id: str,
    session_id: str,
    user_text: str,
    started_at: float,
    completed_at: float | None = None,
    ledger_record: Any | None = None,
    learning_outcome: str | None = None,
) -> dict[str, Any]:
    """Emit a ``turn_receipt`` observe event by querying observe_stream
    for events tagged with ``turn_id`` (already persisted during this
    turn) and aggregating them.

    Returns the payload that was emitted so the caller can also inspect
    it directly (used in tests + structured logging).
    """
    completed = float(completed_at) if completed_at is not None else time.time()
    rows: list[Any] = []
    conn = getattr(observe, "_conn", None)
    lock = getattr(observe, "_lock", None)
    if conn is not None:
        try:
            # Share the ObserveStream lock so the SELECT does not collide
            # with a concurrent emit/INSERT on the same connection (which
            # surfaces as "database is locked" from background threads).
            if lock is not None:
                with lock:
                    rows = conn.execute(
                        """
                        SELECT event_type, payload
                        FROM observe_stream
                        WHERE json_extract(payload, '$.turn_id') = ?
                        """,
                        (turn_id,),
                    ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_type, payload
                    FROM observe_stream
                    WHERE json_extract(payload, '$.turn_id') = ?
                    """,
                    (turn_id,),
                ).fetchall()
        except Exception:
            rows = []
    payload = build_turn_receipt_payload(
        turn_id=turn_id,
        session_id=session_id,
        user_text=user_text,
        started_at=started_at,
        completed_at=completed,
        observe_rows=rows,
        ledger_record=ledger_record,
        learning_outcome=learning_outcome,
    )
    try:
        observe.emit("turn_receipt", payload=payload)
    except Exception:
        pass
    return payload
