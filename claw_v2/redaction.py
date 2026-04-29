from __future__ import annotations

import re
from typing import Any

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)([?&](?:token|key|api_key|access_token|approval_token)=)[^&\s]+"),
    re.compile(r"(?i)approval_token['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?"),
    re.compile(r"/approve\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"),
    re.compile(r"/social_approve\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"),
    re.compile(r"/pipeline_merge_confirm\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"),
    re.compile(r"/pipeline_approve\s+[A-Za-z0-9_\-]+\s+[A-Za-z0-9_\-]+"),
    re.compile(r"xoxb-[A-Za-z0-9\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)(OPENAI|ANTHROPIC|GOOGLE|SLACK_BOT|LINEAR|HEYGEN|FIRECRAWL)_API_KEY\s*[:=]\s*\S+"),
    re.compile(r"(?i)(secret|password|api_key|access_token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?"),
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


def redact_text(text: str, *, limit: int = 2000) -> str:
    redacted = text
    for pattern in _PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    if limit and len(redacted) > limit:
        redacted = redacted[:limit] + "…[truncated]"
    return redacted


def redact_sensitive(value: Any, *, limit: int = 2000) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return redact_text(value, limit=limit)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and key.lower() in _REDACTED_FIELDS and isinstance(val, str) and val:
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_sensitive(val, limit=limit)
        return out
    if isinstance(value, list):
        return [redact_sensitive(item, limit=limit) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item, limit=limit) for item in value)
    return value
