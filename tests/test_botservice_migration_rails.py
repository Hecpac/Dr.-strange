from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

import claw_v2.bot as bot_module
from claw_v2.bot import BotService

# B4.1/B4.2 — BotService migration rails (2026-07-07). These are the
# preconditions the audit requires BEFORE any strangler/migration work on
# BotService: the pre-brain dispatch order is behavior (WIRING §5.1: "Order
# matters"), and the god-module must stop growing while it awaits migration.
# Changing either is a DELIBERATE, test-visible edit — never a side effect.

# Top-level handler call order in BotService._handle_text_body, first
# occurrence wins. Nested dispatchers (e.g. operational alert / boot-context
# inside grouped handlers) are inside these calls; the rail locks the
# top-level sequence a migration would reorder.
EXPECTED_PRE_BRAIN_ORDER = [
    "_maybe_handle_brain_first_new_task",
    "_handle_pending_computer_approval_response",
    "_maybe_handle_operational_failure_summary",
    "_maybe_handle_operational_status",
    "_maybe_handle_cleanup_status_query",
    "_maybe_handle_owner_delegation_request",
    "_maybe_handle_telegram_imperative_request",
    "_maybe_handle_actionable_task_request",
    "_maybe_handle_f4_deterministic_delegation",
    "_maybe_handle_task_intent",
    "_maybe_handle_change_status_question",
    "_maybe_handle_capability_route",
    "_maybe_handle_shortcut",
]

# B4.2 size-ratchet: baseline measured at merge 634a528 (12172 lines) plus an
# explicit tiny allowance for surgical fixes. Growing past this means new code
# is landing in the god-module instead of an extracted home — move it, or
# raise the baseline HERE, deliberately, with review.
BOTSERVICE_LINE_BASELINE = 12172
BOTSERVICE_LINE_ALLOWANCE = 150

_HANDLER_CALL_RE = re.compile(
    r"self\.(_maybe_handle_\w+|_handle_pending_computer_approval_response)\("
)


def _top_level_handler_order() -> list[str]:
    source = inspect.getsource(BotService._handle_text_body)
    ordered: list[str] = []
    for name in _HANDLER_CALL_RE.findall(source):
        if name not in ordered:
            ordered.append(name)
    return ordered


class PreBrainOrderLockTests(unittest.TestCase):
    def test_pre_brain_dispatch_order_is_locked(self) -> None:
        self.assertEqual(
            _top_level_handler_order(),
            EXPECTED_PRE_BRAIN_ORDER,
            "Pre-brain dispatch order changed. WIRING §5.1: order IS behavior. "
            "If this reorder is intentional (e.g. a migration step), update "
            "EXPECTED_PRE_BRAIN_ORDER and §5.1 in the same commit.",
        )

    def test_representative_ordering_decisions_hold(self) -> None:
        # Decisions the order encodes, named so a future reorder reviews them:
        order = _top_level_handler_order()
        idx = {name: i for i, name in enumerate(order)}
        # brain-first semantic routing outruns every literal matcher.
        self.assertEqual(idx["_maybe_handle_brain_first_new_task"], 0)
        # A pending computer approval must be resolved before any imperative
        # matcher can steal the turn (grant words look like imperatives).
        self.assertLess(
            idx["_handle_pending_computer_approval_response"],
            idx["_maybe_handle_telegram_imperative_request"],
        )
        # F4-B1 deterministic delegation captures BEFORE the broad task-intent
        # router (exactly-once contract on the message id).
        self.assertLess(
            idx["_maybe_handle_f4_deterministic_delegation"],
            idx["_maybe_handle_task_intent"],
        )
        # The generic shortcut is the LAST capture before the brain default.
        self.assertEqual(order[-1], "_maybe_handle_shortcut")

    def test_every_locked_handler_still_exists(self) -> None:
        for name in EXPECTED_PRE_BRAIN_ORDER:
            self.assertTrue(hasattr(BotService, name), name)


class BotServiceSizeRatchetTests(unittest.TestCase):
    def test_botservice_module_does_not_grow(self) -> None:
        lines = len(Path(bot_module.__file__).read_text(encoding="utf-8").splitlines())
        limit = BOTSERVICE_LINE_BASELINE + BOTSERVICE_LINE_ALLOWANCE
        self.assertLessEqual(
            lines,
            limit,
            f"claw_v2/bot.py is {lines} lines (ratchet: {limit}). BotService "
            "awaits migration — new code goes in an extracted module, not the "
            "god-module. If growth here is genuinely unavoidable, raise "
            "BOTSERVICE_LINE_BASELINE deliberately in this file with review.",
        )

    def test_ratchet_allowance_stays_tiny(self) -> None:
        # The allowance exists for surgical fixes, not for a slow-motion leak.
        self.assertLessEqual(BOTSERVICE_LINE_ALLOWANCE, 150)


if __name__ == "__main__":
    unittest.main()
