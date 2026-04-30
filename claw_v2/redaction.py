from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{30,}\b"), "<REDACTED:telegram_token>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{20,}"), "[REDACTED]"),
    (re.compile(r"ghp_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED]"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"), "[REDACTED]"),
    (re.compile(r"(?i)([?&](?:token|key|api_key|access_token|approval_token)=)[^&\s]+"), "[REDACTED]"),
    (re.compile(r"(?i)approval_token['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?"), "[REDACTED]"),
    (re.compile(r"/approve\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"/social_approve\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"/pipeline_merge_confirm\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"/pipeline_approve\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"xoxb-[A-Za-z0-9\-]{20,}"), "[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),
    (re.compile(r"(?i)(OPENAI|ANTHROPIC|GOOGLE|SLACK_BOT|LINEAR|HEYGEN|FIRECRAWL)_API_KEY\s*[:=]\s*\S+"), "[REDACTED]"),
    (re.compile(r"(?i)(secret|password|api_key|access_token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?"), "[REDACTED]"),
)

_REDACTED_FIELDS = frozenset({
    "approval_token",
    "token",
    "secret",
    "password",
    "api_key",
    "access_token",
    "bearer",
})

_REDACTED_FIELD_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "access_token",
    "authorization",
    "credential",
    "cookie",
)


def redact_text(text: str, *, limit: int = 2000) -> str:
    redacted = text
    for pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    if limit and len(redacted) > limit:
        redacted = redacted[:limit] + "…[truncated]"
    return redacted


def redact_sensitive(value: Any, *, limit: int = 2000) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return redact_text(value, limit=limit)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if val is None:
                out[key] = None
                continue
            if isinstance(key, str) and _should_redact_field(key, val):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_sensitive(val, limit=limit)
        return out
    if isinstance(value, list):
        return [redact_sensitive(item, limit=limit) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item, limit=limit) for item in value)
    return value


def _should_redact_field(key: str, value: Any) -> bool:
    lowered = key.lower()
    if lowered in _REDACTED_FIELDS:
        return isinstance(value, str) and bool(value)
    if any(fragment in lowered for fragment in _REDACTED_FIELD_FRAGMENTS):
        return isinstance(value, str) and bool(value)
    if "key" in lowered and isinstance(value, str) and len(value) >= 8:
        return True
    return False
