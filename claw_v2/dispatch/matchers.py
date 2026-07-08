"""Declarative route matchers — B4.4a pilot.

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

One matcher so far (change-status, migrated from bot.py module level).
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


_STATUS_CHANGE_PHRASE_RE = re.compile(
    r"(?:estatus|status|estado)\s+de\s+(?:los\s+)?(?:fixes|cambios)"
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
