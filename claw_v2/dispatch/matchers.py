"""Declarative route matchers — B4.4a pilot + B4.4b + B4.4c.

The match side of a pre-brain route as inspectable DATA: a name (the
dispatch_decision handler slug), a pure predicate over the literal message
text, and the matched/unmatched reason slugs the dispatcher emits. Response
rendering stays on BotService; the order-locked call sites in
_handle_text_body stay untouched (botservice_pre_brain_order_is_locked) —
a matcher migrates here by extracting its predicate, never by moving its
call. Full-handler migration into the dispatch_routes registry is a
SEPARATE, deliberate step that edits EXPECTED_PRE_BRAIN_ORDER.

Per the Routing Contract (AGENTS.md): a matcher decides from the literal
text alone — no session_state, no reply context, no ledger reads.

Matchers so far: change-status (B4.4a, from bot.py module level),
cleanup-status (B4.4b) and operational-status (B4.4c), both from inline
lines in their handler methods.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from claw_v2.bot_helpers import _normalize_command_text


@dataclass(frozen=True)
class RouteMatcher:
    """The declarative match contract of one pre-brain route."""

    name: str  # dispatch_decision handler slug
    match: Callable[[str], bool]  # pure predicate over literal message text
    matched_reason: str
    unmatched_reason: str


# 2026-07-09 — FIRST deliberate divergence from the legacy predicate
# (decision: estados-plural, opción B; live incident 2026-07-08: Telegram
# autocorrect sent "Estados de los cambios" and the deterministic route
# missed). Minimal scope: an optional trailing s on "estado" ONLY —
# fullmatch semantics, the other tokens, and normalization stay
# legacy-identical. The delta is enumerated in
# tests/test_b44a_declarative_matcher_pilot.py (DELIBERATE_WIDENING_DECISIONS);
# the legacy reference there stays frozen verbatim. This lifts the
# anti-widening rail for THIS documented delta only, not as a license.
_STATUS_CHANGE_PHRASE_RE = re.compile(
    r"(?:estatus|status|estados?)\s+de\s+(?:los\s+)?(?:fixes|cambios)"
)


def looks_like_change_status_question(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return _STATUS_CHANGE_PHRASE_RE.fullmatch(normalized) is not None


CHANGE_STATUS_MATCHER = RouteMatcher(
    name="change_status_question",
    match=looks_like_change_status_question,
    matched_reason="change_status_phrase_matched",
    unmatched_reason="change_status_phrase_no_match",
)


# B4.4b: exact-message cleanup-status recognizer, extracted verbatim from the
# inline lines of BotService._maybe_handle_cleanup_status_query (bot.py, up to
# 31d489a). The compact step strips every non-alphanumeric, so the match is an
# exact-phrase membership test — even stricter than change-status fullmatch.
_CLEANUP_STATUS_EXACT_PHRASES = frozenset(
    {"limpiaste", "yalimpiaste", "cleaned", "didyouclean", "didyoucleanup"}
)


def looks_like_cleanup_status_query(text: str) -> bool:
    normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    return compact in _CLEANUP_STATUS_EXACT_PHRASES


CLEANUP_STATUS_MATCHER = RouteMatcher(
    name="cleanup_status",
    match=looks_like_cleanup_status_query,
    matched_reason="cleanup_status_matched",
    unmatched_reason="cleanup_status_no_match",
)


# B4.4c: operational-status recognizer, extracted verbatim from the inline
# lines of BotService._maybe_handle_operational_status (bot.py, up to
# be7c6d8). Three branches, preserved exactly: exact normalized-phrase
# membership, exact compact membership, and the greeting+status-token
# substring branch. Note this route runs EARLIER than change-status in the
# order-locked dispatch, so e.g. "hola estado de los cambios" is intercepted
# here via the greeting branch — legacy behavior, corpus-locked in the slice
# test's overlap section.
_OPERATIONAL_STATUS_PHRASES = frozenset(
    {
        "status",
        "estado",
        "estatus",
        "estas",
        "estas?",
        "estas ?",
        "estas vivo",
        "estas viva",
        "estas ahi",
        "estas ahi?",
        "estas ahi ?",
        "ping",
        "como vamos",
        "cómo vamos",
        "que hay pendiente",
        "qué hay pendiente",
        "daily status",
    }
)

_OPERATIONAL_STATUS_COMPACT = frozenset(
    {
        "estas",
        "estasvivo",
        "estasviva",
        "estasahi",
        "buendiastatus",
        "buenosdiasstatus",
        "dailystatus",
        "comovamos",
        "quehaypendiente",
    }
)

_OPERATIONAL_STATUS_GREETINGS = ("buen dia", "buenos dias", "good morning", "hola")
_OPERATIONAL_STATUS_TOKENS = ("status", "estado", "estatus")


def looks_like_operational_status_query(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    greeting_status = any(
        greeting in normalized for greeting in _OPERATIONAL_STATUS_GREETINGS
    ) and any(token in normalized for token in _OPERATIONAL_STATUS_TOKENS)
    contains_status_request = (
        normalized in _OPERATIONAL_STATUS_PHRASES or compact in _OPERATIONAL_STATUS_COMPACT
    )
    return contains_status_request or greeting_status


OPERATIONAL_STATUS_MATCHER = RouteMatcher(
    name="operational_status",
    match=looks_like_operational_status_query,
    matched_reason="operational_status_matched",
    unmatched_reason="operational_status_no_match",
)
