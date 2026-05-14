"""Wave 3.5: defense-in-depth scrubber for system-reminder leak markers.

The chat output sanitizer in bot_helpers handles the *response* path. This
module provides the same scrubbing as a free function so the *audit* path
(observe.emit) and *memory* path (store_message) can apply it before
persisting. Three layers, one regex set — a leak that bypasses one path
still gets caught by the others.
"""
from __future__ import annotations

import re
from typing import Any


_SYSTEM_REMINDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"</?\s*system-reminder\s*>", re.IGNORECASE),
    re.compile(r"&lt;/?\s*system-reminder\s*&gt;", re.IGNORECASE),
)

_REDACTION = "[redacted: system-reminder]"


def redact_system_reminders(value: str) -> str:
    """Replace ``<system-reminder>`` / ``</system-reminder>`` markers (and
    their HTML-entity-encoded variants) with a stable redaction tag.
    Idempotent — running twice is a no-op."""
    redacted = value
    for pattern in _SYSTEM_REMINDER_PATTERNS:
        redacted = pattern.sub(_REDACTION, redacted)
    return redacted


def scrub_for_persistence(value: Any) -> Any:
    """Walk a payload (str / dict / list / tuple) and redact system-reminder
    markers in any string. Non-stringy types pass through. Use this in
    paths that persist user-or-llm-derived content (audit, memory)."""
    if isinstance(value, str):
        return redact_system_reminders(value)
    if isinstance(value, dict):
        return {k: scrub_for_persistence(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_for_persistence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_for_persistence(item) for item in value)
    return value
