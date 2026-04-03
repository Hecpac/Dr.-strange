from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass

from claw_v2.types import AgentClass, SanitizedContent


SUSPICIOUS_PATTERNS = (
    "ignore previous instructions",
    "ignore all instructions",
    "disregard previous",
    "system prompt",
    "developer message",
    "<tool",
    "tool_call",
    "sudo ",
    "rm -rf",
    "role: system",
    "role:system",
)


@dataclass(slots=True)
class QuarantinedExtraction:
    source_url: str | None
    content_type: str
    numeric_data: dict[str, float]
    entity_names: list[str]
    dates: list[str]
    word_count: int
    quarantine_reason: str


def sanitize(content: str, source: str, target_agent_class: AgentClass) -> SanitizedContent:
    lowered = content.lower()
    matches = [pattern for pattern in SUSPICIOUS_PATTERNS if pattern in lowered]
    if len(matches) >= 2:
        return SanitizedContent(
            verdict="malicious",
            content="",
            source=source,
            target_agent_class=target_agent_class,
            reason="multiple suspicious patterns detected",
        )
    if len(matches) == 1:
        extraction = extract_structured(content, source_url=None, reason=matches[0])
        return SanitizedContent(
            verdict="unsure",
            content=f"[EXTERNAL:{source}] quarantined",
            source=source,
            target_agent_class=target_agent_class,
            reason=matches[0],
            structured_data=asdict(extraction),
        )
    cleaned = re.sub(r"\s+", " ", content).strip()
    return SanitizedContent(
        verdict="clean",
        content=f"[EXTERNAL:{source}] {cleaned}",
        source=source,
        target_agent_class=target_agent_class,
    )


def extract_structured(content: str, *, source_url: str | None, reason: str) -> QuarantinedExtraction:
    numbers = {
        f"value_{idx}": float(match)
        for idx, match in enumerate(re.findall(r"\b\d+(?:\.\d+)?\b", content)[:10], start=1)
    }
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", content)
    raw_entities = re.findall(r"\b[A-Z][a-zA-Z0-9\.-]{1,49}\b", content)
    most_common = [name for name, _ in Counter(raw_entities).most_common(20)]
    category = "unknown"
    lowered = content.lower()
    if "http" in lowered or "www." in lowered:
        category = "landing_page"
    elif "api" in lowered or "json" in lowered:
        category = "api_response"
    elif "email" in lowered or "subject:" in lowered:
        category = "email_thread"
    elif "docs" in lowered or "documentation" in lowered:
        category = "documentation"
    return QuarantinedExtraction(
        source_url=source_url,
        content_type=category,
        numeric_data=numbers,
        entity_names=most_common,
        dates=dates,
        word_count=len(content.split()),
        quarantine_reason=reason,
    )
