from __future__ import annotations

import ast
import inspect
import re
import textwrap
import unittest
from dataclasses import FrozenInstanceError

import claw_v2.bot as bot_module
from claw_v2.bot import BotService
from claw_v2.bot_helpers import _normalize_command_text
from claw_v2.dispatch.matchers import (
    CHANGE_STATUS_MATCHER,
    CLEANUP_STATUS_MATCHER,
    OPERATIONAL_FAILURE_SUMMARY_MATCHER,
    OPERATIONAL_STATUS_MATCHER,
    RouteMatcher,
)

# B4.4d — fourth declarative matcher (follows the B4.4a/b/c shape, invariant
# b44d_failure_summary_matcher_is_declarative_data). The failure-summary
# route's match contract moved from the inline staticmethod
# BotService._matches_operational_failure_summary_request (7 blocks: stop
# markers -> specific-task-failure vs broad markers -> direct complaints ->
# por-que-no-completas -> failure terms -> explicit report terms with a
# word-boundary helper -> causal+task-context) to dispatch/matchers.py as
# frozen DATA, VERBATIM: the deliberate mix of substring membership and the
# regex word-boundary helper for single-word report terms is preserved
# unchanged, with NO widening. These tests lock: (1) old-vs-new decisions
# over an exhaustive corpus — the legacy predicate is frozen verbatim below
# as the reference; (2) explicit OVERLAP semantics against the three
# already-migrated matchers, including the ORDER-RESOLVED COLLISION family
# with operational_status found by the adversarial verification (greeting +
# status-token texts that also carry failure+report terms match BOTH — the
# chain order, §5.1 row 5 before row 6, resolves the winner; full-chain tests
# in tests/test_telegram_imperative_router.py lock that winner and its A1
# flip); (3) the dispatch_decision
# telemetry slugs, byte-identical to pre-slice; (4) single-sourcing — bot.py
# carries no parallel recognizer. Since B4.5d the text chain consumes matcher
# data through its per-slot Route adapter, while handle_multimodal remains the
# legacy direct gate+telemetry call site and stays AST-locked here. Response
# rendering (_format_operational_failure_summary + both call-site quality
# guards) stays on BotService — only invocation ownership changed downstream.

# The recognizer EXACTLY as it lived inline at bot.py:11594-11714 up to
# eef92cf (the handler normalized first: _normalize_command_text(text).strip()
# and passed the result). If the declarative matcher ever diverges from this
# behavior, the corpus below fails and the divergence must be a deliberate,
# reviewed edit.


def _legacy_matches_operational_failure_summary(text: str) -> bool:
    normalized = _normalize_command_text(text).strip()
    if not normalized:
        return False

    def _contains_report_term(term: str) -> bool:
        if " " in term:
            return term in normalized
        return (
            re.search(
                rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])",
                normalized,
            )
            is not None
        )

    stop_or_scope_markers = (
        "no continuemos",
        "no sigamos",
        "no avancemos",
        "paremos",
        "detengamos",
        "dejemos esto",
    )
    if any(marker in normalized for marker in stop_or_scope_markers):
        return False
    specific_task_failure = (
        "por que fallo la tarea",
        "porque fallo la tarea",
        "por que fallo el task",
        "porque fallo el task",
        "por que fallaste la tarea",
        "porque fallaste la tarea",
    )
    broad_report_markers = (
        "resumen",
        "recuento",
        "auditoria",
        "auditoría",
        "fallos",
        "errores",
        "hoy",
        "sesion",
        "sesión",
        "ninguna tarea",
        "tareas",
    )
    if any(phrase in normalized for phrase in specific_task_failure) and not any(
        marker in normalized for marker in broad_report_markers
    ):
        return False
    direct_complaints = (
        "no puede completar ninguna tarea",
        "no puedes completar ninguna tarea",
        "no estas completando ninguna tarea",
        "no estás completando ninguna tarea",
        "no completa ninguna tarea",
        "no estas completando tareas",
        "no estás completando tareas",
    )
    if any(phrase in normalized for phrase in direct_complaints):
        return True
    if "ninguna tarea" not in normalized and (
        "por que no completas" in normalized
        or "por qué no completas" in normalized
        or "porque no completas" in normalized
    ):
        return False
    failure_terms = (
        "fallo",
        "fallos",
        "fallaste",
        "fallado",
        "error",
        "errores",
        "problema",
        "problemas",
        "perdido",
        "perdida",
        "bloqueo",
        "bloqueos",
        "no complet",
    )
    explicit_report_terms = (
        "resumen",
        "recuento",
        "auditoria",
        "auditoría",
        "reporte",
        "explica",
        "hoy",
        "sesion",
        "sesión",
        "today",
        "summary",
    )
    causal_terms = ("porque", "por que", "por qué")
    task_context_terms = (
        "agente",
        "bot",
        "daemon",
        "tarea",
        "tareas",
        "task",
        "complet",
        "fallaste",
        "fallo",
        "fallos",
        "error",
        "errores",
        "bloqueo",
        "bloqueos",
    )
    has_failure_term = any(term in normalized for term in failure_terms)
    if not has_failure_term:
        return False
    if any(_contains_report_term(term) for term in explicit_report_terms):
        return True
    return any(term in normalized for term in causal_terms) and any(
        term in normalized for term in task_context_terms
    )


