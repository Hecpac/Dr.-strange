from __future__ import annotations

import json
import re


def _try_parse_json_object(content: str) -> dict | None:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    for candidate in (stripped, _first_json_object(stripped)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _first_json_object(content: str) -> str | None:
    start = content.find("{")
    if start == -1:
        return None
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(content, start)
        return json.dumps(obj)
    except json.JSONDecodeError:
        return None


def _normalize_recommendation(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"approve", "approved", "allow", "proceed"}:
        return "approve"
    if text in {"needs_approval", "needs approval", "review", "manual_review", "manual review"}:
        return "needs_approval"
    if text in {"deny", "denied", "reject", "block"}:
        return "deny"
    return "needs_approval"


def _normalize_risk_level(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high", "critical"}:
        return text
    return "medium"


def _as_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _clamp_confidence(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric < 0.0:
        return 0.0
    if numeric > 1.0:
        return 1.0
    return numeric


def _strip_trace_tags(content: str) -> str:
    content = re.sub(
        r"<(?:trace|thinking)>.*?</(?:trace|thinking)>\s*",
        "",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    content = re.sub(r"</?response>\s*", "", content, flags=re.IGNORECASE)
    return content.strip()


def _validate_schema_keys(data: dict, schema: dict) -> list[str]:
    errors: list[str] = []
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    for key in required:
        if key not in data:
            errors.append(f"missing required key: {key}")
    for key, prop_schema in properties.items():
        if key not in data:
            continue
        expected = prop_schema.get("type")
        value = data[key]
        if expected == "string" and not isinstance(value, str):
            errors.append(f"{key}: expected string")
        elif expected == "integer" and not isinstance(value, int):
            errors.append(f"{key}: expected integer")
        elif expected == "number" and not isinstance(value, (int, float)):
            errors.append(f"{key}: expected number")
        elif expected == "boolean" and not isinstance(value, bool):
            errors.append(f"{key}: expected boolean")
        elif expected == "array" and not isinstance(value, list):
            errors.append(f"{key}: expected array")
        elif expected == "object" and not isinstance(value, dict):
            errors.append(f"{key}: expected object")
    return errors
