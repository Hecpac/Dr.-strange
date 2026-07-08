from __future__ import annotations

import inspect
import re
import unittest
from dataclasses import FrozenInstanceError

import claw_v2.bot as bot_module
from claw_v2.bot import BotService
from claw_v2.bot_helpers import _normalize_command_text
from claw_v2.dispatch.matchers import (
    CHANGE_STATUS_MATCHER,
    CLEANUP_STATUS_MATCHER,
    OPERATIONAL_STATUS_MATCHER,
    RouteMatcher,
)

# B4.4c — third declarative matcher (follows the B4.4a/B4.4b shape, invariant
# b44c_operational_status_matcher_is_declarative_data). The operational-status
# route's match contract moved from inline lines in
# BotService._maybe_handle_operational_status to dispatch/matchers.py as
# frozen DATA. These tests lock: (1) old-vs-new decisions over a
# representative corpus — the legacy inline predicate is frozen verbatim
# below as the reference; (2) explicit OVERLAP semantics against the two
# already-migrated matchers (change-status, cleanup-status), including the
# greeting-branch interception that runs earlier in the order-locked
# dispatch; (3) the dispatch_decision telemetry slugs, byte-identical to
# pre-slice; (4) single-sourcing — bot.py carries no parallel recognizer and
# the order-locked gate consumes the matcher. The pre-brain call order itself
# is locked by tests/test_botservice_migration_rails.py, which this slice
# must never require editing. Response rendering (task counting +
# quality-guard wrap) stays on BotService — only the match side is data.

# The recognizer EXACTLY as it lived inline at bot.py:11505-11541 up to
# be7c6d8. If the declarative matcher ever diverges from this behavior, the
# corpus below fails and the divergence must be a deliberate, reviewed edit.


