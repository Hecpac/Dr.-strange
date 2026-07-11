from __future__ import annotations

import ast
import inspect
import re
import textwrap
import unicodedata
import unittest
from dataclasses import FrozenInstanceError

from claw_v2.bot import BotService
from claw_v2.dispatch.matchers import (
    ACTIONABLE_TASK_MATCHER,
    CHANGE_STATUS_MATCHER,
    CLEANUP_STATUS_MATCHER,
    OPERATIONAL_FAILURE_SUMMARY_MATCHER,
    OPERATIONAL_STATUS_MATCHER,
    OWNER_DELEGATION_MATCHER,
    RouteMatcher,
)

# B4.4f extracts ONLY the direct actionable-task literal predicate from
# BotService into frozen RouteMatcher data. Stateful follow-up resolution,
# semantic-turn gates, continuation handling, preflight and execution remain
# on BotService. The legacy _handle_text_body call stays at its order-locked
# row 8 slot; B4.5f registry invocation is explicitly separate.


def _legacy_normalize_command_text(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _legacy_looks_like_direct_actionable_task(text: str) -> bool:
    """Independent verbatim copy of the pre-B4.4f literal recognizer."""
    normalized = _legacy_normalize_command_text(text)
    # "pr" must be a standalone token, not a substring of "pregunta",
    # "preocupa", "preferir", etc. Same for the action verbs: require a
    # word-boundary start so "completas" (2nd-person present) does not
    # trip the PR-completion branch when paired with "pregunta".
    pr_completion = (
        re.search(r"\bpr\b", normalized) is not None
        and re.search(r"\b(termina|completa|finaliza)", normalized) is not None
    )
    return (
        (
            "actualiza" in normalized
            and any(token in normalized for token in ("codex", "claude", "codex app"))
        )
        or ("regenera" in normalized and "lock" in normalized)
        or ("poetry.lock" in normalized)
        or ("pyproject" in normalized and "lock" in normalized)
        or pr_completion
    )


DECISIONS_BY_BLOCK: dict[str, tuple[tuple[str, bool], ...]] = {
    "actualiza_product": (
        ("Actualiza Codex", True),
        ("actualíza Claude Code", True),
        ("ACTUALIZA CODEX APP", True),
        # Open substring semantics are legacy and intentionally frozen.
        ("desactualiza codex", True),
        ("revisa Codex", False),
        ("actualiza dependencias", False),
        ("Claude Code está listo", False),
    ),
    "regenera_lock": (
        ("Regenera el lock", True),
        ("REGÉNERA poetry lock", True),
        ("regenera lockfile", True),
        ("regenera dependencias", False),
        ("abre el lock", False),
    ),
    "poetry_dot_lock": (
        ("poetry.lock", True),
        ("Revisa POETRY.LOCK", True),
        ("poetry lock", False),
        ("requirements.lock", False),
    ),
    "pyproject_and_lock": (
        ("pyproject y lock", True),
        ("Revisa pyproject.toml; el lock cambió", True),
        ("pyproject.toml", False),
        ("lockfile", False),
    ),
    "pr_completion": (
        ("termina el PR", True),
        ("completa el PR #25", True),
        ("finaliza el pr de redaction", True),
        ("PR termina", True),
        # The action regex has a leading word boundary only; do not silently
        # add a trailing one during extraction.
        ("completas el PR", True),
        (
            "Mi pregunta es porque no completas las tareas faciles o que no necesitan "
            "intervencion o permiso ?",
            False,
        ),
        ("Mi pregunta es porque no completas", False),
        ("preferir termina", False),
        ("prueba completa", False),
        ("pretermina PR", False),
        ("PR incompleta", False),
        ("_pr_ termina", False),
        ("PR2 termina", False),
        ("2PR termina", False),
        ("PR-2 termina", True),
        ("p.r. termina", False),
        ("Cuando termina la reunion de hoy?", False),
        ("PR listo", False),
    ),
}


# Exclusive rows exercise the new direct matcher against every matcher already
# migrated to declarative data. The separate collision family below records a
# real order-resolved overlap; extraction must not invent exclusivity.
OVERLAP_DECISIONS: tuple[tuple[str, bool, bool, bool, bool, bool, bool], ...] = (
    # text, actionable, owner, failure, operational, cleanup, change
    ("Actualiza Codex", True, False, False, False, False, False),
    ("decide tú", False, True, False, False, False, False),
    ("hoy hubo errores", False, False, True, False, False, False),
    ("status", False, False, False, True, False, False),
    ("¿ya limpiaste?", False, False, False, False, True, False),
    ("estado de los cambios", False, False, False, False, False, True),
    ("hola", False, False, False, False, False, False),
)


# "hazlo tú" is NOT a direct actionable-task literal, but it remains a legacy
# actionable follow-up and an owner-delegation match. Owner runs earlier at row
# 7, so it is the full-chain winner before actionable row 8 is reached.
ORDER_RESOLVED_COLLISIONS: tuple[tuple[str, bool, bool, bool], ...] = (
    # text, direct actionable matcher, actionable follow-up, owner matcher
    ("hazlo tú", False, True, True),
    ("ejecútalo tú", False, True, True),
)


# The direct predicate itself also has real collisions with earlier routes.
# These are not exclusivity rows: source order, not matcher mutation, chooses
# the winner.
ORDER_RESOLVED_DIRECT_COLLISIONS: tuple[tuple[str, bool, bool, bool, bool], ...] = (
    # text, actionable, owner, failure-summary, operational-status
    ("actualiza Codex, hazlo tú", True, True, False, False),
    ("hoy errores actualiza Codex", True, False, True, False),
    ("hola status actualiza Codex", True, False, False, True),
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


class ActionableTaskDecisionLockTests(unittest.TestCase):
    def test_frozen_reference_is_independent_of_production_matcher(self) -> None:
        calls = {
            _call_path(node)
            for node in ast.walk(_function_ast(_legacy_looks_like_direct_actionable_task))
            if isinstance(node, ast.Call)
        }
        self.assertNotIn("ACTIONABLE_TASK_MATCHER.match", calls)
        self.assertNotIn("_normalize_command_text", calls)
        self.assertIn("_legacy_normalize_command_text", calls)

    def test_new_matcher_reproduces_legacy_decisions_by_block(self) -> None:
        for block, decisions in DECISIONS_BY_BLOCK.items():
            for text, expected in decisions:
                with self.subTest(block=block, text=text):
                    legacy = _legacy_looks_like_direct_actionable_task(text)
                    self.assertEqual(legacy, expected, "corpus drifted from legacy")
                    self.assertEqual(
                        ACTIONABLE_TASK_MATCHER.match(text),
                        legacy,
                        "declarative matcher diverged from legacy",
                    )


class MatcherOverlapTests(unittest.TestCase):
    def test_exclusive_overlap_rows_are_locked(self) -> None:
        for row in OVERLAP_DECISIONS:
            text, exp_action, exp_owner, exp_failure, exp_op, exp_cleanup, exp_change = row
            with self.subTest(text=text):
                self.assertEqual(ACTIONABLE_TASK_MATCHER.match(text), exp_action)
                self.assertEqual(OWNER_DELEGATION_MATCHER.match(text), exp_owner)
                self.assertEqual(OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text), exp_failure)
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_op)
                self.assertEqual(CLEANUP_STATUS_MATCHER.match(text), exp_cleanup)
                self.assertEqual(CHANGE_STATUS_MATCHER.match(text), exp_change)
                self.assertLessEqual(sum(row[1:]), 1)

    def test_legacy_followup_owner_collisions_are_order_resolved(self) -> None:
        body_src = inspect.getsource(BotService._handle_text_body)
        owner_at = body_src.index("self._owner_delegation_slot")
        imperative_at = body_src.index("self._maybe_handle_telegram_imperative_request")
        actionable_at = body_src.index("self._maybe_handle_actionable_task_request")
        self.assertLess(owner_at, imperative_at)
        self.assertLess(imperative_at, actionable_at)

        for text, exp_direct, exp_followup, exp_owner in ORDER_RESOLVED_COLLISIONS:
            with self.subTest(text=text):
                self.assertEqual(ACTIONABLE_TASK_MATCHER.match(text), exp_direct)
                self.assertEqual(BotService._looks_like_actionable_followup(text), exp_followup)
                self.assertEqual(OWNER_DELEGATION_MATCHER.match(text), exp_owner)

    def test_direct_matcher_collisions_keep_earlier_route_winners(self) -> None:
        body_src = inspect.getsource(BotService._handle_text_body)
        failure_at = body_src.index("self._failure_summary_slot")
        operational_at = body_src.index("self._operational_status_slot")
        owner_at = body_src.index("self._owner_delegation_slot")
        actionable_at = body_src.index("self._maybe_handle_actionable_task_request")
        self.assertLess(failure_at, operational_at)
        self.assertLess(operational_at, owner_at)
        self.assertLess(owner_at, actionable_at)

        for (
            text,
            exp_action,
            exp_owner,
            exp_failure,
            exp_operational,
        ) in ORDER_RESOLVED_DIRECT_COLLISIONS:
            with self.subTest(text=text):
                self.assertEqual(ACTIONABLE_TASK_MATCHER.match(text), exp_action)
                self.assertEqual(OWNER_DELEGATION_MATCHER.match(text), exp_owner)
                self.assertEqual(OPERATIONAL_FAILURE_SUMMARY_MATCHER.match(text), exp_failure)
                self.assertEqual(OPERATIONAL_STATUS_MATCHER.match(text), exp_operational)