REPRESENTATIVE_DECISIONS: list[tuple[str, bool]] = [
    # A3 positives — direct complaints intercept immediately.
    ("no estás completando ninguna tarea", True),
    ("porque no estás completando ninguna tarea", True),
    ("no puede completar ninguna tarea", True),
    ("no puedes completar ninguna tarea", True),
    ("no estas completando ninguna tarea", True),
    ("no completa ninguna tarea", True),
    ("no estas completando tareas", True),
    ("no estás completando tareas", True),
    # A5+A6 positives — failure term + explicit report term (word-boundary).
    ("hoy hubo errores", True),
    ("dame el resumen de fallos de hoy", True),
    ("resumen de errores", True),
    ("explica los fallos", True),
    ("summary of errors today", True),
    ("Recuento de problemas de la sesión", True),
    ("auditoria de problemas", True),
    ("reporte de errores", True),
    # A2 escape — specific-task question WITH a broad report marker continues
    # into A5+A6 and captures.
    ("porque fallaste la tarea hoy", True),
    ("porque fallaste la tarea resumen", True),
    ("porque fallaste la tarea recuento", True),
    ("porque fallaste la tarea auditoria", True),
    ("porque fallaste la tarea auditoría", True),
    ("porque fallaste la tarea fallos", True),
    ("porque fallaste la tarea errores", True),
    ("porque fallaste la tarea sesion", True),
    ("porque fallaste la tarea sesión", True),
    ("porque fallaste la tarea ninguna tarea", True),
    ("porque fallaste la tarea tareas", True),
    # A5+A7 positives — failure term + causal + task context (NO explicit
    # report term). Includes the corrected row from the adversarial
    # verification: "fallaste" is simultaneously failure_term and
    # task_context_term, so the bare causal complaint matches.
    ("porque fallaste", True),
    ("por que hay errores en el daemon", True),
    ("porque el bot tiene problemas", True),
    ("porque el agente tiene problemas", True),
    ("porque la tarea tiene problemas", True),
    ("porque las tareas tienen problemas", True),
    ("porque el task tiene problemas", True),
    ("porque completar tuvo problemas", True),
    ("porque fallo", True),
    ("porque fallos", True),
    ("porque error", True),
    ("porque errores", True),
    ("porque bloqueo", True),
    ("porque bloqueos", True),
    # A1 negatives — stop/scope markers kill everything, even with report
    # terms present.
    ("no continuemos", False),
    ("resumen de errores de hoy, pero no continuemos", False),
    ("paremos", False),
    ("hoy hubo errores; no sigamos", False),
    ("hoy hubo errores; no avancemos", False),
    ("hoy hubo errores; detengamos", False),
    ("hoy hubo errores; dejemos esto", False),
    # A2 negatives — specific single-task questions without broad markers
    # belong to the brain.
    ("porque fallaste la tarea", False),
    ("por que fallo el task", False),
    ("por que fallo la tarea", False),
    ("porque fallo la tarea", False),
    ("porque fallo el task", False),
    ("por que fallaste la tarea", False),
    # A4 negative — completion meta-question without "ninguna tarea".
    ("por que no completas", False),
    ("porque no completas", False),
    # A4 bypass — "ninguna tarea" deliberately continues into A5+A7.
    ("por que no completas ninguna tarea", True),
    # A5 missing failure-term literals, each paired with an A6 report term.
    ("reporte: el agente ha fallado", True),
    ("reporte del proceso perdido", True),
    ("reporte de la tarea perdida", True),
    ("reporte del bloqueo", True),
    ("reporte: no completado", True),
    # A5 negatives — no failure term at all.
    ("dame el resumen de hoy", False),
    ("status del deploy", False),
    ("estado de los cambios", False),
    # A6 word-boundary negative — "resumenes" does not satisfy the
    # word-bounded "resumen", and no other branch rescues it.
    ("fallo en resumenes", False),
    # A6 left/right word boundaries reject letters, digits and underscores.
    ("fallo;resumen", True),
    ("fallo en preresumen", False),
    ("fallo en 2resumen", False),
    ("fallo en _resumen", False),
    ("fallo en resumen2", False),
    ("fallo en resumen_extra", False),
    # A7 negatives — failure term but neither report term nor causal+context.
    ("hubo un fallo", False),
    ("errores", False),
    ("porque problemas", False),
    # Exterior whitespace is stripped before the seven blocks run.
    (" \t\nhoy hubo errores\r\n ", True),
    ("", False),
]