def _legacy_looks_like_operational_status(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    status_phrases = {
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
    greeting_status = any(
        greeting in normalized for greeting in ("buen dia", "buenos dias", "good morning", "hola")
    ) and any(token in normalized for token in ("status", "estado", "estatus"))
    contains_status_request = normalized in status_phrases or compact in {
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
    return contains_status_request or greeting_status


REPRESENTATIVE_DECISIONS: list[tuple[str, bool]] = [
    # Positives — exact-phrase branch (normalized membership).
    ("status", True),
    ("estado", True),
    ("Estatus", True),
    ("ping", True),
    ("daily status", True),
    ("como vamos", True),
    ("¿Cómo vamos?", True),
    ("que hay pendiente", True),
    ("¿Qué hay pendiente?", True),
    # Positives — compact branch (punctuation/case/accents collapse).
    ("¿Estás?", True),
    ("estas vivo", True),
    ("¿Estás ahí?", True),
    ("ESTAS AHI", True),
    # Positives — greeting + status-token substring branch.
    ("Buen día, status", True),
    ("buenos dias estatus", True),
    ("good morning status", True),
    ("hola, como va el estado", True),
    # Negatives — status-adjacent but not the exact contract.
    ("dame el status del deploy", False),
    ("como va todo", False),
    ("estado del daemon", False),
    ("hola", False),
    ("good morning", False),
    ("ping the server", False),
    ("", False),
]


class OperationalStatusDecisionLockTests(unittest.TestCase):
    def test_new_matcher_reproduces_legacy_decisions(self) -> None:
        for text, expected in REPRESENTATIVE_DECISIONS:
            with self.subTest(text=text):
                legacy = _legacy_looks_like_operational_status(text)
                self.assertEqual(legacy, expected, "corpus drifted from the legacy reference")
                self.assertEqual(
                    OPERATIONAL_STATUS_MATCHER.match(text),
                    legacy,
                    "declarative matcher diverged from the legacy recognizer",
                )


# Overlap contract with the already-migrated matchers. operational_status
# runs EARLIER than change-status in the order-locked dispatch (§5.1 rows
# 6 vs 10), so any text both could claim is intercepted by
# operational_status first; the cases below lock which matcher owns each
# phrase. (op, chg, cln) per text.
OVERLAP_DECISIONS: list[tuple[str, bool, bool, bool]] = [
    # Bare "estado"/"estatus"/"status" belong to operational_status; the
    # change-status fullmatch needs the full "… de los cambios/fixes" phrase.
    ("estado", True, False, False),
    ("status", True, False, False),
    # The full change-status phrases do NOT hit operational_status (not in
    # its exact sets; no greeting token) — they reach row 10 untouched.
    ("estado de los cambios", False, True, False),
    ("estatus de los fixes", False, True, False),
    # Greeting-branch interception: a greeting + "estado" substring makes
    # operational_status capture even when change-status words follow.
    # change-status would not match anyway (fullmatch rejects the prefix) —
    # legacy behavior, locked here so a future edit that widens/narrows the
    # greeting branch is corpus-visible.
    ("hola estado de los cambios", True, False, False),
    ("buen dia, estatus de los fixes", True, False, False),
    # Cleanup-status phrases touch neither status matcher.
    ("¿ya limpiaste?", False, False, True),
    ("cleaned", False, False, True),
]


class MatcherOverlapTests(unittest.TestCase):
    def test_overlap_with_migrated_matchers_is_locked(self) -> None:
        for text, exp_op, exp_chg, exp_cln in OVERLAP_DECISIONS:
            with self.subTest(text=text):
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_op, "op drift")
                self.assertEqual(CHANGE_STATUS_MATCHER.match(text), exp_chg, "chg drift")
                self.assertEqual(CLEANUP_STATUS_MATCHER.match(text), exp_cln, "cln drift")

    def test_no_overlap_case_claims_two_matchers(self) -> None:
        # Every locked overlap case resolves to exactly one owner (or none);
        # the order lock decides ties, but today no tie exists in the corpus.
        for text, exp_op, exp_chg, exp_cln in OVERLAP_DECISIONS:
            with self.subTest(text=text):
                self.assertLessEqual(exp_op + exp_chg + exp_cln, 1)


class OperationalStatusMatcherContractTests(unittest.TestCase):
    def test_telemetry_slugs_are_byte_identical_to_pre_slice(self) -> None:
        # These three strings ride on every operational-status
        # dispatch_decision event; changing any is a telemetry-visible break.
        self.assertEqual(OPERATIONAL_STATUS_MATCHER.name, "operational_status")
        self.assertEqual(OPERATIONAL_STATUS_MATCHER.matched_reason, "operational_status_matched")
        self.assertEqual(OPERATIONAL_STATUS_MATCHER.unmatched_reason, "operational_status_no_match")

    def test_matcher_is_frozen_data(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            OPERATIONAL_STATUS_MATCHER.name = "x"  # type: ignore[misc]

    def test_matcher_predicate_takes_literal_text_only(self) -> None:
        # Routing Contract: the match side reads the literal message text
        # alone — no session_state, no ledger, no context objects.
        self.assertEqual(
            list(inspect.signature(OPERATIONAL_STATUS_MATCHER.match).parameters), ["text"]
        )

    def test_route_matcher_shape_is_the_pilot_contract(self) -> None:
        self.assertEqual(
            set(RouteMatcher.__dataclass_fields__),
            {"name", "match", "matched_reason", "unmatched_reason"},
        )


class BotWiringSingleSourceTests(unittest.TestCase):
    def test_bot_carries_no_parallel_recognizer(self) -> None:
        # The inline phrase sets / greeting branch must not survive in the
        # handler — the matcher is the single source of the decision.
        gate_src = inspect.getsource(BotService._maybe_handle_operational_status)
        self.assertNotIn("status_phrases", gate_src)
        self.assertNotIn("greeting_status", gate_src)
        self.assertNotIn("estas vivo", gate_src)
        self.assertFalse(hasattr(bot_module, "_looks_like_operational_status"))

    def test_gate_consumes_the_declarative_matcher(self) -> None:
        gate_src = inspect.getsource(BotService._maybe_handle_operational_status)
        self.assertIn("OPERATIONAL_STATUS_MATCHER.match", gate_src)


if __name__ == "__main__":
    unittest.main()
