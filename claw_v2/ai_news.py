"""AI news brief — dataclasses, validation y rendering con jerarquía
de evidencia.

Reglas clave:
- `claim_map` se construye desde retrieved source metadata → claim
  evidence → model summary. NO al revés.
- `confidence="high"` requiere `source_kind in {"primary","wire"}` Y
  `verified_fields` no vacío.
- "de hoy" en summary requiere `published_at` o `unverified_fields`
  con tag `not_today`.
- Weekday/date sanity check: el día de la semana debe coincidir con la
  fecha real para la timezone configurada.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo


SourceKind = Literal["primary", "wire", "aggregator", "social", "unknown"]
Confidence = Literal["high", "medium", "low"]


_PRIMARY_OR_WIRE: frozenset[str] = frozenset({"primary", "wire"})


_WEEKDAY_NAMES_ES: list[str] = [
    "lunes",
    "martes",
    "miércoles",
    "miercoles",
    "jueves",
    "viernes",
    "sábado",
    "sabado",
    "domingo",
]


_WEEKDAY_INDEX_ES: dict[str, int] = {
    "lunes": 0,
    "martes": 1,
    "miércoles": 2,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sábado": 5,
    "sabado": 5,
    "domingo": 6,
}


@dataclass(slots=True)
class ClaimEvidence:
    claim: str
    source_title: str
    source_url: str
    source_kind: SourceKind = "unknown"
    published_at: str | None = None
    fetched_at: str = ""
    verified_fields: list[str] = field(default_factory=list)
    unverified_fields: list[str] = field(default_factory=list)
    confidence: Confidence = "medium"


@dataclass(slots=True)
class AiNewsItem:
    title: str
    summary: str
    claims: list[ClaimEvidence] = field(default_factory=list)
    confidence: Confidence = "medium"


@dataclass(slots=True)
class AiNewsBrief:
    date: str  # ISO YYYY-MM-DD
    timezone: str  # e.g., "America/Chicago"
    weekday: str  # localized weekday name (es or index)
    fetched_at: str
    items: list[AiNewsItem] = field(default_factory=list)
    trend: str | None = None


def _normalize_weekday(weekday: str) -> str:
    return weekday.strip().lower()


def _expected_weekday_index(date: str, timezone: str) -> int | None:
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        tz = ZoneInfo(timezone)
        # Localize at noon to avoid edge cases at midnight DST
        localized = dt.replace(hour=12, tzinfo=tz)
        return localized.weekday()
    except Exception:
        return None


def _summary_claims_today(summary: str) -> bool:
    lowered = summary.lower()
    return "de hoy" in lowered or "hoy " in lowered or lowered.startswith("hoy")


def _validate_claim(claim: ClaimEvidence) -> list[str]:
    errors: list[str] = []
    if not claim.source_url:
        errors.append("claim_missing_source_url")
    if not claim.fetched_at:
        errors.append("claim_missing_fetched_at")
    if claim.confidence == "high":
        if claim.source_kind not in _PRIMARY_OR_WIRE:
            errors.append("high_confidence_requires_primary_or_wire_source_kind")
        if not claim.verified_fields:
            errors.append("high_confidence_requires_verified_fields")
    if claim.source_kind == "unknown" and claim.confidence == "high":
        errors.append("unknown_source_kind_cannot_be_high")
    if claim.source_kind == "aggregator" and claim.confidence == "high":
        errors.append("aggregator_source_capped_at_medium")
    if claim.source_kind == "social" and claim.confidence in {"high", "medium"}:
        # social without corroboration capped at low
        errors.append("social_source_without_corroboration_capped_at_low")
    return errors


def validate_ai_news_brief(brief: AiNewsBrief) -> list[str]:
    """Validate a brief. Returns list of error codes (empty = valid)."""
    errors: list[str] = []
    expected_idx = _expected_weekday_index(brief.date, brief.timezone)
    declared_idx = _WEEKDAY_INDEX_ES.get(_normalize_weekday(brief.weekday))
    if expected_idx is None:
        errors.append("invalid_date_or_timezone")
    elif declared_idx is None:
        errors.append("unknown_weekday_label")
    elif expected_idx != declared_idx:
        errors.append("weekday_mismatch_for_date")
    if not brief.fetched_at:
        errors.append("brief_missing_fetched_at")
    for item in brief.items:
        if not item.claims:
            errors.append(f"item_without_claims:{item.title[:30]}")
            continue
        if _summary_claims_today(item.summary):
            has_published = any(c.published_at for c in item.claims)
            has_staleness_flag = any(
                "not_today" in (c.unverified_fields or []) for c in item.claims
            )
            if not has_published and not has_staleness_flag:
                errors.append(f"summary_says_today_without_evidence:{item.title[:30]}")
        for claim in item.claims:
            errors.extend(_validate_claim(claim))
    return errors


def _confidence_marker(confidence: Confidence) -> str:
    return {"high": "✓", "medium": "≈", "low": "?"}[confidence]


def render_ai_news_brief(brief: AiNewsBrief) -> str:
    """Render brief as Markdown. Marks unverified claims explicitly."""
    lines: list[str] = []
    lines.append(f"🌅 AI Brief — {brief.date} ({brief.weekday}) {brief.timezone}")
    lines.append("")
    for index, item in enumerate(brief.items, start=1):
        lines.append(f"{index}. {item.title} {_confidence_marker(item.confidence)}")
        if item.summary:
            lines.append(f"   {item.summary}")
        for claim in item.claims:
            label = (claim.source_title or claim.source_url)[:80]
            kind_label = claim.source_kind
            if claim.confidence == "low" or claim.source_kind in {"social", "unknown"}:
                lines.append(f"   ⚠️ señal no verificada — {label} [{kind_label}]")
            else:
                date_part = (
                    f" — {claim.published_at}" if claim.published_at else " — fecha no confirmada"
                )
                lines.append(f"   • {label} [{kind_label}]{date_part}")
        lines.append("")
    if brief.trend:
        lines.append(f"📊 Trend del día: {brief.trend}")
    return "\n".join(lines).rstrip()