class FailureSummaryDecisionLockTests(unittest.TestCase):
    def test_new_matcher_reproduces_legacy_decisions(self) -> None:
        for text, expected in REPRESENTATIVE_DECISIONS:
            with self.subTest(text=text):
                legacy = _legacy_matches_operational_failure_summary(text)
                self.assertEqual(legacy, expected, "corpus drifted from the legacy reference")
                self.assertEqual(
                    OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text),
                    legacy,
                    "declarative matcher diverged from the legacy recognizer",
                )


# Overlap contract with the three already-migrated matchers. failure_summary
# runs EARLIEST of the four in the order-locked dispatch (§5.1 row 5, before
# operational_status row 6, cleanup row 7 and change-status row 10).
# (fs, op, chg, cln) per text. Every row here has at most ONE owner.
OVERLAP_DECISIONS: list[tuple[str, bool, bool, bool, bool]] = [
    # failure-summary exclusives.
    ("hoy hubo errores", True, False, False, False),
    ("dame el resumen de fallos de hoy", True, False, False, False),
    ("porque fallaste", True, False, False, False),
    # change-status exclusives (fullmatch phrases; failure_terms guard keeps
    # failure-summary out, incl. the PR #246 estados-plural widening).
    ("estado de los cambios", False, False, True, False),
    ("Estados de los cambios", False, False, True, False),
    # operational-status exclusives.
    ("status", False, True, False, False),
    ("hola estado de los cambios", False, True, False, False),
    # cleanup exclusive.
    ("¿ya limpiaste?", False, False, False, True),
    # nobody — falls to the brain.
    ("porque fallaste la tarea", False, False, False, False),
    ("dame el status del deploy", False, False, False, False),
]


# ORDER-RESOLVED COLLISIONS (adversarial verification, 2026-07-09): the
# operational_status greeting+token branch matches by open substring, so a
# greeting + status-token text that ALSO carries failure+report terms
# satisfies BOTH matchers. That is live behavior today, not introduced by any
# migration. Matchers alone cannot arbitrate — the order-locked chain does:
# failure_summary (§5.1 row 5) dispatches BEFORE operational_status (row 6),
# so when both are True, failure_summary wins. The A1 stop-marker row flips
# ownership to operational_status because A1 rejects failure_summary before
# its report branches run. This table locks both predicates; full-chain tests
# in tests/test_telegram_imperative_router.py lock the winner and early returns.
# (fs, op) per text; winner-by-order is fs whenever fs is True.
ORDER_RESOLVED_COLLISIONS: list[tuple[str, bool, bool]] = [
    ("hola status hoy errores", True, True),
    ("Buenos días, estatus: errores hoy", True, True),
    ("Good morning — status: errors today", True, True),
    ("hola status hoy errores; no continuemos", False, True),
]


class MatcherOverlapTests(unittest.TestCase):
    def test_overlap_with_migrated_matchers_is_locked(self) -> None:
        for text, exp_fs, exp_op, exp_chg, exp_cln in OVERLAP_DECISIONS:
            with self.subTest(text=text):
                self.assertEqual(
                    OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text), exp_fs, "fs drift"
                )
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_op, "op drift")
                self.assertEqual(CHANGE_STATUS_MATCHER.match(text), exp_chg, "chg drift")
                self.assertEqual(CLEANUP_STATUS_MATCHER.match(text), exp_cln, "cln drift")

    def test_no_exclusivity_case_claims_two_matchers(self) -> None:
        for text, exp_fs, exp_op, exp_chg, exp_cln in OVERLAP_DECISIONS:
            with self.subTest(text=text):
                self.assertLessEqual(exp_fs + exp_op + exp_chg + exp_cln, 1)

    def test_order_resolved_collisions_are_locked(self) -> None:
        for text, exp_fs, exp_op in ORDER_RESOLVED_COLLISIONS:
            with self.subTest(text=text):
                self.assertEqual(
                    OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text), exp_fs, "fs drift"
                )
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_op, "op drift")
                # change/cleanup never join this family.
                self.assertFalse(CHANGE_STATUS_MATCHER.match(text))
                self.assertFalse(CLEANUP_STATUS_MATCHER.match(text))

    def test_collision_family_always_includes_operational_status(self) -> None:
        # The documented ties are exactly fs+op; a corpus row that collides
        # any other way must be a new, deliberately reviewed entry.
        for text, exp_fs, exp_op in ORDER_RESOLVED_COLLISIONS:
            with self.subTest(text=text):
                self.assertTrue(exp_op)


