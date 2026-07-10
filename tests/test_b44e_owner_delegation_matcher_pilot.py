from __future__ import annotations

import ast
import inspect
import re
import textwrap
import unicodedata
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from claw_v2.bot import BotService
from claw_v2.dispatch.matchers import (
    CHANGE_STATUS_MATCHER,
    CLEANUP_STATUS_MATCHER,
    OPERATIONAL_FAILURE_SUMMARY_MATCHER,
    OPERATIONAL_STATUS_MATCHER,
    OWNER_DELEGATION_MATCHER,
    RouteMatcher,
)

# B4.4e extracts only the owner-delegation route's boolean contract and base
# dispatch-decision slugs. The rich OwnerDelegationIntent still comes from the
# frozen classifier because resolution and dynamic ``owner_delegation:{kind}``
# telemetry need its fields. Registry ownership is deliberately deferred to
# B4.5e: this pilot locks the legacy text-chain call site and its 4000-character
# memory limit, with no quality guard.


_LEGACY_OWNER_DELEGATION_EXEC_PATTERNS_ES: tuple[str, ...] = (
    r"\b(?:correlo|corrolo)s?\s+tu(?:\s+mismo)?\b",
    r"\bcorre\s+los?\s+tu(?:\s+mismo)?\b",
    r"\bpuedes\s+correr(?:los?)?\s+tu\b",
    r"\bhaz\s*los?\s+tu(?:\s+mismo)?\b",
    r"\bhazlo\s+tu(?:\s+mismo)?\b",
    r"\bejecuta(?:lo|los)?\s+tu(?:\s+mismo)?\b",
    r"\blo\s+haces\s+tu(?:\s+mismo)?\b",
    r"\blo\s+corres\s+tu(?:\s+mismo)?\b",
)
_LEGACY_OWNER_DELEGATION_EXEC_PATTERNS_EN: tuple[str, ...] = (
    r"\brun\s+(?:it|this|that|them|those)\s+yourself\b",
    r"\byou\s+run\s+(?:it|this|that|them|those)\b",
    r"\bdo\s+(?:it|this|that|them)\s+yourself\b",
    r"\bdo\s+(?:it|this|that|them)\s+for\s+me\b",
    r"\byou\s+handle\s+(?:it|this|that|them)\b",
)
_LEGACY_OWNER_DELEGATION_DECISION_PATTERNS_ES: tuple[str, ...] = (
    r"\bdecide\s+tu\b",
    r"\btu\s+decides\b",
    r"\bescoge\s+tu\b",
    r"\belige\s+tu\b",
    r"\btu\s+eliges\b",
)
_LEGACY_OWNER_DELEGATION_DECISION_PATTERNS_EN: tuple[str, ...] = (
    r"\byou\s+decide\b",
    r"\byou\s+choose\b",
    r"\byou\s+pick\b",
)
_LEGACY_OWNER_DELEGATION_NO_MANUAL_PATTERNS_ES: tuple[str, ...] = (
    r"\bte\s+toca\s+a\s+ti\b",
    r"\b(?:ya\s+)?no\s+tengo\s+que\s+teclear(?:\s+nada)?\b",
    r"\bno\s+me\s+pidas\s+(?:que\s+lo\s+haga|que\s+teclee|que\s+corra)\b",
    r"\bno\s+me\s+preguntes(?:\s+m[aá]s)?\b",
    r"\bno\s+me\s+devuelvas\s+el\s+trabajo\b",
    r"\bno\s+me\s+hagas\s+teclear\b",
    r"\bencargate\s+tu(?:\s+de\s+(?P<hint_es_encargate>.+))?\b",
    r"\bgestiona(?:lo|los)?\s+tu(?:\s+de\s+(?P<hint_es_gestiona>.+))?\b",
)
_LEGACY_OWNER_DELEGATION_NO_MANUAL_PATTERNS_EN: tuple[str, ...] = (
    r"\btake\s+ownership(?:\s+of\s+(?P<hint_en_ownership>.+))?\b",
    r"\bhandle\s+it\b",
    r"\bdon'?t\s+ask\s+me\s+(?:to\s+do\s+it|to\s+run|anymore)\b",
    r"\bstop\s+asking\s+me\s+to\s+(?:run|do|type)\b",
)
_LEGACY_OWNER_DELEGATION_MIN_SIGNAL = 0.70


