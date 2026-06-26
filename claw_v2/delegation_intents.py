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
    re.compile(r"\bque es\b"),  # definitional ("¿qué es X?" → norm "que es x")
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
)

# An explicit non-X platform means this is NOT an X-feed review (prevents
# "revisa mi feed de Instagram" from enqueuing an X review).
_OTHER_PLATFORM = re.compile(
    r"\b(instagram|insta|facebook|fb|linkedin|tiktok|threads|youtube|yt|reddit|"
    r"mastodon|bluesky|bsky|whatsapp|telegram)\b"
)
# X / Twitter must be EXPLICITLY the target. "x" counts as the platform only
# when bound to a review verb/noun ("repaso por X", "barrido de X") or a feed
# word ("feed/timeline/TL de X") — NOT after an arbitrary object noun, where X
# is a placeholder for a project/repo/topic ("código/repo/PR de X"). Placeholder
# "x" ("punto x", "valor de x") is already rejected above, and bare
# "feed"/"timeline" without X never qualifies — prefer false negatives over
# enqueuing the wrong feed.
_X_PLATFORM = re.compile(rf"\btwitter\b|\b(?:{_REVIEW}|feed|timeline|tl)\s+(?:de|en|por|a)\s+x\b")

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

    if any(p.search(norm) for p in _REJECT_PATTERNS) or _OTHER_PLATFORM.search(norm):
        return None

    has_review = bool(re.search(rf"\b{_REVIEW}\b", norm)) or any(
        phrase in norm for phrase in _REVIEW_PHRASES
    )
    if not has_review:
        return None

    # X / Twitter must be the explicit target (no bare feed/timeline, no bare X).
    if not _X_PLATFORM.search(norm):
        return None

    return AuthenticatedBrowseIntent(kind="authenticated_browse", objective=_OBJECTIVE)