class FailureSummaryMatcherContractTests(unittest.TestCase):
    def test_telemetry_slugs_are_byte_identical_to_pre_slice(self) -> None:
        # These three strings ride on every failure-summary dispatch_decision
        # event (both call sites); changing any is a telemetry-visible break.
        self.assertEqual(OPERATIONAL_FAILURE_SUMMARY_MATCHER.name, "operational_failure_summary")
        self.assertEqual(
            OPERATIONAL_FAILURE_SUMMARY_MATCHER.matched_reason,
            "operational_failure_summary_matched",
        )
        self.assertEqual(
            OPERATIONAL_FAILURE_SUMMARY_MATCHER.unmatched_reason,
            "operational_failure_summary_no_match",
        )

    def test_matcher_is_frozen_data(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            OPERATIONAL_FAILURE_SUMMARY_MATCHER.name = "x"  # type: ignore[misc]

    def test_matcher_predicate_takes_literal_text_only(self) -> None:
        # Routing Contract: the match side reads the literal message text
        # alone — no session_state, no ledger, no context objects.
        self.assertEqual(
            list(inspect.signature(OPERATIONAL_FAILURE_SUMMARY_MATCHER.match).parameters),
            ["text"],
        )

    def test_route_matcher_shape_is_the_pilot_contract(self) -> None:
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


class BotWiringSingleSourceTests(unittest.TestCase):
    def test_bot_carries_no_parallel_recognizer(self) -> None:
        # The 7-block inline predicate must not survive in the handler — the
        # matcher is the single source of the decision.
        gate_src = inspect.getsource(BotService._maybe_handle_operational_failure_summary)
        self.assertNotIn("stop_or_scope_markers", gate_src)
        self.assertNotIn("failure_terms", gate_src)
        self.assertNotIn("_contains_report_term", gate_src)
        self.assertFalse(
            hasattr(BotService, "_matches_operational_failure_summary_request"),
            "the inline staticmethod must be deleted, not kept as a parallel recognizer",
        )
        self.assertFalse(hasattr(bot_module, "_matches_operational_failure_summary_request"))

    def test_gate_consumes_the_declarative_matcher(self) -> None:
        gate_src = inspect.getsource(BotService._maybe_handle_operational_failure_summary)
        self.assertIn("OPERATIONAL_FAILURE_SUMMARY_MATCHER.match", gate_src)

    def test_legacy_multimodal_call_site_takes_slugs_from_the_matcher(self) -> None:
        # B4.5d moves only the text-chain invocation into registry Route data.
        # The multimodal block remains legacy and must keep all six real
        # dispatch_decision kwargs sourced from the matcher/call-site locals.
        common_expected = {
            "handler": "OPERATIONAL_FAILURE_SUMMARY_MATCHER.name",
            "route": ('"intercepted" if failure_summary_response is not None else "fall_through"'),
            "reason": (
                "OPERATIONAL_FAILURE_SUMMARY_MATCHER.matched_reason "
                "if failure_summary_response is not None else "
                "OPERATIONAL_FAILURE_SUMMARY_MATCHER.unmatched_reason"
            ),
            "session_id": "session_id",
            "captured": "failure_summary_response is not None",
        }
        captured_dump = _expression_dump(common_expected["captured"])
        candidates: list[ast.Call] = []
        for node in ast.walk(_function_ast(BotService.handle_multimodal)):
            if not isinstance(node, ast.Call) or _call_path(node) != "self._emit_dispatch_decision":
                continue
            kwargs = {keyword.arg: keyword.value for keyword in node.keywords}
            captured = kwargs.get("captured")
            if (
                captured is not None
                and ast.dump(captured, include_attributes=False) == captured_dump
            ):
                candidates.append(node)
        self.assertEqual(len(candidates), 1)
        call = candidates[0]
        self.assertEqual(call.args, [])
        actual = {
            keyword.arg: ast.dump(keyword.value, include_attributes=False)
            for keyword in call.keywords
        }
        expected = {
            key: _expression_dump(value)
            for key, value in {**common_expected, "text": "multimodal_text"}.items()
        }
        self.assertEqual(actual, expected)

    def test_multimodal_failure_summary_position_is_ast_locked(self) -> None:
        # Position is behavior: the literal-text gate and its telemetry must
        # run once, after text extraction and before the multimodal brain fallback.
        expected = [
            "self._text_from_multimodal_blocks",
            "self._maybe_handle_operational_failure_summary",
            "self._emit_dispatch_decision",
            "self.brain.handle_message",
        ]
        target_paths = set(expected)
        calls = sorted(
            (
                (node.lineno, node.col_offset, path)
                for node in ast.walk(_function_ast(BotService.handle_multimodal))
                if isinstance(node, ast.Call) and (path := _call_path(node)) in target_paths
            )
        )
        self.assertEqual([path for _, _, path in calls], expected)


if __name__ == "__main__":
    unittest.main()