def _legacy_normalize_command_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _legacy_owner_delegation_lexical_score(normalized: str, matched_text: str) -> float:
    matched_tokens = set(_legacy_normalize_command_text(matched_text).split())
    if not matched_tokens:
        return 0.0
    tokens = set(normalized.split())
    if not tokens:
        return 0.0
    return len(matched_tokens & tokens) / len(matched_tokens)


def _legacy_owner_delegation_direction_score(normalized: str, *, kind: str) -> float:
    text = f" {normalized} "
    hypothetical_markers = (
        " what would happen ",
        " que pasaria ",
        " que pasa si ",
        " if you decide ",
        " if you choose ",
        " si decide tu ",
        " si tu decides ",
        " dime si decide tu ",
        " antes de que ",
        " deberia hacerlo tu o yo ",
        " deberia hacerlo yo o tu ",
    )
    if any(marker in text for marker in hypothetical_markers):
        return 0.20
    if re.search(r"\b(?:deberia|should)\b.+\b(?:tu|you)\b.+\b(?:yo|me|i)\b", normalized):
        return 0.25
    if kind == "decision" and re.search(
        r"\b(?:decide tu|tu decides|tu eliges|escoge tu|elige tu|you decide|you choose|you pick)\b",
        normalized,
    ):
        return 0.95
    if kind == "execution" and re.search(r"\b(?:tu|you|yourself|for me)\b", normalized):
        return 0.95
    if kind == "no_manual_work" and re.search(
        r"\b(?:no me|no tengo|don't ask|don t ask|stop asking|take ownership|handle it|encargate|gestiona|gestionalo|te toca)\b",
        normalized,
    ):
        return 0.95
    return 0.65


def _legacy_owner_delegation_pattern_matches(*, normalized: str, pattern: str, kind: str) -> bool:
    match = re.search(pattern, normalized)
    if match is None:
        return False
    lexical_score = _legacy_owner_delegation_lexical_score(normalized, match.group(0))
    direction_score = _legacy_owner_delegation_direction_score(normalized, kind=kind)
    return (
        lexical_score >= _LEGACY_OWNER_DELEGATION_MIN_SIGNAL
        and direction_score >= _LEGACY_OWNER_DELEGATION_MIN_SIGNAL
    )


def _legacy_matches_owner_delegation(text: str) -> bool:
    """Independent copy of the pre-B4.4e boolean route gate."""
    if not text:
        return False
    normalized = _legacy_normalize_command_text(text)

    for pattern in (
        _LEGACY_OWNER_DELEGATION_EXEC_PATTERNS_ES + _LEGACY_OWNER_DELEGATION_EXEC_PATTERNS_EN
    ):
        if _legacy_owner_delegation_pattern_matches(
            normalized=normalized, pattern=pattern, kind="execution"
        ):
            return True

    for pattern in (
        _LEGACY_OWNER_DELEGATION_DECISION_PATTERNS_ES
        + _LEGACY_OWNER_DELEGATION_DECISION_PATTERNS_EN
    ):
        if _legacy_owner_delegation_pattern_matches(
            normalized=normalized, pattern=pattern, kind="decision"
        ):
            return True

    for pattern in (
        _LEGACY_OWNER_DELEGATION_NO_MANUAL_PATTERNS_ES
        + _LEGACY_OWNER_DELEGATION_NO_MANUAL_PATTERNS_EN
    ):
        if _legacy_owner_delegation_pattern_matches(
            normalized=normalized, pattern=pattern, kind="no_manual_work"
        ):
            return True

    return False


