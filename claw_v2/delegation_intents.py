"""Narrow, conservative classifier for high-confidence authenticated-browse
delegation intents (F4-B1).

Only matches unambiguous "review my authenticated X / Twitter feed" requests —
work that must run as a delegated background browse (authenticated Chrome/CDP),
not inline in the chat turn. It deliberately prefers false negatives over
false-positive job creation: a near-miss falls through to the brain rather than
silently enqueuing the wrong work.

Pure function, no side effects — unit-testable in isolation.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = ["AuthenticatedBrowseIntent", "classify_authenticated_browse_intent"]


@dataclass(frozen=True, slots=True)
class AuthenticatedBrowseIntent:
    kind: str  # "authenticated_browse"
    objective: str  # imperative delegation objective (survives non-actionable guard)


def _normalize(text: str) -> str:
    """Lowercase + strip accents so matching is accent-insensitive."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


# Authoring / definitional / opinion / summarize / placeholder markers. Any hit
# disqualifies the turn (it is not a feed-review request).
_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bescrib"),  # escribe / escribir / escríbeme
    re.compile(r"\bredact"),  # redacta
    re.compile(r"\bpostea|\bpublica|\bpubliqu"),
    re.compile(r"\bborrador\b|\bdraft\b|\bhilo\b|\btweet\b|\bpost\b"),
    re.compile(r"\bque es\b|\bqué es\b"),  # definitional ("¿qué es X?")
    re.compile(r"\bopina|\bopinas|\bpiensas de\b|\bque opinas\b"),  # opinion
    re.compile(r"\bresume\b|\bresumen\b|\bresumeme\b"),  # summarize-text
    # X-as-placeholder, not the platform:
    re.compile(r"\bpunto x\b|\bvalor de x\b|\bvariable x\b|\bequis\b"),
    re.compile(r"\bx\s+(razon|motivo)\b"),  # "por X razón / motivo"
    re.compile(r"\bpor x\s+(o|y)\b"),  # "por X o por Y"
)

# Review verbs (normalized).
_REVIEW = (
    r"(repaso|repasa|repasar|repasame|barrido|barre|barreme|revisa|revisame|"
    r"revisar|revision|chequea|chequeame|chequear|ojea|ojeame)"
)
_REVIEW_PHRASES: tuple[str, ...] = (
    "dale una vuelta",
    "echa un vistazo",
    "echale un vistazo",
    "ponme al dia",
    "ponme al día",
)

# Strong, unambiguous platform/feed signals.
_STRONG_PLATFORM = re.compile(r"\btwitter\b|\b(feed|timeline|tl)\b")
# Incident idiom: a review verb followed by "(por|de|en|a) X" — X as the
# platform. Guarded against placeholders by _REJECT_PATTERNS above.
_INCIDENT_SHAPE = re.compile(rf"\b{_REVIEW}\b.*\b(por|de|en|a)\s+x\b")

_OBJECTIVE = (
    "Revisa el feed autenticado de X (Twitter) del usuario por el carril de "
    "navegacion delegada y reporta lo relevante (cuentas, temas y enlaces "
    "destacados). No inventes el contenido; si no puedes leer el feed, dilo."
)


def classify_authenticated_browse_intent(text: str) -> AuthenticatedBrowseIntent | None:
    """Return an intent only for unambiguous authenticated X/feed-review
    requests; otherwise ``None`` (fall through to the brain)."""
    if not text or not text.strip():
        return None
    norm = _normalize(text)

    if any(p.search(norm) for p in _REJECT_PATTERNS):
        return None

    has_review = bool(re.search(rf"\b{_REVIEW}\b", norm)) or any(
        phrase in norm for phrase in _REVIEW_PHRASES
    )
    if not has_review:
        return None

    has_platform = bool(_STRONG_PLATFORM.search(norm)) or bool(_INCIDENT_SHAPE.search(norm))
    if not has_platform:
        return None

    return AuthenticatedBrowseIntent(kind="authenticated_browse", objective=_OBJECTIVE)