class ActionableTaskMatcherContractTests(unittest.TestCase):
    def test_telemetry_slugs_are_byte_identical_to_pre_slice(self) -> None:
        self.assertEqual(ACTIONABLE_TASK_MATCHER.name, "telegram_actionable_task")
        self.assertEqual(
            ACTIONABLE_TASK_MATCHER.matched_reason,
            "telegram_actionable_task_matched",
        )
        self.assertEqual(
            ACTIONABLE_TASK_MATCHER.unmatched_reason,
            "telegram_actionable_task_no_match",
        )

    def test_matcher_is_frozen_data(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            ACTIONABLE_TASK_MATCHER.name = "x"  # type: ignore[misc]

    def test_matcher_predicate_takes_literal_text_only(self) -> None:
        self.assertEqual(
            list(inspect.signature(ACTIONABLE_TASK_MATCHER.match).parameters),
            ["text"],
        )
        source = inspect.getsource(ACTIONABLE_TASK_MATCHER.match)
        self.assertNotIn("session_state", source)
        self.assertNotIn("semantic_turn", source)
        self.assertNotIn("reply_context", source)

    def test_route_matcher_shape_remains_the_pilot_contract(self) -> None:
        self.assertEqual(
            set(RouteMatcher.__dataclass_fields__),
            {"name", "match", "matched_reason", "unmatched_reason"},
        )


class BotWiringSingleSourceTests(unittest.TestCase):
    def test_bot_carries_no_parallel_direct_recognizer(self) -> None:
        self.assertFalse(
            hasattr(BotService, "_looks_like_direct_actionable_task"),
            "the BotService staticmethod must be deleted, not kept in parallel",
        )

    def test_both_direct_literal_checks_consume_matcher_data(self) -> None:
        owners = (
            BotService._maybe_handle_actionable_task_request,
            BotService._resolve_actionable_task_objective,
        )
        for owner in owners:
            with self.subTest(owner=owner.__name__):
                calls = [
                    node
                    for node in ast.walk(_function_ast(owner))
                    if isinstance(node, ast.Call)
                    and _call_path(node) == "ACTIONABLE_TASK_MATCHER.match"
                ]
                self.assertEqual(len(calls), 1)
                self.assertEqual(
                    ast.dump(calls[0].args[0], include_attributes=False),
                    _expression_dump("text"),
                )

    def test_stateful_followup_and_execution_stay_on_botservice(self) -> None:
        gate_src = inspect.getsource(BotService._maybe_handle_actionable_task_request)
        resolver_src = inspect.getsource(BotService._resolve_actionable_task_objective)
        self.assertIn("semantic_turn.intent", gate_src)
        self.assertIn("self.brain.memory.get_session_state", gate_src)
        self.assertIn("self._maybe_resolve_telegram_continuation", gate_src)
        self.assertIn("self._run_capability_preflight", gate_src)
        self.assertIn("self._task_handler.start_autonomous_task", gate_src)
        self.assertIn("self._looks_like_actionable_followup", resolver_src)
        self.assertIn('state.get("pending_action")', resolver_src)

    def test_handler_and_resolver_emit_no_inner_dispatch_decisions(self) -> None:
        self.assertEqual(
            _dispatch_calls(BotService._maybe_handle_actionable_task_request),
            [],
        )
        self.assertEqual(
            _dispatch_calls(BotService._resolve_actionable_task_objective),
            [],
        )

    def test_runtime_and_slash_gates_stay_outside_the_literal_matcher(self) -> None:
        # The extracted predicate stays verbatim even where an earlier handler
        # gate rejects the message for channel/command reasons.
        self.assertTrue(ACTIONABLE_TASK_MATCHER.match("/termina PR"))
        gate_src = inspect.getsource(BotService._maybe_handle_actionable_task_request)
        runtime_at = gate_src.index('!= "telegram"')
        slash_at = gate_src.index('text.startswith("/")')
        matcher_at = gate_src.index("ACTIONABLE_TASK_MATCHER.match")
        self.assertLess(runtime_at, matcher_at)
        self.assertLess(slash_at, matcher_at)

    def test_legacy_call_site_keeps_exact_dispatch_kwargs_and_cardinality(self) -> None:
        body = _function_ast(BotService._handle_text_body)
        captured_dump = _expression_dump("actionable_task_response is not None")
        candidates: list[ast.Call] = []
        for node in ast.walk(body):
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
        self.assertTrue(
            any(
                isinstance(statement, ast.Expr) and statement.value is candidates[0]
                for statement in body.body
            ),
            "the one actionable decision must remain an unconditional top-level call",
        )
        actual = {
            keyword.arg: ast.dump(keyword.value, include_attributes=False)
            for keyword in candidates[0].keywords
        }
        expected = {
            key: _expression_dump(value)
            for key, value in {
                "handler": "ACTIONABLE_TASK_MATCHER.name",
                "route": (
                    '"intercepted" if actionable_task_response is not None else "fall_through"'
                ),
                "reason": (
                    "ACTIONABLE_TASK_MATCHER.matched_reason "
                    "if actionable_task_response is not None "
                    "else ACTIONABLE_TASK_MATCHER.unmatched_reason"
                ),
                "session_id": "session_id",
                "text": "stripped",
                "captured": "actionable_task_response is not None",
            }.items()
        }
        self.assertEqual(actual, expected)
        self.assertNotIn("matched_pattern", actual)
        self.assertFalse(
            any(isinstance(node, ast.JoinedStr) for node in ast.walk(candidates[0])),
            "actionable dispatch must not gain a dynamic reason",
        )

    def test_call_order_limit_and_no_guard_stay_legacy_exact(self) -> None:
        body = _function_ast(BotService._handle_text_body)
        body_src = inspect.getsource(BotService._handle_text_body)
        handler_at = body_src.index("actionable_task_response =")
        decision_at = body_src.index("self._emit_dispatch_decision", handler_at)
        store_at = body_src.index("self._store_memory_turn", decision_at)
        remember_at = body_src.index("self._remember_assistant_turn_state", store_at)
        return_at = body_src.index("return actionable_task_response", remember_at)
        f4_at = body_src.index("f4_delegation_response =", return_at)
        self.assertLess(handler_at, decision_at)
        self.assertLess(decision_at, store_at)
        self.assertLess(store_at, remember_at)
        self.assertLess(remember_at, return_at)
        self.assertLess(return_at, f4_at)

        actionable_block = body_src[handler_at:f4_at]
        self.assertIn("assistant_limit=2000", actionable_block)
        self.assertNotIn("_quality_guard_response", actionable_block)
        self.assertNotIn("_final_render", actionable_block)
        self.assertNotIn("_post_capture_intercepted", actionable_block)
        self.assertNotIn("dispatch_routes", actionable_block)
        self.assertNotIn("_actionable_task_slot", actionable_block)
        self.assertNotIn("_route_actionable", actionable_block)

        actionable_test = _expression_dump("actionable_task_response is not None")
        capture_ifs = [
            statement
            for statement in body.body
            if isinstance(statement, ast.If)
            and ast.dump(statement.test, include_attributes=False) == actionable_test
        ]
        self.assertEqual(len(capture_ifs), 1)
        capture_if = capture_ifs[0]
        self.assertEqual(capture_if.orelse, [])
        self.assertEqual(len(capture_if.body), 3)

        store_statement, remember_statement, return_statement = capture_if.body
        self.assertIsInstance(store_statement, ast.Expr)
        self.assertIsInstance(remember_statement, ast.Expr)
        self.assertIsInstance(return_statement, ast.Return)
        assert isinstance(store_statement, ast.Expr)
        assert isinstance(remember_statement, ast.Expr)
        assert isinstance(return_statement, ast.Return)
        self.assertIsInstance(store_statement.value, ast.Call)
        self.assertIsInstance(remember_statement.value, ast.Call)
        assert isinstance(store_statement.value, ast.Call)
        assert isinstance(remember_statement.value, ast.Call)

        store_call = store_statement.value
        remember_call = remember_statement.value
        self.assertEqual(_call_path(store_call), "self._store_memory_turn")
        self.assertEqual(
            [ast.dump(arg, include_attributes=False) for arg in store_call.args],
            [
                _expression_dump("session_id"),
                _expression_dump("stripped"),
                _expression_dump("actionable_task_response"),
            ],
        )
        self.assertEqual(
            {
                keyword.arg: ast.dump(keyword.value, include_attributes=False)
                for keyword in store_call.keywords
            },
            {"assistant_limit": _expression_dump("2000")},
        )
        self.assertEqual(_call_path(remember_call), "self._remember_assistant_turn_state")
        self.assertEqual(
            [ast.dump(arg, include_attributes=False) for arg in remember_call.args],
            [
                _expression_dump("session_id"),
                _expression_dump("stripped"),
                _expression_dump("actionable_task_response"),
            ],
        )
        self.assertEqual(remember_call.keywords, [])
        self.assertEqual(
            ast.dump(return_statement.value, include_attributes=False),
            _expression_dump("actionable_task_response"),
        )

        response_dump = _expression_dump("actionable_task_response")
        response_post_capture_calls = [
            node
            for node in ast.walk(body)
            if isinstance(node, ast.Call)
            and _call_path(node)
            in {"self._store_memory_turn", "self._remember_assistant_turn_state"}
            and any(ast.dump(arg, include_attributes=False) == response_dump for arg in node.args)
        ]
        self.assertEqual(response_post_capture_calls, [store_call, remember_call])

        def _assignment_call_path(statement: ast.stmt) -> str | None:
            if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
                return None
            return _call_path(statement.value)

        handler_statement = next(
            statement
            for statement in body.body
            if _assignment_call_path(statement) == "self._maybe_handle_actionable_task_request"
        )
        f4_statement = next(
            statement
            for statement in body.body
            if _assignment_call_path(statement) == "self._maybe_handle_f4_deterministic_delegation"
        )
        decision_statement = next(
            statement
            for statement in body.body
            if isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and _call_path(statement.value) == "self._emit_dispatch_decision"
            and statement.value
            in [
                node
                for node in ast.walk(body)
                if isinstance(node, ast.Call)
                and _call_path(node) == "self._emit_dispatch_decision"
                and any(
                    keyword.arg == "handler"
                    and ast.dump(keyword.value, include_attributes=False)
                    == _expression_dump("ACTIONABLE_TASK_MATCHER.name")
                    for keyword in node.keywords
                )
            ]
        )
        handler_index = body.body.index(handler_statement)
        self.assertEqual(body.body[handler_index + 1], decision_statement)
        self.assertEqual(body.body[handler_index + 2], capture_if)
        self.assertEqual(body.body[handler_index + 3], f4_statement)

    def test_b45f_registry_artifacts_do_not_exist(self) -> None:
        self.assertNotIn("_actionable_task_slot", inspect.getsource(BotService.__init__))
        self.assertNotIn(
            "actionable_task",
            inspect.getsource(BotService._build_pre_brain_routes),
        )
        self.assertFalse(hasattr(BotService, "_route_actionable_task"))


if __name__ == "__main__":
    unittest.main()