REPRESENTATIVE_DECISIONS: list[tuple[str, bool]] = [
    # execution block
    ("córrelo tú mismo", True),
    ("hazlo tú", True),
    ("run it yourself", True),
    ("you handle it", True),
    # decision block
    ("decide tú", True),
    ("tú eliges", True),
    ("you choose", True),
    ("you pick", True),
    # no_manual_work block
    ("te toca a ti", True),
    ("encárgate tú", True),
    ("take ownership", True),
    ("stop asking me to type", True),
    # Inline action hints remain route matches; the classifier keeps owning
    # extraction of the rich hint used by the resolver.
    ("encárgate tú de actualizar el deck", True),
    ("take ownership of preparing the report", True),
    # Lexical triggers with hypothetical/weak direction scores stay rejected.
    ("¿debería hacerlo tú o yo?", False),
    ("antes de que hagas algo, dime si decide tú", False),
    ("what would happen if you decide?", False),
    ("should you do it or should I?", False),
    # Empty, exterior whitespace, casual chat, and unrelated imperatives.
    ("", False),
    ("   ", False),
    ("hola", False),
    ("ok", False),
    ("gracias", False),
    ("perfecto", False),
    ("envía el informe diario", False),
    ("abre Linear", False),
]


class OwnerDelegationDecisionLockTests(unittest.TestCase):
    def test_frozen_reference_does_not_call_the_production_detector(self) -> None:
        legacy_functions = (
            _legacy_normalize_command_text,
            _legacy_owner_delegation_lexical_score,
            _legacy_owner_delegation_direction_score,
            _legacy_owner_delegation_pattern_matches,
            _legacy_matches_owner_delegation,
        )
        call_paths = {
            _call_path(node)
            for function in legacy_functions
            for node in ast.walk(_function_ast(function))
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("detect_owner_delegation", call_paths)

    def test_new_matcher_reproduces_legacy_decisions(self) -> None:
        for text, expected in REPRESENTATIVE_DECISIONS:
            with self.subTest(text=text):
                legacy = _legacy_matches_owner_delegation(text)
                self.assertEqual(legacy, expected, "corpus drifted from the legacy gate")
                self.assertEqual(
                    OWNER_DELEGATION_MATCHER.match(text),
                    legacy,
                    "declarative matcher diverged from the legacy gate",
                )


# Owner delegation runs after failure-summary, operational-status and cleanup,
# and before change-status in the order-locked chain. Rows here are exclusive;
# the collision family below records inputs matched by an earlier route too.
# (owner, change, cleanup, operational, failure) per text.
OVERLAP_DECISIONS: list[tuple[str, bool, bool, bool, bool, bool]] = [
    ("decide tú", True, False, False, False, False),
    ("encárgate tú de actualizar el deck", True, False, False, False, False),
    ("estado de los cambios", False, True, False, False, False),
    ("¿ya limpiaste?", False, False, True, False, False),
    ("status", False, False, False, True, False),
    ("hoy hubo errores", False, False, False, False, True),
    ("hola", False, False, False, False, False),
]

# Earlier routes win these legacy collisions by chain position. Extraction of
# a matcher does not arbitrate or reorder them.
ORDER_RESOLVED_COLLISIONS: list[tuple[str, bool, bool, bool]] = [
    # (text, owner, operational_status, failure_summary)
    ("hola status decide tú", True, True, False),
    ("hoy hubo errores; encárgate tú", True, False, True),
    # Triple collision: failure-summary wins because it runs first.
    ("hola status hoy errores; decide tú", True, True, True),
    # A1 rejects failure-summary, flipping the earlier-route winner to
    # operational-status while owner delegation remains a later match.
    ("hola status hoy errores; no continuemos; decide tú", True, True, False),
]


class MatcherOverlapTests(unittest.TestCase):
    def test_overlap_with_all_four_existing_matchers_is_locked(self) -> None:
        for (
            text,
            exp_owner,
            exp_change,
            exp_cleanup,
            exp_operational,
            exp_failure,
        ) in OVERLAP_DECISIONS:
            with self.subTest(text=text):
                self.assertEqual(OWNER_DELEGATION_MATCHER.match(text), exp_owner)
                self.assertEqual(CHANGE_STATUS_MATCHER.match(text), exp_change)
                self.assertEqual(CLEANUP_STATUS_MATCHER.match(text), exp_cleanup)
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_operational)
                self.assertEqual(OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text), exp_failure)

    def test_exclusive_overlap_rows_have_at_most_one_match(self) -> None:
        for row in OVERLAP_DECISIONS:
            with self.subTest(text=row[0]):
                self.assertLessEqual(sum(row[1:]), 1)

    def test_order_resolved_collisions_are_locked(self) -> None:
        for text, exp_owner, exp_operational, exp_failure in ORDER_RESOLVED_COLLISIONS:
            with self.subTest(text=text):
                self.assertEqual(OWNER_DELEGATION_MATCHER.match(text), exp_owner)
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_operational)
                self.assertEqual(OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text), exp_failure)
                self.assertFalse(CHANGE_STATUS_MATCHER.match(text))
                self.assertFalse(CLEANUP_STATUS_MATCHER.match(text))


