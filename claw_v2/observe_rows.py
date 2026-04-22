from __future__ import annotations

import json


def events_from_rows(rows: list[tuple]) -> list[dict]:
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


def spending_payload(rows: list[tuple]) -> dict:
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
