from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from claw_v2.types import AgentClass, SanitizedContent


MAX_SCAN_BYTES = 256 * 1024

ZERO_WIDTH_RE = re.compile(r"[​‌‍⁠﻿]")
HTML_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
HTML_INVISIBLE_TAG_RE = re.compile(
    r"<(\w+)[^>]*\b(?:style\s*=\s*\"[^\"]*(?:display\s*:\s*none|visibility\s*:\s*hidden|color\s*:\s*#?fff(?:fff)?)[^\"]*\"|hidden)[^>]*>(.*?)</\1>",
    re.DOTALL | re.IGNORECASE,
)
HTML_ATTR_PAYLOAD_RE = re.compile(r"\b(?:alt|title|aria-label)\s*=\s*\"([^\"]+)\"", re.IGNORECASE)


SUSPICIOUS_PATTERN_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("ignore previous instructions", r"ignore\W*(?:all\W*)?(?:the\W*)?(?:previous|prior|above)\W*instructions"),
        ("disregard previous", r"disregard\W*(?:the\W*)?(?:previous|prior|above)"),
        ("forget everything", r"forget\W*everything"),
        ("override your", r"override\W*your\W*(?:instructions|rules|guidelines)"),
        ("new instructions", r"(?:new|updated|revised)\W*instructions\W*:"),
        ("system prompt", r"system\W*prompt"),
        ("developer message", r"developer\W*message"),
        ("you are now", r"you\W*are\W*now\W*(?:a|an|the)?\W*\w+"),
        ("act as", r"\bact\W*as\W*(?:a|an|the)?\W*\w+"),
        ("pretend to be", r"pretend\W*to\W*be"),
        ("jailbreak", r"\bjailbreak\b"),
        ("DAN mode", r"\bDAN\W*mode\b"),
        ("role system", r"\brole\W*[:=]\W*system\b"),
        ("assistant turn", r"(?:^|\n)\s*assistant\s*:"),
        ("human turn", r"(?:^|\n)\s*human\s*:"),
        ("user turn injection", r"(?:^|\n)\s*user\s*:\s*\w+"),
        ("chat template token", r"<\|im_(?:start|end)\|>|\[INST\]|<<SYS>>|</?s>"),
        ("tool call markup", r"<tool[\s_>]|tool_call|to\s*=\s*functions"),
        ("sudo command", r"\bsudo\s+\w+"),
        ("rm -rf", r"\brm\s+-rf\b"),
        ("dangerous url scheme", r"(?:javascript:|data:text/html|file://)"),
        ("exfil python", r"```python\s*\n\s*(?:import\s+(?:os|subprocess|requests)|open\(|eval\(|exec\()"),
    )
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


def _strip_html_invisible(text: str) -> str:
    cleaned = HTML_COMMENT_RE.sub(lambda m: " " + m.group(1) + " ", text)
    cleaned = HTML_INVISIBLE_TAG_RE.sub(lambda m: " " + m.group(2) + " ", cleaned)
    cleaned = HTML_ATTR_PAYLOAD_RE.sub(lambda m: " " + m.group(1) + " ", cleaned)
    return cleaned


def _normalize_for_scan(text: str) -> str:
    truncated = text[:MAX_SCAN_BYTES]
    normalized = unicodedata.normalize("NFKD", truncated)
    normalized = ZERO_WIDTH_RE.sub("", normalized)
    normalized = _strip_html_invisible(normalized)
    return normalized


def _telemetry_path() -> Path | None:
    root = os.environ.get("CLAW_TELEMETRY_ROOT")
    if not root:
        return None
    try:
        path = Path(root).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path / "sanitizer.jsonl"
    except OSError:
        return None


def _log_verdict(verdict: str, source: str, pattern: str | None, length: int) -> None:
    path = _telemetry_path()
    if path is None:
        return
    record = {
        "ts": time.time(),
        "verdict": verdict,
        "source": source,
        "pattern": pattern,
        "length": length,
    }
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def sanitize(content: str, source: str, target_agent_class: AgentClass) -> SanitizedContent:
    scan_target = _normalize_for_scan(content)
    matched_label: str | None = None
    for label, pattern in SUSPICIOUS_PATTERN_REGEXES:
        if pattern.search(scan_target):
            matched_label = label
            break
    length = len(content)
    if matched_label is not None:
        _log_verdict("malicious", source, matched_label, length)
        return SanitizedContent(
            verdict="malicious",
            content="",
            source=source,
            target_agent_class=target_agent_class,
            reason=f"suspicious pattern: {matched_label}",
        )
    cleaned = re.sub(r"\s+", " ", content).strip()
    _log_verdict("clean", source, None, length)
    return SanitizedContent(
        verdict="clean",
        content=f"[EXTERNAL:{source}] {cleaned}",
        source=source,
        target_agent_class=target_agent_class,
    )


def extract_structured(content: str, *, source_url: str | None, reason: str) -> QuarantinedExtraction:
    scrubbed = _normalize_for_scan(content)
    numbers = {
        f"value_{idx}": float(match)
        for idx, match in enumerate(re.findall(r"\b\d+(?:\.\d+)?\b", scrubbed)[:10], start=1)
    }
    dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", scrubbed)
    raw_entities = re.findall(r"\b[A-Z][a-zA-Z0-9\.-]{1,49}\b", scrubbed)
    safe_entities: list[str] = []
    for name in raw_entities:
        lowered = name.lower()
        if any(pattern.search(lowered) for _, pattern in SUSPICIOUS_PATTERN_REGEXES):
            continue
        safe_entities.append(name)
    most_common = [name for name, _ in Counter(safe_entities).most_common(20)]
    category = "unknown"
    lowered_all = scrubbed.lower()
    if "http" in lowered_all or "www." in lowered_all:
        category = "landing_page"
    elif "api" in lowered_all or "json" in lowered_all:
        category = "api_response"
    elif "email" in lowered_all or "subject:" in lowered_all:
        category = "email_thread"
    elif "docs" in lowered_all or "documentation" in lowered_all:
        category = "documentation"
    return QuarantinedExtraction(
        source_url=source_url,
        content_type=category,
        numeric_data=numbers,
        entity_names=most_common,
        dates=dates,
        word_count=len(scrubbed.split()),
        quarantine_reason=reason,
    )