class OwnerDelegationMatcherContractTests(unittest.TestCase):
    def test_base_telemetry_slugs_are_byte_identical_to_pre_slice(self) -> None:
        self.assertEqual(OWNER_DELEGATION_MATCHER.name, "owner_delegation")
        self.assertEqual(
            OWNER_DELEGATION_MATCHER.matched_reason,
            "owner_delegation_matched",
        )
        self.assertEqual(
            OWNER_DELEGATION_MATCHER.unmatched_reason,
            "owner_delegation_no_match",
        )

    def test_matcher_is_frozen_data(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            OWNER_DELEGATION_MATCHER.name = "x"  # type: ignore[misc]

    def test_matcher_predicate_takes_literal_text_only(self) -> None:
        self.assertEqual(
            list(inspect.signature(OWNER_DELEGATION_MATCHER.match).parameters),
            ["text"],
        )

    def test_route_matcher_shape_remains_the_pilot_contract(self) -> None:
        self.assertEqual(
            set(RouteMatcher.__dataclass_fields__),
            {"name", "match", "matched_reason", "unmatched_reason"},
        )


def _function_ast(owner: object) -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(owner)))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef))


def _call_path(call: ast.Call) -> str | None:
    parts: list[str] = []
    node: ast.expr = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _expression_dump(source: str) -> str:
    return ast.dump(ast.parse(source, mode="eval").body, include_attributes=False)


def _dispatch_calls(owner: object) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_function_ast(owner))
        if isinstance(node, ast.Call) and _call_path(node) == "self._emit_dispatch_decision"
    ]


