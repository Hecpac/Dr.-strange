from __future__ import annotations

import inspect
import re
import unittest
from dataclasses import FrozenInstanceError

import claw_v2.bot as bot_module
from claw_v2.bot import BotService
from claw_v2.bot_helpers import _normalize_command_text
from claw_v2.dispatch.matchers import CLEANUP_STATUS_MATCHER, RouteMatcher

# B4.4b — second declarative matcher (follows the B4.4a shape, invariant
# b44b_cleanup_matcher_is_declarative_data). The cleanup-status route's match
# contract moved from inline lines in
# BotService._maybe_handle_cleanup_status_query to dispatch/matchers.py as
# frozen DATA. These tests lock: (1) old-vs-new decisions over a
# representative corpus — the legacy inline predicate is frozen verbatim
# below as the reference; (2) the dispatch_decision telemetry slugs,
# byte-identical to pre-slice; (3) single-sourcing — bot.py carries no
# parallel recognizer and the order-locked gate consumes the matcher. The
# pre-brain call order itself is locked by
# tests/test_botservice_migration_rails.py, which this slice must never
# require editing. Response rendering (session_state + approvals reads)
# stays on BotService — only the match side is data.

# The recognizer EXACTLY as it lived inline at bot.py:9153-9156 up to
# 31d489a. If the declarative matcher ever diverges from this behavior, the
# corpus below fails and the divergence must be a deliberate, reviewed edit.


def _legacy_looks_like_cleanup_status_query(text: str) -> bool:
    normalized = _normalize_command_text(text).strip(" \t\n\r.,;:!?¿¡")
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    return compact in {"limpiaste", "yalimpiaste", "cleaned", "didyouclean", "didyoucleanup"}


REPRESENTATIVE_DECISIONS: list[tuple[str, bool]] = [
    # Positives — exact-message phrases only; the compact step strips every
    # non-alphanumeric so punctuation, case, accents, and inner spacing all
    # normalize away.
    ("limpiaste", True),
    ("¿Limpiaste?", True),
    ("Ya limpiaste", True),
    ("¿ya limpiaste?", True),
    ("ya... limpiaste!!", True),
    ("cleaned", True),
    ("Cleaned?", True),
    ("did you clean", True),
    ("Did you clean?", True),
    ("did you cleanup", True),
    ("DID-YOU-CLEANUP", True),
    ("  limpiaste  ", True),
    # Negatives — any extra content beyond the exact phrase falls through to
    # the brain (Routing Contract: capture only when unambiguous from the
    # literal text alone).
    ("limpiaste los approvals", False),
    ("ya limpiaste todo", False),
    ("¿limpiaste las aprobaciones stale?", False),
    ("did you clean the queue", False),
    ("clean", False),
    ("limpia", False),
    ("limpiar", False),
    ("cleanup", False),
    ("", False),
]


class CleanupStatusDecisionLockTests(unittest.TestCase):
    def test_new_matcher_reproduces_legacy_decisions(self) -> None:
        for text, expected in REPRESENTATIVE_DECISIONS:
            with self.subTest(text=text):
                legacy = _legacy_looks_like_cleanup_status_query(text)
                self.assertEqual(legacy, expected, "corpus drifted from the legacy reference")
                self.assertEqual(
                    CLEANUP_STATUS_MATCHER.match(text),
                    legacy,
                    "declarative matcher diverged from the legacy recognizer",
                )


class CleanupStatusMatcherContractTests(unittest.TestCase):
    def test_telemetry_slugs_are_byte_identical_to_pre_slice(self) -> None:
        # These three strings ride on every cleanup-status dispatch_decision
        # event; changing any of them is a telemetry-visible break.
        self.assertEqual(CLEANUP_STATUS_MATCHER.name, "cleanup_status")
        self.assertEqual(CLEANUP_STATUS_MATCHER.matched_reason, "cleanup_status_matched")
        self.assertEqual(CLEANUP_STATUS_MATCHER.unmatched_reason, "cleanup_status_no_match")

    def test_matcher_is_frozen_data(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            CLEANUP_STATUS_MATCHER.name = "x"  # type: ignore[misc]

    def test_matcher_predicate_takes_literal_text_only(self) -> None:
        # Routing Contract: the match side reads the literal message text
        # alone — no session_state, no ledger, no context objects.
        self.assertEqual(list(inspect.signature(CLEANUP_STATUS_MATCHER.match).parameters), ["text"])

    def test_route_matcher_shape_is_the_pilot_contract(self) -> None:
        self.assertEqual(
            set(RouteMatcher.__dataclass_fields__),
            {"name", "match", "matched_reason", "unmatched_reason"},
        )


class BotWiringSingleSourceTests(unittest.TestCase):
    def test_bot_carries_no_parallel_recognizer(self) -> None:
        # The inline normalize/compact/set-membership lines must not survive
        # in the handler — the matcher is the single source of the decision.
        gate_src = inspect.getsource(BotService._maybe_handle_cleanup_status_query)
        self.assertNotIn("compact", gate_src)
        self.assertNotIn("limpiaste", gate_src)
        self.assertFalse(hasattr(bot_module, "_looks_like_cleanup_status_query"))

    def test_gate_consumes_the_declarative_matcher(self) -> None:
        gate_src = inspect.getsource(BotService._maybe_handle_cleanup_status_query)
        self.assertIn("CLEANUP_STATUS_MATCHER.match", gate_src)


if __name__ == "__main__":
    unittest.main()
