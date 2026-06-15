from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from claw_v2.redaction import redact_sensitive


MemoryPromptResidency = Literal[
    "always_in_prompt",
    "retrieval_on_demand",
    "never_in_prompt",
]

PROMPT_RESIDENCIES: frozenset[str] = frozenset(
    {"always_in_prompt", "retrieval_on_demand", "never_in_prompt"}
)
ALWAYS_IN_PROMPT_CONFIDENCE = 0.70
LONG_EVIDENCE_CHARS = 1_200

_SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "access_token",
    "credential",
    "cookie",
    "private_key",
    "approval",
    "bearer",
)
_SECRET_TAGS = {
    "token",
    "secret",
    "password",
    "api_key",
    "access_token",
    "credential",
    "cookie",
    "private_key",
}
_INSTRUCTION_SHAPED_RE = re.compile(
    r"(?i)\b("
    r"ignore\s+(?:all\s+)?(?:previous|prior)|"
    r"system\s+prompt|developer\s+message|"
    r"you\s+are\s+now|jailbreak|"
    r"follow\s+these\s+instructions|"
    r"approval[_ -]?token|api[_ -]?key|password|secret"
    r")\b"
)


@dataclass(frozen=True, slots=True)
class MemoryResidencyDecision:
    residency: MemoryPromptResidency
    reason: str
    source: str
    source_trust: str
    confidence: float
    freshness: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "residency": self.residency,
            "reason": self.reason,
            "source": self.source,
            "source_trust": self.source_trust,
            "confidence": self.confidence,
            "freshness": self.freshness,
        }


def normalize_prompt_residency(value: Any) -> MemoryPromptResidency | None:
    text = str(value or "").strip()
    if text in PROMPT_RESIDENCIES:
        return text  # type: ignore[return-value]
    return None


def classify_memory_fact(
    row: dict[str, Any], *, now: datetime | None = None
) -> MemoryResidencyDecision:
    key = str(row.get("key") or "")
    value = str(row.get("value") or "")
    source = str(row.get("source") or "unknown")
    source_trust = str(row.get("source_trust") or "untrusted")
    confidence = _coerce_confidence(row.get("confidence"))
    explicit = normalize_prompt_residency(row.get("prompt_residency"))
    freshness = _freshness(row, now=now)
    tags = _entity_tags(row.get("entity_tags"))

    if _contains_secret_like_payload(row):
        return _decision(
            "never_in_prompt", "secret_like_payload", source, source_trust, confidence, freshness
        )
    if explicit == "never_in_prompt":
        return _decision(
            "never_in_prompt", "explicit_policy", source, source_trust, confidence, freshness
        )
    if source_trust == "untrusted" and _INSTRUCTION_SHAPED_RE.search(f"{key}\n{value}"):
        return _decision(
            "never_in_prompt",
            "untrusted_instruction_shaped",
            source,
            source_trust,
            confidence,
            freshness,
        )
    if explicit == "retrieval_on_demand":
        return _decision(
            "retrieval_on_demand", "explicit_policy", source, source_trust, confidence, freshness
        )
    if freshness == "expired":
        return _decision(
            "retrieval_on_demand", "expired_fact", source, source_trust, confidence, freshness
        )
    if confidence < ALWAYS_IN_PROMPT_CONFIDENCE:
        return _decision(
            "retrieval_on_demand", "low_confidence", source, source_trust, confidence, freshness
        )
    if len(value) > LONG_EVIDENCE_CHARS:
        return _decision(
            "retrieval_on_demand", "long_evidence", source, source_trust, confidence, freshness
        )
    if explicit == "always_in_prompt":
        return _decision(
            "always_in_prompt", "explicit_policy", source, source_trust, confidence, freshness
        )
    if _is_learning_fact(key, tags):
        return _decision(
            "always_in_prompt", "durable_learning_fact", source, source_trust, confidence, freshness
        )
    if key.startswith("profile.") or source_trust in {"trusted", "verified", "system"}:
        return _decision(
            "always_in_prompt",
            "durable_high_confidence",
            source,
            source_trust,
            confidence,
            freshness,
        )
    return _decision(
        "retrieval_on_demand", "default_retrieval", source, source_trust, confidence, freshness
    )


def format_memory_fact_for_prompt(
    row: dict[str, Any],
    *,
    decision: MemoryResidencyDecision | None = None,
    separator: str = ":",
    limit: int = 420,
) -> str:
    decision = decision or classify_memory_fact(row)
    key = _safe_prompt_text(row.get("key", ""), limit=120)
    value = _safe_prompt_text(row.get("value", ""), limit=limit)
    metadata = (
        f"source={_safe_prompt_text(decision.source, limit=80)} "
        f"trust={_safe_prompt_text(decision.source_trust, limit=40)} "
        f"confidence={decision.confidence:.2f} "
        f"freshness={decision.freshness} "
        f"residency={decision.residency}"
    )
    return f"- {key}{separator} {value} [{metadata}]"


def _decision(
    residency: MemoryPromptResidency,
    reason: str,
    source: str,
    source_trust: str,
    confidence: float,
    freshness: str,
) -> MemoryResidencyDecision:
    return MemoryResidencyDecision(
        residency=residency,
        reason=reason,
        source=source,
        source_trust=source_trust,
        confidence=confidence,
        freshness=freshness,
    )


def _contains_secret_like_payload(row: dict[str, Any]) -> bool:
    key = str(row.get("key") or "")
    value = str(row.get("value") or "")
    tags = _entity_tags(row.get("entity_tags"))
    lowered_key = key.lower()
    if any(fragment in lowered_key for fragment in _SECRET_KEY_FRAGMENTS):
        return True
    if any(tag in _SECRET_TAGS for tag in tags):
        return True
    return redact_sensitive(key, limit=0) != key or redact_sensitive(value, limit=0) != value


def _freshness(row: dict[str, Any], *, now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    valid_until = _parse_datetime(row.get("valid_until"))
    if valid_until is not None and valid_until < _normalize_datetime(current):
        return "expired"
    return "current"


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _entity_tags(raw: Any) -> set[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    if not isinstance(raw, list):
        return set()
    return {str(tag).strip().lower() for tag in raw if str(tag).strip()}


def _is_learning_fact(key: str, tags: set[str]) -> bool:
    return key == "learning_loop_consolidated" or "learning" in tags


def _coerce_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def _safe_prompt_text(value: Any, *, limit: int) -> str:
    text = str(redact_sensitive(value, limit=limit))
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip() + "...[truncated]"
    return text