class BotWiringSingleSourceTests(unittest.TestCase):
    def test_gate_uses_matcher_then_materializes_the_rich_intent(self) -> None:
        gate = _function_ast(BotService._maybe_handle_owner_delegation_request)
        matcher_calls = [
            node
            for node in ast.walk(gate)
            if isinstance(node, ast.Call) and _call_path(node) == "OWNER_DELEGATION_MATCHER.match"
        ]
        detector_calls = [
            node
            for node in ast.walk(gate)
            if isinstance(node, ast.Call) and _call_path(node) == "detect_owner_delegation"
        ]
        self.assertEqual(len(matcher_calls), 1)
        self.assertEqual(len(detector_calls), 1)
        self.assertEqual(
            ast.dump(matcher_calls[0].args[0], include_attributes=False),
            _expression_dump("text"),
        )
        self.assertLess(matcher_calls[0].lineno, detector_calls[0].lineno)

    def test_gate_carries_no_classifier_patterns_or_scores(self) -> None:
        gate_src = inspect.getsource(BotService._maybe_handle_owner_delegation_request)
        self.assertNotIn("_OWNER_DELEGATION_", gate_src)
        self.assertNotIn("lexical_score", gate_src)
        self.assertNotIn("direction_score", gate_src)

    def test_gate_no_match_event_slug_comes_from_the_matcher(self) -> None:
        gate = _function_ast(BotService._maybe_handle_owner_delegation_request)
        event_args = [
            node.args[0]
            for node in ast.walk(gate)
            if isinstance(node, ast.Call) and _call_path(node) == "self.observe.emit" and node.args
        ]
        self.assertIn(
            _expression_dump("OWNER_DELEGATION_MATCHER.unmatched_reason"),
            [ast.dump(arg, include_attributes=False) for arg in event_args],
        )

    def test_text_chain_base_dispatch_kwargs_come_from_matcher_data(self) -> None:
        captured_dump = _expression_dump("owner_delegation_response is not None")
        candidates: list[ast.Call] = []
        for call in _dispatch_calls(BotService._handle_text_body):
            kwargs = {keyword.arg: keyword.value for keyword in call.keywords}
            captured = kwargs.get("captured")
            if (
                captured is not None
                and ast.dump(captured, include_attributes=False) == captured_dump
            ):
                candidates.append(call)
        self.assertEqual(len(candidates), 1)
        call = candidates[0]
        self.assertEqual(call.args, [])
        actual = {
            keyword.arg: ast.dump(keyword.value, include_attributes=False)
            for keyword in call.keywords
        }
        expected = {
            key: _expression_dump(value)
            for key, value in {
                "handler": "OWNER_DELEGATION_MATCHER.name",
                "route": (
                    '"intercepted" if owner_delegation_response is not None else "fall_through"'
                ),
                "reason": (
                    "OWNER_DELEGATION_MATCHER.matched_reason "
                    "if owner_delegation_response is not None else "
                    "OWNER_DELEGATION_MATCHER.unmatched_reason"
                ),
                "session_id": "session_id",
                "text": "stripped",
                "captured": "owner_delegation_response is not None",
            }.items()
        }
        self.assertEqual(actual, expected)

    def test_inner_dispatch_keeps_kind_derived_dynamic_telemetry(self) -> None:
        calls = _dispatch_calls(BotService._maybe_handle_owner_delegation_request)
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call.args, [])
        actual = {
            keyword.arg: ast.dump(keyword.value, include_attributes=False)
            for keyword in call.keywords
        }
        dynamic_reason = 'f"owner_delegation:{intent.kind}"'
        expected = {
            key: _expression_dump(value)
            for key, value in {
                "handler": "OWNER_DELEGATION_MATCHER.name",
                "route": '"intercepted"',
                "reason": dynamic_reason,
                "session_id": "session_id",
                "text": "text",
                "captured": "True",
                "matched_pattern": dynamic_reason,
            }.items()
        }
        self.assertEqual(actual, expected)

    def test_text_chain_keeps_memory_limit_and_has_no_quality_guard(self) -> None:
        chain_src = inspect.getsource(BotService._handle_text_body)
        start = chain_src.index("owner_delegation_response =")
        end = chain_src.index("telegram_imperative_response", start)
        owner_block = chain_src[start:end]
        self.assertIn("assistant_limit=4000", owner_block)
        self.assertNotIn("_quality_guard_response", owner_block)
        self.assertNotIn("dispatch_routes", owner_block)

    def test_b45e_registry_artifacts_do_not_exist(self) -> None:
        self.assertNotIn("_owner_delegation_slot", inspect.getsource(BotService.__init__))
        self.assertFalse(hasattr(BotService, "_route_owner_delegation"))

    def test_direct_detector_consumers_are_explicitly_bounded(self) -> None:
        # The two route-related consumers are the matcher predicate (boolean)
        # and the legacy gate (rich intent materialization after that boolean
        # check). The other three are semantic/actionability classifiers; they
        # do not emit a route decision and are not parallel route recognizers.
        consumers: set[tuple[str, str]] = set()
        for path in sorted(Path("claw_v2").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            definitions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    definitions.append((node.name, node))
                elif isinstance(node, ast.ClassDef):
                    definitions.extend(
                        (f"{node.name}.{item.name}", item)
                        for item in node.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    )
            for qualified_name, definition in definitions:
                if any(
                    isinstance(node, ast.Call) and _call_path(node) == "detect_owner_delegation"
                    for node in ast.walk(definition)
                ):
                    consumers.add((path.as_posix(), qualified_name))

        self.assertEqual(
            consumers,
            {
                ("claw_v2/bot.py", "_looks_like_operator_action_request"),
                (
                    "claw_v2/bot.py",
                    "BotService._maybe_handle_owner_delegation_request",
                ),
                (
                    "claw_v2/bot_helpers.py",
                    "looks_like_actionable_telegram_message",
                ),
                (
                    "claw_v2/dispatch/matchers.py",
                    "looks_like_owner_delegation_request",
                ),
                ("claw_v2/semantic_turn.py", "classify_semantic_turn"),
            },
        )


if __name__ == "__main__":
    unittest.main()
