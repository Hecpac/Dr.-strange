from __future__ import annotations

import inspect
import re
import unittest
from dataclasses import FrozenInstanceError

import claw_v2.bot as bot_module
from claw_v2.bot import BotService
from claw_v2.bot_helpers import _normalize_command_text
from claw_v2.dispatch.matchers import CHANGE_STATUS_MATCHER, RouteMatcher

# B4.4a — declarative matcher pilot (invariant
# b44a_route_matcher_is_declarative_data). The change-status route's match
# contract moved from bot.py module level to dispatch/matchers.py as frozen
# DATA. These tests lock: (1) old-vs-new decisions over a representative
# corpus — the legacy predicate is frozen verbatim below as the reference;
# (2) the dispatch_decision telemetry slugs, byte-identical to pre-pilot;
# (3) single-sourcing — bot.py carries no parallel recognizer and both the
# order-locked gate and the renderer consume the matcher. The pre-brain call
# order itself is locked by tests/test_botservice_migration_rails.py, which
# this pilot must never require editing.

# The recognizer EXACTLY as it lived at bot.py:971-979 up to 8df1a6f.
# If the declarative matcher ever diverges from this behavior, the corpus
# below fails and the divergence must be a deliberate, reviewed edit.
_LEGACY_STATUS_CHANGE_PHRASE_RE = re.compile(
    r"(?:estatus|status|estado)\s+de\s+(?:los\s+)?(?:fixes|cambios)"
)


def _legacy_looks_like_change_status_question(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]+", " ", _normalize_command_text(text)).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return _LEGACY_STATUS_CHANGE_PHRASE_RE.fullmatch(normalized) is not None


REPRESENTATIVE_DECISIONS: list[tuple[str, bool]] = [
    # Positives — includes the three phrasings the change-status e2e tests
    # in tests/test_bot.py have always sent through handle_text.
    ("Estatus de los fixes", True),
    ("status de los fixes", True),
    ("estado de los cambios", True),
    ("¿Estatus de los cambios?", True),
    ("ESTADO DE FIXES", True),
    ("estátus de los cámbios", True),
    ("  status   de  los   cambios  ", True),
    ("estatus de cambios", True),
    # Negatives — fullmatch semantics: leading/trailing content falls through
    # to the brain (Routing Contract: capture only when unambiguous from the
    # literal text alone).
    ("dame el estatus de los cambios", False),
    ("estatus de los cambios y reinicia el daemon", False),
    ("estado", False),
    ("status de la tarea", False),
    ("cómo van los fixes", False),
    ("Estatus de los fixes\ny de paso reinicia", False),
    ("", False),
]


class ChangeStatusDecisionLockTests(unittest.TestCase):
    def test_new_matcher_reproduces_legacy_decisions(self) -> None:
        for text, expected in REPRESENTATIVE_DECISIONS:
            with self.subTest(text=text):
                legacy = _legacy_looks_like_change_status_question(text)
                self.assertEqual(legacy, expected, "corpus drifted from the legacy reference")
                self.assertEqual(
                    CHANGE_STATUS_MATCHER.match(text),
                    legacy,
                    "declarative matcher diverged from the legacy recognizer",
                )


class ChangeStatusMatcherContractTests(unittest.TestCase):
    def test_telemetry_slugs_are_byte_identical_to_pre_pilot(self) -> None:
        # These three strings ride on every change-status dispatch_decision
        # event; changing any of them is a telemetry-visible break.
        self.assertEqual(CHANGE_STATUS_MATCHER.name, "change_status_question")
        self.assertEqual(CHANGE_STATUS_MATCHER.matched_reason, "change_status_phrase_matched")
        self.assertEqual(CHANGE_STATUS_MATCHER.unmatched_reason, "change_status_phrase_no_match")

    def test_matcher_is_frozen_data(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            CHANGE_STATUS_MATCHER.name = "x"  # type: ignore[misc]

    def test_matcher_predicate_takes_literal_text_only(self) -> None:
        # Routing Contract: the match side reads the literal message text
        # alone — no session_state, no ledger, no context objects.
        self.assertEqual(list(inspect.signature(CHANGE_STATUS_MATCHER.match).parameters), ["text"])

    def test_route_matcher_shape_is_the_pilot_contract(self) -> None:
        self.assertEqual(
            set(RouteMatcher.__dataclass_fields__),
            {"name", "match", "matched_reason", "unmatched_reason"},
        )


class BotWiringSingleSourceTests(unittest.TestCase):
    def test_bot_carries_no_parallel_recognizer(self) -> None:
        self.assertFalse(hasattr(bot_module, "_STATUS_CHANGE_PHRASE_RE"))
        self.assertFalse(hasattr(bot_module, "_looks_like_change_status_question"))

    def test_gate_and_renderer_consume_the_declarative_matcher(self) -> None:
        gate_src = inspect.getsource(BotService._maybe_handle_change_status_question)
        self.assertIn("CHANGE_STATUS_MATCHER.match", gate_src)
        renderer_src = inspect.getsource(BotService._change_status_question_response)
        self.assertIn("CHANGE_STATUS_MATCHER.match", renderer_src)


if __name__ == "__main__":
    unittest.main()
